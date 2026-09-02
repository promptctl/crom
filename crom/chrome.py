"""Starts, finds, and stops the Chrome processes behind resolved profiles.

[LAW:one-source-of-truth] The OS process table is the sole authority on "is this profile
running." We identify a crom-managed Chrome by the absolute `--user-data-dir` path it
was launched with — no pidfiles, no shadow state that can drift from reality.

Everything here takes a `ResolvedProfile`, whose `argv` is already complete, so this
module never reads a config file or decides a port.
"""

import http.client
import json
import os
import re
import signal
import socket
import subprocess
import tempfile
import textwrap
import time
import urllib.error
import urllib.request
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

# How long crom will spend reading whatever answers on the CDP port. This is the bound
# that matters: `urlopen`'s timeout applies to each `recv`, never to the call, so a
# listener trickling bytes just under it holds a read open for hours without once
# tripping it — and `_await_startup` can only check its own deadline between probes.
PORT_REPLY_SECONDS = 2.0

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


def _printable(text: str) -> str:
    """Text crom did not write, rendered so a terminal will only ever display it.

    One job, done once wherever foreign text enters a message: Chrome's stderr, and
    whatever answers on the CDP port. Downstream then holds a printable string by
    construction rather than by discipline. [LAW:parse-dont-validate]

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

    Every arm of `_probe_port` that quotes its stranger comes through here — the reply
    body, an HTTP reason phrase, the offending line inside an `HTTPException`. All three
    are chosen by whatever holds the port and all three land in an error headed for a
    terminal, so they are one job rather than three call sites that each have to remember.
    An arm added later reaches the sanitising by using this; one was already forgotten.
    """
    return textwrap.shorten(_printable(text), PORT_REPLY_SUMMARY_CHARS, placeholder=" …")


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


