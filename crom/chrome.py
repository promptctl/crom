"""Starts, finds, and stops the Chrome processes behind resolved profiles.

[LAW:one-source-of-truth] The OS process table is the sole authority on "is this profile
running." We identify a crom-managed Chrome by the absolute `--user-data-dir` path it
was launched with — no pidfiles, no shadow state that can drift from reality.

That authority is scoped to profiles crom launched, and one other question needs a
different mechanism: "does *any* browser hold this directory." The user's own Chrome
runs with no `--user-data-dir` in its argv, so `scan` cannot see it at all —
`singleton_holder` reads Chrome's own process singleton instead. Two questions, two
mechanisms, both about Chrome, so both live here.

Nothing here reads a config file or decides a port — what to launch arrives already
resolved.
"""

import json
import os
import re
import signal
import socket
import subprocess
import textwrap
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from .model import CromError, ResolvedProfile

LAUNCH_TIMEOUT_SECONDS = 30.0
SHUTDOWN_TIMEOUT_SECONDS = 5.0

# Enough to carry Chrome's complaint and the lines around it, small enough that a browser
# which logged all day is quoted by its ending rather than its whole history.
STDERR_TAIL_BYTES = 4096

# What Chrome's stderr is called inside the profile's user-data-dir. Prefixed because
# Chrome writes `chrome_debug.log` into that same directory under `--enable-logging`, and
# an operator reading a launch failure should be able to tell at a glance which of the two
# files crom put there.
STDERR_FILENAME = "crom-stderr.log"

# How long crom keeps reading whatever answers on the CDP port, and how long any one
# blocking socket operation inside that may take. One probe costs at most the sum, and it
# is a sum because the deadline is checked between reads, so a read already in flight when
# it passes still runs to its own timeout. Both names exist so that arithmetic can be read
# somewhere; the second was once a bare `timeout=1` passed to an HTTP client, which left
# the quantity that actually matters — what one probe can cost — written down nowhere.
#
# The ceiling covers connecting, sending, and reading, because `_probe_port` owns its
# socket for all three. Borrowed, it covered only the phase after the headers: a status
# line trickled one byte per 0.9s held a probe open past 12 measured seconds, every
# individual recv legal, against a ceiling that then claimed 3s.
#
# The recv slice is not smaller because a real browser needs it. Measured against
# Chrome/152 on loopback, worst time from the connection being accepted to the reply being
# complete: 282ms over 16,197 samples on default flags. A timeout near that misreads a
# healthy Chrome as `_Silent` now and then — survivable, since `_Silent` is the retry
# state, but paid on every launch to tighten a ceiling that already sits ten times inside
# `LAUNCH_TIMEOUT_SECONDS`. 1.0s keeps three and a half times the measured worst.
PORT_REPLY_SECONDS = 2.0
PORT_RECV_SECONDS = 1.0

# How much of that reply to keep — a cap, and worth saying which. A CDP version document
# is 428 bytes as Chrome ships, but Chrome reflects `--user-agent` into it and that flag
# is user-configurable through `[defaults]`: measured, a 12,000-character agent makes the
# document 12,315 bytes. Clipping it stops it parsing as JSON, and crom would then call
# its own healthy browser a stranger — a false failure on a launch that worked. 64KB is
# over five times that worst case; a `--user-agent` past roughly that is the documented
# edge of what crom can recognise, rather than a surprise.
PORT_REPLY_BYTES = 65536

# One line of an unknown server's reply, long enough to recognise it by and short enough
# that a minified page cannot become the error message.
PORT_REPLY_SUMMARY_CHARS = 120

# HTTP/1.1 because Chrome's DevTools server answers nothing at all to HTTP/1.0 — measured.
# `Connection: close` is a courtesy to every other server: Chrome ignores it and holds the
# socket open, so the read cannot rely on it, but a listener that honours it lets the read
# finish on end-of-reply rather than on the clock.
_VERSION_REQUEST = (
    b"GET /json/version HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
)


# `ps` hands us one flat string per process with no argv boundaries, so the directory
# has to be delimited by something. It cannot be whitespace: a profile directory under a
# project path like `~/My Projects/app` contains spaces, and `(\S+)` would silently clip
# it to `~/My` — crom would then never recognise its own running browser. So the capture
# runs to the next ` --switch`, or to the end of the line.
#
# It deliberately does *not* depend on where `--user-data-dir` sits. An earlier version
# required it to be immediately followed by `--remote-debugging-port` at the very end —
# the shape `resolve.build_argv` emits — which made crom's own launch ordering a
# load-bearing assumption about a string that Chrome owns. Chrome re-execs itself
# (`--restart`) after something as ordinary as "relaunch the browser to load your profile
# data", and rewrites its argv when it does:
#
#     ... --remote-debugging-port=9223 --restart --user-data-dir=/…/dev --restart
#
# The anchored pattern does not match that. `scan()` then returns nothing, and every
# command that asks "is this profile running" is told no about a browser that is running:
# `up` starts a second Chrome on the same directory, `down` cannot find it, `list` reports
# it stopped, and `rm` deletes a live browser's user-data-dir. Observed against a real
# Chrome, not hypothesised. [FRAMING:representation] this pattern is a map of Chrome's
# *current* command line, not of how crom launched it; only the first is the territory.
#
# The cost of the looser terminator is that a directory containing a literal ` --word` is
# not recognisable. Flattened `ps` output genuinely cannot distinguish that from a switch,
# so no pattern over this input can. The old anchor made the same trade in reverse and had
# it backwards: it defended a path nobody has at the price of a restart everybody gets.
#
# The leading greedy `.*` still forces the *last* `--user-data-dir=`, so a configured flag
# whose value contains that literal text cannot shadow the real one (`parse_layer`
# inspects only the switch name before `=`, so such a value is not rejected).
_USER_DATA_DIR_RE = re.compile(r".*--user-data-dir=(.+?)(?=\s+--[A-Za-z0-9-]+|\s*\Z)")


