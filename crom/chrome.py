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
import time
import urllib.error
import urllib.request
from pathlib import Path

from .model import CromError, ResolvedProfile

LAUNCH_TIMEOUT_SECONDS = 30.0
SHUTDOWN_TIMEOUT_SECONDS = 5.0


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
# whose value contains that literal text cannot shadow the real one (`parse_flags`
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


def launch(profile: ResolvedProfile) -> tuple[int, ...]:
    """Start Chrome for this profile and return its PIDs once CDP answers.

    [LAW:no-silent-failure] we wait for the endpoint we promised the caller and raise if
    it never comes up, rather than returning a port nothing is listening on.
    """
    _require_port_available(profile)
    profile.profile_dir.mkdir(parents=True, exist_ok=True)

    try:
        subprocess.Popen(
            profile.argv,
            env={**os.environ, **profile.env},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        # `config` checks an explicit `chrome_binary` at parse time, so reaching here
        # means the binary moved or lost its permissions between then and now. Raised as
        # a CromError so it lands inside the CLI's exit-code contract rather than
        # escaping as a raw traceback. [LAW:no-silent-failure]
        raise CromError(
            f"could not start Chrome for '{profile.ref}': {e}\n"
            f"Command was: {' '.join(profile.argv)}"
        ) from e

    deadline = time.time() + LAUNCH_TIMEOUT_SECONDS
    while time.time() < deadline:
        if _cdp_ready(profile.port):
            return find_pids(profile)
        time.sleep(0.1)
    raise CromError(
        f"Chrome did not open CDP port {profile.port} for '{profile.ref}' within "
        f"{LAUNCH_TIMEOUT_SECONDS:.0f}s.\nCommand was: {' '.join(profile.argv)}"
    )


def kill(profile: ResolvedProfile) -> tuple[int, ...]:
    """Terminate every main Chrome process bound to this profile; return what we killed."""
    pids = find_pids(profile)
    for pid in pids:
        _signal(pid, signal.SIGTERM)

    # Give Chrome a moment to shut down gracefully, then SIGKILL stragglers.
    deadline = time.time() + SHUTDOWN_TIMEOUT_SECONDS
    while time.time() < deadline and find_pids(profile):
        time.sleep(0.1)
    for pid in find_pids(profile):
        _signal(pid, signal.SIGKILL)
    return pids


def _signal(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        # The process exited between our scan and our signal — the outcome we wanted.
        pass