def _read_reply(resp: http.client.HTTPResponse) -> bytes:
    """Whatever the port is willing to say, within a bounded time and a bounded size.

    Three endings — enough bytes, end of the reply, or the clock — and the clock is the
    one that had to exist. `resp.read(n)` blocks until it holds all n bytes, and the
    socket timeout bounds each `recv` rather than the call, so a listener trickling one
    byte just under that timeout keeps every individual read legal while the total runs to
    hours. `_await_startup` cannot see it, because it checks its deadline between probes
    and this is inside one. Bounding bytes never bounded that; only a clock does.
    [LAW:no-ambient-temporal-coupling] how long this may take is owned here rather than
    left to whatever is on the far end.
    """
    deadline = time.monotonic() + PORT_REPLY_SECONDS
    reply = b""
    while len(reply) < PORT_REPLY_BYTES and time.monotonic() < deadline:
        chunk = resp.read1(PORT_REPLY_BYTES - len(reply))
        if not chunk:
            break
        reply += chunk
    return reply


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

    An HTTP status that is not 200 lands with the stranger rather than with silence, since
    something is speaking HTTP there and it is not serving CDP. Measured against
    Chrome/152 on macOS, that costs no real launch: sampling this endpoint every 5ms
    across four startups yielded exactly two observations each time — connection refused,
    then 200 with the version document — and never an intermediate reply. There is no
    window in which a starting Chrome looks like a stranger.

    What this cannot tell apart is a *different* Chrome on our port, which advertises the
    same shape. `_require_port_available` rejects a port already held before launch, so
    what remains is a listener that appeared inside the wait window, where the realistic
    stranger is not a browser.
    """
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as resp:
            reply = _read_reply(resp)
    except urllib.error.HTTPError as e:
        return _AnsweredByStranger(f"HTTP {e.code} {_summarise(str(e.reason))}")
    except (urllib.error.URLError, OSError):
        # A half-delivered response reads the same as nothing here, and calling it silence
        # costs only another 100ms round. Reached far more often by the ordinary case:
        # the port is refusing connections because Chrome has not opened it yet.
        return _Silent()
    except http.client.HTTPException as e:
        # Something accepted the connection and then said what HTTP could not parse, which
        # is a stranger rather than silence — and it is how a service that does not speak
        # HTTP at all shows up, the very case this outcome exists for. Below the `OSError`
        # arm deliberately: `RemoteDisconnected` is both a `ConnectionResetError` and a
        # `BadStatusLine`, and a connection that closed without saying anything is silence.
        return _AnsweredByStranger(_summarise(f"{type(e).__name__}: {e}"))

    if _advertises_devtools(reply):
        return _Answered()
    return _AnsweredByStranger(_summarise(reply.decode("utf-8", "replace")) or "an empty reply")


def _require_port_available(profile: ResolvedProfile) -> None:
    """Fail immediately, and by name, when something else already holds the port.

    Without this the launch simply times out after 30s and blames the wrong thing.
    [LAW:no-silent-failure] the diagnosis belongs at the moment of failure.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", profile.port))
            return
        except OSError:
            pass
    raise CromError(
        f"port {profile.port} (assigned to '{profile.ref}') is held by another process. "
        f"{_lsof_hint(profile.port)}"
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

        `_printable` does the rendering, so what Chrome writes and what a stranger on the
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
        return _printable(raw)


@contextmanager
def _stderr_sink() -> Iterator[_StderrSink]:
    """Lend Chrome a stderr sink for the launch window that leaves nothing behind.

    The file is unlinked on the way out while Chrome still holds it open, so a launch
    that succeeded goes on writing into an inode with no name: nothing to find, nothing
    to clean up, and the space reclaimed in full when the browser exits.

    Unlike `DEVNULL` that is not free, and the cost is bounded by nothing crom holds —
    it exits about a second after the browser starts, so there is no process left to
    truncate or rotate anything. Measured against a real headless Chrome: 5.3KB after
    four minutes on crom's default flags, essentially all of it during startup. Under
    `--enable-logging=stderr --v=1` it is 676KB in ninety seconds. The driver is
    activity, not uptime, so a busy profile configured for verbose logging can hold a
    large invisible file for as long as it runs. Bounding it needs a sink someone is
    still watching, which is a different design than this one.
    """
    try:
        # Creating and opening in one call is what keeps the guard honest: a separate
        # `mkstemp` + `os.fdopen` needs two, and a failure between them leaks the
        # descriptor — in the one situation, exhaustion, where that is least affordable.
        handle = tempfile.NamedTemporaryFile(prefix="crom-chrome-stderr-", delete=False)
    except OSError as e:
        # `CromGroup.invoke` handles `CromError` alone, so a raw OSError would leave
        # `launch` as a traceback and bypass the exit-code contract. Translated here for
        # the same reason `Popen` and `ps` are. [LAW:no-silent-failure]
        raise CromError(f"could not open a temporary file to capture Chrome's output: {e}") from e

    path = Path(handle.name)
    try:
        with handle:
            yield _StderrSink(handle=handle, path=path)
    finally:
        # `missing_ok` because a tmp-cleaner having got there first is not a failure:
        # the state unlink exists to establish already holds. Anything else — a temp
        # filesystem that went read-only mid-launch — stays loud, even though it costs
        # the error in flight, because that is the browser's filesystem breaking and not
        # diagnostics plumbing. [LAW:no-silent-failure]
        path.unlink(missing_ok=True)


def _quote(transcript: str) -> str:
    """Render Chrome's own words as a message section, or nothing when it said nothing.

    Both arms return a section, so callers interpolate it unconditionally rather than
    branching on whether Chrome spoke. [LAW:dataflow-not-control-flow] the emptiness is a
    value the message absorbs, not a case the message has to know about.
    """
    if not transcript:
        return ""
    return f"\nChrome said:\n{textwrap.indent(transcript, '    ')}"


def _await_startup(proc: subprocess.Popen[bytes], port: int) -> _LaunchOutcome:
    """Watch a starting Chrome until the port answers, the child dies, or time runs out.

    Liveness is sampled *before* the port so that the port gets the last word within a
    round: a child observed alive and then found to have exited still counts as answered
    if the port replied in between. The two observations cannot be simultaneous, and this
    is the order in which their disagreement resolves toward the browser that is actually
    reachable. [LAW:no-ambient-temporal-coupling] the ordering is the mechanism, not a
    timing bet — no sleep tunes it and no retry papers over it.

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

    with _stderr_sink() as sink:
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
        said = _quote(sink.tail())

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


def _await_exit(profile: ResolvedProfile) -> bool:
    """Wait up to the shutdown timeout for no main Chrome to hold this profile."""
    deadline = time.time() + SHUTDOWN_TIMEOUT_SECONDS
    while time.time() < deadline and find_pids(profile):
        time.sleep(0.1)
    return not find_pids(profile)


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

    Escalation is a table walked the same way each round — signal whatever is still
    alive, then wait for it — rather than a graceful path and a separate forced one.
    [LAW:dataflow-not-control-flow] A profile that was never running finds nothing to
    signal, waits on nothing, and returns `()` on the first round, which is what makes
    `rm` and `down` safe to call unconditionally.

    Residual, and deliberately not chased: `find_pids` matches only the main browser and
    skips Chrome's `--type=` helpers, so a crashpad handler can briefly outlive this.
    Extending the wait to helpers would widen `scan`'s contract for a window that closes
    on its own; `cli._delete_profile_data` covers what remains by reporting a failed
    delete as a retryable `CromError` rather than a traceback.
    """
    pids = find_pids(profile)
    survivors = pids
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in survivors:
            _signal(pid, sig)
        if _await_exit(profile):
            return pids
        # Re-read only between rounds. Rescanning before the first round would spend a
        # second `ps` to re-learn what `pids` already holds, and open a window in which a
        # process that appeared meanwhile gets signalled but is not in the returned set.
        survivors = find_pids(profile)

    # [LAW:no-silent-failure] Returning here would report a running browser as stopped —
    # to `down`, which would print "Stopped", and to `rm`, which would go on to delete a
    # live browser's directory.
    #
    # The message says nothing about deleting: `rm` aborting before its delete is `rm`'s
    # business, and a module that names one caller's next step is coupled to that caller.
    # [LAW:composability]
    remaining = ", ".join(map(str, find_pids(profile)))
    raise CromError(
        f"could not stop '{profile.ref}': pid(s) {remaining} survived SIGKILL after "
        f"{SHUTDOWN_TIMEOUT_SECONDS:.0f}s.\nInspect it with: ps -p {remaining}"
    )


def _signal(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        # The process exited between our scan and our signal — the outcome we wanted.
        pass