def _group_by_user_data_dir(ps_output: str) -> dict[str, tuple[int, ...]]:
    """Parse `ps` output into main-browser PIDs grouped by their user-data-dir.

    Kept pure and separate from the `ps` call so the parsing — the part with the
    interesting edge cases — is testable without spawning processes.
    [LAW:effects-at-boundaries]
    """
    found: dict[str, list[int]] = {}
    for line in ps_output.splitlines():
        pid_str, _, cmd = line.strip().partition(" ")
        if not pid_str.isdigit():
            continue
        if "--type=" in cmd:  # helper/renderer/gpu — not the main process
            continue
        match = _USER_DATA_DIR_RE.search(cmd)
        if match:
            found.setdefault(match.group(1), []).append(int(pid_str))
    return {directory: tuple(pids) for directory, pids in found.items()}


def scan() -> dict[str, tuple[int, ...]]:
    """Every running main Chrome, grouped by the user-data-dir it was launched with.

    One `ps` call answers the liveness question for every profile at once, so listing
    twenty profiles costs one process scan rather than twenty.
    [LAW:one-source-of-truth] this is the only place crom reads the process table.
    """
    # macOS BSD `pgrep` doesn't support -a (print cmdline), so we use `ps` and filter in
    # Python. This is the portable path and gives us the full argv to distinguish the
    # main browser from helper processes.
    # This became the single process-table reader in this design, which concentrates the
    # benefit and the failure alike: `list`, `up`, `down`, `rm`, `config` and migration
    # all arrive here, so a raw `CalledProcessError` or a missing `ps` would escape the
    # exit-code contract from every one of them. [LAW:no-silent-failure]
    try:
        result = subprocess.run(
            ["ps", "-Ao", "pid=,command="],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as e:
        raise CromError(
            "could not read the process table: `ps` was not found on PATH.\n"
            "crom answers 'is this profile running' by reading `ps`, so it cannot work "
            "without it."
        ) from e
    except subprocess.CalledProcessError as e:
        raise CromError(
            f"could not read the process table: `ps` exited {e.returncode}"
            f"{(chr(10) + e.stderr.strip()) if e.stderr else ''}"
        ) from e
    return _group_by_user_data_dir(result.stdout)


def find_pids_for_dir(profile_dir: Path) -> tuple[int, ...]:
    """PIDs of the main browser process(es) using this user-data-dir.

    Keyed on the raw path rather than a `ResolvedProfile` so migration can ask about
    directories crom no longer has a profile for.
    """
    return scan().get(str(profile_dir), ())


def find_pids(profile: ResolvedProfile) -> tuple[int, ...]:
    return find_pids_for_dir(profile.profile_dir)


def is_running(profile: ResolvedProfile) -> bool:
    return bool(find_pids(profile))


# Chrome's own process singleton. At startup the browser creates this as a symlink whose
# target is `<hostname>-<pid>` — a link that never resolves, the target string being the
# whole payload — and removes it on clean exit. It is the only record of "someone is
# writing this user-data-dir" that covers a Chrome crom did not launch.
SINGLETON_LOCK = "SingletonLock"


def _pid_alive(pid: str) -> bool | None:
    """Is the process this text names running — or None when it names no pid to ask about.

    Takes the raw text rather than an int because asking the OS *is* how we learn the
    text was a pid at all: `str.isdigit` admits `'²'`, which `int` then rejects, and
    admits digit strings far past the `pid_t` `os.kill` takes. Splitting the question
    across two predicates let them disagree, and the disagreement left by exception.
    [LAW:parse-dont-validate]
    """
    try:
        os.kill(int(pid), 0)
        return True
    except (ValueError, OverflowError):
        # Not a number, or not one this machine can hold in a pid: either way the caller
        # is not looking at the convention Chrome writes.
        return None
    except ProcessLookupError:
        return False
    except PermissionError:
        # A fact about the process, not the path — the opposite reading to the one
        # `singleton_holder` gives this same exception. Alive and owned by another user:
        # what we lack is the right to signal it, which is not what we asked.
        return True


def singleton_holder(user_data_dir: Path) -> str | None:
    """What holds this user-data-dir against a consistent read, described — or None.

    The string is *evidence*, not a verdict: it says what was found on disk, and the
    caller writes the sentence around it. That is what lets the four ways of being held
    share one return type. [LAW:dataflow-not-control-flow]

      absent, or unreachable          nobody has it open, or Chrome exited cleanly → None
      `<host>-<pid>`, ours, alive     a browser is writing it                      → held
      `<host>-<pid>`, ours, dead      residue of a crash; nothing is writing        → None
      `<host>-<pid>`, another host    a pid this machine cannot ask about           → held
      unreadable, not `<host>-<pid>`, or a pid no OS could hold  → not the convention → held

    The last two are held because unknown is not the same as free, and only one of the
    two mistakes is recoverable: refusing a still directory costs the caller a command,
    while copying a moving one costs them a profile that fails weeks later.
    [LAW:no-silent-failure]
    """
    try:
        target = os.readlink(user_data_dir / SINGLETON_LOCK)
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        # These say the path could not be resolved, which is a fact about the path and
        # not about any lock — nothing holds a user-data-dir that is a regular file, and
        # nothing is proven by a directory we were not allowed to look in. Reading them
        # as held would send an operator off to quit a browser that was never open.
        #
        # Free is safe, not merely kinder: nothing can be observed inside a directory we
        # may not enter, and any operation that would act on this same path fails against
        # the same permission. [LAW:composability]
        return None
    except OSError as e:
        # Anything else — EINVAL for a `SingletonLock` that exists but is not a symlink,
        # EIO for a sick disk — found something or failed for a reason that does not
        # block the copy. Unknown is not free. [LAW:no-silent-failure]
        return f"{SINGLETON_LOCK} could not be read as a symlink: {e.strerror}"

    # Sanitised before it leaves: a `path` seed can name an untrusted tree, and this
    # evidence reaches a terminal through `_refuse`.
    seen = f"{SINGLETON_LOCK} -> {printable(target)}"
    # `rpartition`, because the host half carries hyphens of its own — `my-mac.local-123`
    # splits after the pid, never before it.
    host, _, pid = target.rpartition("-")
    alive = _pid_alive(pid)
    if not host or alive is None:
        return f"{seen}, which is not the `<host>-<pid>` Chrome writes"
    if host != socket.gethostname():
        # A profile on a synced or network home directory, locked from elsewhere. Also
        # what a same-machine hostname change looks like, so both names are quoted: macOS
        # moves between `.local` and `.lan` with the network, and the operator reading
        # this needs to see that the two strings differ only there.
        return (
            f"{seen}, which names host {host!r}; this machine is "
            f"{socket.gethostname()!r}, so that process cannot be asked whether it is "
            f"still running"
        )
    return f"{seen}, and that process is running" if alive else None


def printable(text: str) -> str:
    """Text crom did not write, rendered so a terminal will only ever display it.

    One job, done once wherever foreign text enters a message. Downstream then holds a
    printable string by construction rather than by discipline. [LAW:parse-dont-validate]

    Control characters are dropped because this text reaches a terminal that `DEVNULL`
    used to shield: Chrome's log lines carry page-derived text, and a listener on the port
    is unvetted by definition, so an escape sequence could repaint the error it appears
    in. Newline and tab are the message's shape, not control of the terminal, and stay.
    Callers holding bytes decode with `errors="replace"` on the way in, because refusing
    to decode would fail while reporting a failure.
    [LAW:single-enforcer] the sanitising happens here and nowhere else.
    """
    return "".join(ch for ch in text if ch in "\n\t" or ch.isprintable()).strip()


def _summarise(text: str) -> str:
    """One short printable line of something a stranger on the port chose to say.

    Every byte of it was written by whatever holds the port, and every byte of it lands in
    an error headed for a terminal, so being made safe and being made short are one job
    here rather than two that a message-building call site has to remember.
    """
    return textwrap.shorten(printable(text), PORT_REPLY_SUMMARY_CHARS, placeholder=" …")


def _lsof_hint(port: int) -> str:
    """How to find out who holds a port, worded the same wherever crom has to ask."""
    return f"Find it with: lsof -nP -iTCP:{port} -sTCP:LISTEN"


@dataclass(frozen=True)
class _Silent:
    """Nothing is listening on the port — the ordinary state while Chrome is starting."""


@dataclass(frozen=True)
class _Answered:
    """A Chrome DevTools endpoint answered on the port we asked for."""


@dataclass(frozen=True)
class _AnsweredByStranger:
    """Something answered on the port, but not as a browser crom could drive."""

    served: str


@dataclass(frozen=True)
class _Exited:
    """The child process was gone before CDP ever replied."""

    returncode: int


@dataclass(frozen=True)
class _NeverAnswered:
    """The deadline passed with the child still alive and the port still silent."""


# [LAW:types-are-the-program] Two questions, four endings, and the answers overlap because
# the facts do. The probe reports what the port is doing right now; the wait reports how
# the launch ended. Two of those are the same fact seen twice — a CDP endpoint answering
# *is* a successful launch, and a stranger answering *is* a failed one — so they are one
# variant each rather than a pair per union with an adapter in between. What the wait adds
# is the two endings a single probe cannot see, because both are about elapsed time: a
# child that died, and a port that stayed `_Silent` until the deadline.
#
# Before this union the wait returned pids or raised a timeout — two shapes for four
# endings, and the endings with nowhere to go were the common ones: a Chrome that died in
# 20ms got reported 30 seconds later as a port that had not opened, and a foreign server
# on the port got reported as a successful launch.
_PortAnswer = _Silent | _Answered | _AnsweredByStranger
_LaunchOutcome = _Answered | _AnsweredByStranger | _Exited | _NeverAnswered


def _advertises_devtools(reply: bytes) -> bool:
    """Whether this reply is a CDP version document — one that names a browser websocket.

    `webSocketDebuggerUrl` is the discriminator because it is the handle a CDP client
    actually connects by: a reply that names one is drivable, and a reply that does not is
    useless to crom whatever else it contains. Every way of not being that document —
    not JSON, JSON that is not an object, an object without the key — is the same answer,
    so they are caught together rather than enumerated into distinct diagnoses nobody
    would act on differently.
    """
    try:
        return str(json.loads(reply)["webSocketDebuggerUrl"]).startswith("ws://")
    except (ValueError, TypeError, KeyError, RecursionError):
        # `RecursionError` because `PORT_REPLY_BYTES` is large enough to hold JSON nested
        # deeper than the decoder will follow — measured, that starts around 32KB of
        # `[[[…`, which only a stranger would send and which is still just "not a DevTools
        # document". It is not a `ValueError`, so without it a hostile listener answers a
        # launch with a traceback instead of a CromError.
        return False


def _body_of(reply: bytes) -> bytes:
    """What follows the header block, or nothing while the headers are still arriving."""
    return reply.partition(b"\r\n\r\n")[2]


def _describe(reply: bytes) -> str:
    """One line naming what answered, for someone who has to go and find it.

    The body first, because that is where a server puts the page identifying itself, and
    the status line when there is no body — which is all a reply that never finished its
    headers has to offer, and exactly what a listener speaking some other protocol
    entirely hands over.
    """
    identifying = _body_of(reply) or reply.partition(b"\r\n")[0]
    return _summarise(identifying.decode("utf-8", "replace")) or "an empty reply"


def _classify(reply: bytes) -> _PortAnswer:
    """What was said on the port, as the answer it amounts to.

    [LAW:parse-dont-validate] the whole ladder lives here, so the three outcomes are read
    off one value rather than assembled from where an exception happened to be caught.
    Saying nothing is not the same as saying something crom cannot use: the first is a
    port that has not come up yet and is worth asking again, the second ends the launch.
    """
    if not reply:
        return _Silent()
    if _advertises_devtools(_body_of(reply)):
        return _Answered()
    return _AnsweredByStranger(_describe(reply))


@dataclass(frozen=True)
class _Said:
    """The peer finished. This is everything it was going to say, usable or not."""

    reply: bytes


@dataclass(frozen=True)
class _StillSpeaking:
    """The clock ran out mid-sentence, so what arrived is a prefix rather than a reply."""


# [LAW:types-are-the-program] The two endings differ in what they license, not in their
# bytes, so they cannot be one type carrying bytes. A prefix of a CDP document and a
# prefix of a dev server's page look exactly alike — there is nothing in a truncated reply
# to classify *with* — and classifying one anyway ends a launch on a guess, permanently,
# because a stranger is terminal. Only `_Said` is evidence.
_ReadOutcome = _Said | _StillSpeaking


def _read_reply(conn: socket.socket, deadline: float) -> _ReadOutcome:
    """Whatever the port says, until the answer is known or the clock runs out.

    Four endings, and the clock is the one that had to exist. An HTTP client borrowed from
    the library bounds each `recv` and never the call, so a listener trickling one byte
    just under that timeout keeps every individual read legal while the total runs for
    hours — measured, a status line paced at one byte per 0.9s held a probe open past 12s
    against a 3s ceiling. `_await_startup` cannot intervene, because it checks its deadline
    between probes and this is inside one. Bounding bytes never bounded that; only a clock
    does, and only a socket this function owns can be held to one for the whole exchange
    rather than for the part after the headers.
    [LAW:no-ambient-temporal-coupling] how long this may take is owned here rather than
    left to whatever is on the far end.

    Stopping as soon as the reply *is* a CDP document is what keeps the clock from costing
    anything. Chrome ignores `Connection: close` and holds the socket open — measured — so
    reading to end-of-reply would spend the full deadline on every healthy launch. There
    is nothing to wait for once the question is answered, so it does not wait: a stranger
    that holds the connection open spends the deadline, and spends it once, because that
    outcome ends the launch.
    """
    reply = b""
    while time.monotonic() < deadline:
        if len(reply) >= PORT_REPLY_BYTES:
            # More than any real version document, so there is nothing left to wait for:
            # whatever this is, it is not the browser, and it has said enough to say so.
            return _Said(reply)
        try:
            chunk = conn.recv(PORT_REPLY_BYTES - len(reply))
        except TimeoutError:
            # Nothing arrived in this slice, which says nothing about the peer being
            # finished — only the deadline gets to decide that, so it is asked again.
            # Breaking here instead made a browser that paused a second mid-reply look
            # like a stranger, and a stranger ends the launch.
            continue
        except OSError:
            # The peer broke the connection off. What it already said is all there is.
            return _Said(reply)
        if not chunk:
            return _Said(reply)
        reply += chunk
        if _advertises_devtools(_body_of(reply)):
            return _Said(reply)
    return _StillSpeaking()


def _probe_port(port: int) -> _PortAnswer:
    """Who is answering CDP's version endpoint on this port, if anyone.

    We probe the endpoint we intend to use rather than Chrome's DevToolsActivePort file:
    Chrome only writes that file to *report* a port it chose itself (the
    `--remote-debugging-port=0` case), not when we hand it a fixed port. The live
    endpoint is the honest readiness signal.

    [LAW:parse-dont-validate] The reply is read for what it proves rather than counted as
    an answer. This returned `resp.status == 200`, which accepted any HTTP server holding
    the port, so a dev server on 9222 made `launch` report success for a browser nothing
    could drive — a silent failure that surfaced later as a CDP client refusing to connect
    to a profile crom had called running.

    Anything that answers without being that document is a stranger rather than silence,
    since something is speaking on the port and it is not serving CDP. Measured, that
    costs no real launch — a starting browser is never briefly mistakable for a stranger:

        macOS,  Chrome/152      4 startups,   5ms sampling
        Linux,  Chromium/151   12 startups, tight loop, in a container, all cores loaded

    Every run on both, without exception, produced exactly two observations: connection
    refused, then a complete CDP document. The Linux runs are the stronger half — load
    stretched startup from 0.2s to 0.5s, widening any partially-initialised window, and
    49,598 samples landed inside it without once catching a non-200 or a malformed
    document. That is evidence, not proof; what would overturn it is a single sample of a
    starting browser answering something else, and a listener that hangs on the port for
    the whole timeout instead of failing in half a second is what it would cost.

    What this cannot tell apart is a *different* Chrome on our port, which advertises the
    same shape. `_require_port_available` rejects a port already held before launch, so
    what remains is a listener that appeared inside the wait window, where the realistic
    stranger is not a browser.

    The socket is ours rather than an HTTP client's because the deadline has to cover the
    whole exchange — see `_read_reply`. That also settles what a peer accepting and then
    closing means: it is a read of zero bytes, so silence falls out of the data, instead of
    resting on `RemoteDisconnected` being caught by one `except` arm and not the next.
    """
    deadline = time.monotonic() + PORT_REPLY_SECONDS
    try:
        with socket.create_connection(("127.0.0.1", port), PORT_RECV_SECONDS) as conn:
            conn.sendall(_VERSION_REQUEST)
            heard = _read_reply(conn, deadline)
    except OSError:
        # Overwhelmingly the ordinary case: the port refuses connections because Chrome
        # has not opened it yet. Also a connection that broke before it said anything,
        # which is the same fact — nothing on this port has spoken to us.
        return _Silent()

    match heard:
        case _Said(reply):
            return _classify(reply)
        case _StillSpeaking():
            # Not evidence of a stranger, so not treated as one. Silence is retryable and
            # a stranger is terminal, which makes this the difference between a loaded
            # machine costing another 100ms round and a launch that fails for good.
            return _Silent()


def _port_is_free(port: int) -> bool:
    """Whether a listener could take this port right now.

    Asked by binding rather than by connecting, because those are different questions and
    the difference is the whole point: connecting asks whether anyone is *answering*,
    binding asks whether the port is *available* — which is the question a launch actually
    puts to the kernel. They part company whenever a socket outlives whoever was serving
    through it, so a port that `_probe_port` calls silent can still refuse the bind.

    [LAW:one-source-of-truth] the pre-launch check and the post-stop wait ask the same
    question here, so they cannot come to disagree about what a free port is.
    """
    # Two `OSError`s with nothing in common but their type. A refused bind is the answer
    # — the port is held — while a socket that cannot be made at all (fd exhaustion) means
    # the question could not be asked, and reporting that as "held" would be an answer
    # shaped like a fact about the port for a failure that has nothing to do with it.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                return False
    except OSError as e:
        # [LAW:no-silent-failure] `kill` reaches here too, so a raw OSError would leave
        # `down` and `rm` outside the CLI's exit-code contract as a traceback.
        raise CromError(f"could not check whether port {port} is free: {e}") from e
    return True


def _require_port_available(profile: ResolvedProfile) -> None:
    """Fail immediately, and by name, when something else already holds the port.

    Without this the launch simply times out after 30s and blames the wrong thing.
    [LAW:no-silent-failure] the diagnosis belongs at the moment of failure.
    """
    if not _port_is_free(profile.port):
        raise CromError(
            f"port {profile.port} (assigned to '{profile.ref}') is held by another "
            f"process. {_lsof_hint(profile.port)}"
        )


@dataclass(frozen=True)
class _StderrSink:
    """Where a starting Chrome's stderr goes, and the separate handle we read it back by.

    A file, not a pipe, because Chrome outlives crom. Nothing drains a pipe after `up`
    exits, so a browser that fills the OS buffer blocks on write and never opens CDP —
    measured, a child that printed 200KB and stayed alive was still unreachable three
    seconds later through an undrained `PIPE`, and reached us in 0.02s through a file. A
    file has no buffer to fill and needs no reader to survive.

    `handle` is the child's end and `path` the reader's, and they are two open file
    descriptions on one inode by design rather than one shared between us. `fork`+`dup2`
    hands the child our file *offset* as well as our file, so seeking `handle` in order to
    read it rewinds where Chrome writes next: measured, a child printing `AAAA` then
    `BBBB` around a parent `seek(0)` leaves a file containing only `BBBB`. Reading through
    a second descriptor keeps our seeks ours.

    That sharing is also what makes Chrome's own fan-out harmless. The zygote, the GPU
    process and every renderer inherit fd 2 by `fork`, so they hold the same description
    and advance the same offset; concurrent writes queue behind it rather than racing to
    compute a position. Measured on this platform: six processes writing 12,000 lines
    through one inherited description lost none and interleaved none, and the same holds
    for 64KB writes. `O_APPEND` guards the opposite topology — several *separate*
    descriptions appending to one file — which nothing here creates.
    """

    handle: IO[bytes]
    path: Path

    def tail(self) -> str:
        """The last `STDERR_TAIL_BYTES` of what Chrome has said so far, safe to print.

        `printable` does the rendering, so what Chrome writes and what a stranger on the
        CDP port writes are made safe by one piece of code rather than two that could
        drift apart. [LAW:single-enforcer]
        """
        try:
            with open(self.path, "rb") as reader:
                reader.seek(0, os.SEEK_END)
                reader.seek(max(0, reader.tell() - STDERR_TAIL_BYTES))
                raw = reader.read().decode("utf-8", "replace")
        except OSError as e:
            # Said, not swallowed. `""` already means "Chrome printed nothing", so
            # returning it here would collapse two different facts into one value the
            # message cannot tell apart. [LAW:parse-dont-validate] Reached on a
            # successful launch too, where a raw error would crash a browser that came
            # up fine over a diagnostic nobody needed.
            return f"(crom could not read what Chrome printed: {e})"
        return printable(raw)

    def quoted(self) -> str:
        """Chrome's own words as a message section, or nothing when it said nothing.

        Both arms return a section, so callers interpolate it unconditionally rather than
        branching on whether Chrome spoke. [LAW:dataflow-not-control-flow] the emptiness
        is a value the message absorbs, not a case the message has to know about.

        The tail is capped and the file is not, so the section names the file it was read
        from: the quotation answers "what happened", the path answers "and what else did
        it say". A silent Chrome gets neither, because an empty file is nothing to find.
        """
        transcript = self.tail()
        if not transcript:
            return ""
        return (
            f"\nChrome said:\n{textwrap.indent(transcript, '    ')}"
            f"\nIts full output is at {self.path}"
        )


@contextmanager
def _stderr_sink(profile_dir: Path) -> Iterator[_StderrSink]:
    """Lend Chrome a stderr sink that outlives the launch under a name someone can find.

    This file used to be a `NamedTemporaryFile` unlinked on the way out, which left a
    successful launch writing into an inode with no name — invisible to `du` and `find`
    until the browser died and took it with it. The space is the same either way; what
    changed is that an operator can now see whose it is, and read it. It doubles as the
    answer to "what did my Chrome say", which previously existed only inside a failed
    launch's error message.

    Truncated per launch rather than appended to, and living inside the user-data-dir
    rather than beside it, because both make the lifetime someone else already owns the
    right one: the file holds what *this* browser said, and `rm` deletes it with the
    profile it belongs to while `down` leaves it for the post-mortem. Neither needs a
    cleanup rule of its own. [LAW:single-enforcer] one place deletes a profile's data.

    Unbounded within a launch, and deliberately: bounding it needs a live process to
    truncate or rotate, and crom exits about a second after the browser starts. Measured
    against a real headless Chrome: 5.3KB after four minutes on crom's default flags,
    essentially all of it during startup, against 676KB in ninety seconds under
    `--enable-logging=stderr --v=1`. The driver is activity, not uptime. Visible and
    unbounded is the trade — an operator can act on a large file they can see, and
    cannot act on a browser's output crom threw away.
    """
    path = profile_dir / STDERR_FILENAME
    try:
        handle = path.open("wb")
    except OSError as e:
        # `CromGroup.invoke` handles `CromError` alone, so a raw OSError would leave
        # `launch` as a traceback and bypass the exit-code contract. Translated here for
        # the same reason `Popen` and `ps` are. [LAW:no-silent-failure]
        raise CromError(f"could not open {path} to capture Chrome's output: {e}") from e

    with handle:
        yield _StderrSink(handle=handle, path=path)


def _await_startup(proc: subprocess.Popen[bytes], port: int) -> _LaunchOutcome:
    """Watch a starting Chrome until the port answers, the child dies, or time runs out.

    Liveness is sampled *before* the port, which buys a dying child one more probe: the
    exit code read at the top of a round is from before that round's probe, so a child
    that dies while the port is being asked still reads as alive here and the port is
    asked again before the launch is called dead. That is not a spare round — Chrome's
    own launcher hands the browser off and exits, and the browser it leaves behind is
    what answers on the round after. (An answer outranks an exit within a single round
    whichever way round the samples are taken, because the match reads the port's answer
    first. The sample order decides only how many rounds the port gets.)
    [LAW:no-ambient-temporal-coupling] the ordering is the mechanism, not a timing bet —
    no sleep tunes it and no retry papers over it.

    A stranger ends the wait as soon as it is seen. Waiting it out would buy nothing: the
    listener holding the port is why Chrome cannot have it, and it will not yield inside
    the deadline. Only `_Silent` is worth another round, because only silence is a state
    a starting browser passes through.
    """
    deadline = time.time() + LAUNCH_TIMEOUT_SECONDS
    while True:
        returncode = proc.poll()
        answer = _probe_port(port)
        match answer:
            case _Answered() | _AnsweredByStranger():
                return answer
            case _Silent():
                pass
        if returncode is not None:
            return _Exited(returncode)
        if time.time() >= deadline:
            return _NeverAnswered()
        time.sleep(0.1)


def launch(profile: ResolvedProfile) -> tuple[int, ...]:
    """Start Chrome for this profile and return its PIDs once CDP answers.

    [LAW:no-silent-failure] we wait for the endpoint we promised the caller and raise if
    it never comes up, rather than returning a port nothing is listening on — or one that
    something else is listening on, which used to read as success.

    The three ways that can go wrong get three messages, because they are three problems
    with three different next steps: read what the browser printed and fix the binary,
    look at the flags a browser that is still running never got past, or find out who took
    the port. A reader can tell which happened from the message alone, without going back
    to the machine to look. Every one of them also carries Chrome's own account of itself,
    which is usually the only line a user can act on directly.
    """
    _require_port_available(profile)
    profile.profile_dir.mkdir(parents=True, exist_ok=True)
    command = " ".join(profile.argv)

    with _stderr_sink(profile.profile_dir) as sink:
        try:
            proc = subprocess.Popen(
                profile.argv,
                env={**os.environ, **profile.env},
                stdout=subprocess.DEVNULL,
                stderr=sink.handle,
                start_new_session=True,
            )
        except OSError as e:
            # `config` checks an explicit `chrome_binary` at parse time, so reaching here
            # means the binary moved or lost its permissions between then and now. Raised
            # as a CromError so it lands inside the CLI's exit-code contract rather than
            # escaping as a raw traceback. [LAW:no-silent-failure]
            raise CromError(
                f"could not start Chrome for '{profile.ref}': {e}\nCommand was: {command}"
            ) from e

        outcome = _await_startup(proc, profile.port)
        # Read unconditionally: one operation on every launch, and what varies is the text
        # it returns. [LAW:dataflow-not-control-flow]
        said = sink.quoted()

    match outcome:
        case _Answered():
            return find_pids(profile)
        case _Exited(returncode):
            raise CromError(
                f"Chrome for '{profile.ref}' exited {returncode} during startup, before "
                f"it opened CDP port {profile.port}.{said}\nCommand was: {command}"
            )
        case _NeverAnswered():
            raise CromError(
                f"Chrome for '{profile.ref}' is still running but did not open CDP port "
                f"{profile.port} within {LAUNCH_TIMEOUT_SECONDS:.0f}s.{said}\n"
                f"Command was: {command}"
            )
        case _AnsweredByStranger(served):
            raise CromError(
                f"port {profile.port} (assigned to '{profile.ref}') is answering, but not "
                f"as a Chrome DevTools endpoint — it served: {served}. Something else took "
                f"the port while Chrome was starting, so the browser crom just launched "
                f"cannot be reached there.\n{_lsof_hint(profile.port)}{said}\n"
                f"Command was: {command}"
            )


def _still_held(profile: ResolvedProfile) -> tuple[str, ...]:
    """Whatever still contradicts "this profile is stopped", named for whoever must chase it.

    Being stopped has two halves — the profile runs no process, and it holds no port — and
    they end independently. A listening socket belongs to the file description, not to the
    process that opened it, so the CDP port stays bound for as long as *anything* holds a
    copy of it; the browser leaving the process table is not by itself the port coming
    back. That gap is what a relaunch falls into: it binds against a socket whose owner is
    already dead, or reaches CDP and is answered on behalf of a corpse.

    Both halves are read in one place so that the wait and the failure message cannot come
    to disagree about what stopped means. [LAW:one-source-of-truth] An empty tuple is the
    whole promise kept, which is the only condition `kill` returns on.

    The lines are worded for the one place they are ever printed — the error `kill` raises
    once escalation is spent — which is why the process line may speak of SIGKILL. Every
    earlier round reads these only for emptiness.
    """
    pids = find_pids(profile)
    listed = ", ".join(map(str, pids))
    return tuple(
        detail
        for detail, unmet in (
            (
                f"pid(s) {listed} are still running — SIGTERM then SIGKILL did not end "
                f"them. Inspect them with: ps -p {listed}",
                bool(pids),
            ),
            (
                f"port {profile.port} is still held. {_lsof_hint(profile.port)}",
                not _port_is_free(profile.port),
            ),
        )
        if unmet
    )


def _await_release(profile: ResolvedProfile) -> tuple[str, ...]:
    """Wait up to the shutdown timeout for the profile to give up process and port alike."""
    deadline = time.time() + SHUTDOWN_TIMEOUT_SECONDS
    while time.time() < deadline and _still_held(profile):
        time.sleep(0.1)
    return _still_held(profile)


def kill(profile: ResolvedProfile) -> tuple[int, ...]:
    """Stop every main Chrome bound to this profile, returning only once they are gone.

    The postcondition is the whole point. `rm` deletes the profile's user-data-dir the
    instant this returns, and "the signals were sent" is not a strong enough promise to
    delete a directory on: `os.kill` returns before the kernel has finished tearing the
    process down, and this used to return inside that window. `shutil.rmtree` walking a
    directory Chrome is still writing raises `FileNotFoundError` when an entry vanishes
    mid-walk or `ENOTEMPTY` when one appears — neither a `CromError`, so it escaped the
    CLI's exit-code contract as a traceback, after `rm` had already undeclared the
    profile. [LAW:no-ambient-temporal-coupling] a transition `rm` depends on is owned
    here rather than assumed to have completed by the time the caller looks.

    The port is half of that postcondition, and the half nobody owned. `up` after a `down`
    on the same port — which is all a restart is — races whatever still holds the CDP
    socket, and loses in one of two ways: it cannot bind and blames a foreign process, or
    the readiness probe is answered by the browser that is on its way out and it reports a
    live endpoint belonging to a corpse. Waiting on `_still_held` rather than on the
    process table alone is what makes "stopped" mean "relaunchable".

    Escalation is a table walked the same way each round — signal whatever is still
    alive, then wait for it — rather than a graceful path and a separate forced one.
    [LAW:dataflow-not-control-flow] A profile that was never running signals nothing and
    returns `()` as soon as its port answers free, which is what makes `rm` and `down`
    safe to call unconditionally.

    The uniform rule has a price, taken deliberately: a stopped profile whose port some
    unrelated program happens to be sitting on is reported as a stop that could not be
    established, rather than as "was not running". Nothing here can tell that program
    apart from a socket the browser left behind — both are simply a port that will not
    bind — and of the two available lies, promising a relaunchable profile that is not one
    is the one that costs a caller its next command. Saying so costs both rounds, since a
    profile with nothing to signal has nothing to escalate — a bounded 10s on a stop that
    has already failed, against the 30s `launch` spends before reporting its own failure.
    [LAW:no-silent-failure]

    Residual, and deliberately not chased: `find_pids` matches only the main browser and
    skips Chrome's `--type=` helpers, so a crashpad handler can briefly outlive this.
    Extending the wait to helpers would widen `scan`'s contract for a window that closes
    on its own; `cli._delete_profile_data` covers what remains by reporting a failed
    delete as a retryable `CromError` rather than a traceback.
    """
    pids = find_pids(profile)
    survivors = pids
    unmet: tuple[str, ...] = ()
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in survivors:
            _signal(pid, sig)
        unmet = _await_release(profile)
        if not unmet:
            return pids
        # Re-read only between rounds. Rescanning before the first round would spend a
        # second `ps` to re-learn what `pids` already holds, and open a window in which a
        # process that appeared meanwhile gets signalled but is not in the returned set.
        survivors = find_pids(profile)

    # [LAW:no-silent-failure] Returning here would report a running browser as stopped —
    # to `down`, which would print "Stopped", and to `rm`, which would go on to delete a
    # live browser's directory.
    #
    # The message carries the observations `_await_release` ended on, so it names the half
    # that failed instead of the half this code happens to check last — a port still held
    # under a process table that is already empty reads as exactly that.
    #
    # It says nothing about deleting: `rm` aborting before its delete is `rm`'s business,
    # and a module that names one caller's next step is coupled to that caller.
    # [LAW:composability]
    # The header states only what is true in both arms. A profile that was never running
    # but whose port will not come free was signalled nothing, and saying "SIGKILL was
    # sent" there sends its reader looking for a browser that does not exist; what
    # escalation was spent belongs on the line that only appears when there was something
    # to spend it on.
    detail = "\n".join(f"  - {line}" for line in unmet)
    raise CromError(f"could not stop '{profile.ref}':\n{detail}")


def _signal(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        # The process exited between our scan and our signal — the outcome we wanted.
        pass
