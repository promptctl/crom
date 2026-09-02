"""Starts, finds, and stops the Chrome processes behind resolved profiles.

[LAW:one-source-of-truth] The OS process table is the sole authority on "is this profile
running." We identify a crom-managed Chrome by the absolute `--user-data-dir` path it
was launched with — no pidfiles, no shadow state that can drift from reality.

Everything here takes a `ResolvedProfile`, whose `argv` is already complete, so this
module never reads a config file or decides a port.
"""

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


def _cdp_ready(port: int) -> bool:
    """True once Chrome's CDP HTTP endpoint answers on this port.

    We probe the endpoint we intend to use rather than Chrome's DevToolsActivePort file:
    Chrome only writes that file to *report* a port it chose itself (the
    `--remote-debugging-port=0` case), not when we hand it a fixed port. The live
    endpoint is the honest readiness signal.
    """
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


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
        f"Find it with: lsof -nP -iTCP:{profile.port} -sTCP:LISTEN"
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
    """

    handle: IO[bytes]
    path: Path

    def tail(self) -> str:
        """The last `STDERR_TAIL_BYTES` of what Chrome has said so far.

        Decoded leniently: this is diagnostic text on its way into an error message, and a
        malformed byte in Chrome's output is not a reason to fail while reporting a
        failure. [LAW:no-silent-failure] would be inverted by raising here.
        """
        with open(self.path, "rb") as reader:
            reader.seek(0, os.SEEK_END)
            reader.seek(max(0, reader.tell() - STDERR_TAIL_BYTES))
            return reader.read().decode("utf-8", errors="replace").strip()


@contextmanager
def _stderr_sink() -> Iterator[_StderrSink]:
    """Lend Chrome a stderr sink for the launch window that leaves nothing behind.

    The file is unlinked on the way out while Chrome still holds it open. A launch that
    succeeded goes on writing into an inode with no name — no artifact to find, none to
    clean up, and the space reclaimed in full when the browser exits. So capture costs a
    successful launch exactly what `DEVNULL` did: nothing anyone can see.
    """
    fd, name = tempfile.mkstemp(prefix="crom-chrome-stderr-")
    path = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            yield _StderrSink(handle=handle, path=path)
    finally:
        path.unlink()


def _quote(transcript: str) -> str:
    """Render Chrome's own words as a message section, or nothing when it said nothing.

    Both arms return a section, so callers interpolate it unconditionally rather than
    branching on whether Chrome spoke. [LAW:dataflow-not-control-flow] the emptiness is a
    value the message absorbs, not a case the message has to know about.
    """
    if not transcript:
        return ""
    return f"\nChrome said:\n{textwrap.indent(transcript, '    ')}"


@dataclass(frozen=True)
class _Answered:
    """CDP replied on the port we asked for. The launch succeeded."""


@dataclass(frozen=True)
class _Exited:
    """The child process was gone before CDP ever replied."""

    returncode: int


@dataclass(frozen=True)
class _NeverAnswered:
    """The deadline passed with the child still alive and the port still silent."""


# [LAW:types-are-the-program] The wait has three real endings, so it resolves to a value
# with three shapes. Before this union it returned pids or raised a timeout — two shapes
# for three endings, and the ending with nowhere to go was the common one: a Chrome that
# died in 20ms got reported, 30 seconds later, as a port that had not opened yet.
_LaunchOutcome = _Answered | _Exited | _NeverAnswered


def _await_startup(proc: subprocess.Popen[bytes], port: int) -> _LaunchOutcome:
    """Watch a starting Chrome until it serves CDP, dies, or runs out of time.

    Liveness is sampled *before* readiness so that readiness gets the last word within a
    round: a child observed alive and then found to have exited still counts as answered
    if the port replied in between. The two observations cannot be simultaneous, and this
    is the order in which their disagreement resolves toward the browser that is actually
    reachable. [LAW:no-ambient-temporal-coupling] the ordering is the mechanism, not a
    timing bet — no sleep tunes it and no retry papers over it.
    """
    deadline = time.time() + LAUNCH_TIMEOUT_SECONDS
    while True:
        returncode = proc.poll()
        if _cdp_ready(port):
            return _Answered()
        if returncode is not None:
            return _Exited(returncode)
        if time.time() >= deadline:
            return _NeverAnswered()
        time.sleep(0.1)


def launch(profile: ResolvedProfile) -> tuple[int, ...]:
    """Start Chrome for this profile and return its PIDs once CDP answers.

    [LAW:no-silent-failure] we wait for the endpoint we promised the caller and raise if
    it never comes up, rather than returning a port nothing is listening on. A browser
    that died and a browser that is merely slow fail with different messages, because
    they are different problems and only one of them is worth waiting the full timeout
    for — and either way the message carries Chrome's own account of itself, which is
    usually the only line in it a user can act on.
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
                f"Chrome did not open CDP port {profile.port} for '{profile.ref}' within "
                f"{LAUNCH_TIMEOUT_SECONDS:.0f}s.{said}\nCommand was: {command}"
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
