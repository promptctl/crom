"""Brings the window of one crom-managed browser to the front.

Separate from `chrome`, whose job is the process — start it, find it, stop it. Which
window the window server shows is a different question about a different subsystem, and
the two meet only at a PID. [LAW:decomposition]

Why crom does this at all, rather than leaving it to the user: every crom-managed Chrome
is the same application bundle, so the ordinary `tell application "Google Chrome" to
activate` cannot pick between them — it raises whichever instance the window server
prefers, which on a machine running four project browsers is a coin toss. A PID names
exactly one of them however many are running, and System Events is the only interface
that accepts one.

macOS only, and unlike the rest of crom that is not incidental. `chrome.scan` shells out
to `ps` because it is portable; there is no portable spelling of "raise this window", so a
machine with no `osascript` is told so rather than left watching a command exit 0 having
done nothing. [LAW:no-silent-failure]
"""

import subprocess

from .model import CromError, ResolvedProfile

# Raise the process, then report how many windows it has.
#
# The `whose` specifier is written out twice on purpose, and the obvious tidy-up breaks
# it: bind it once (`set target to first process whose unix id is pid`) and reuse the
# variable, and `count of windows` answers 0 for a browser that demonstrably has one.
# Measured against Chrome/152 on macOS 25.3 — a real window counts as 1 through the
# inlined specifier and 0 through the variable, both at exit 0. The duplication is the
# spelling that works; the deduplicated version fails silently, which is the worst way for
# a measurement to be wrong.
#
# The PID crosses as an `argv` item rather than being interpolated into the script text,
# so the script crom runs is a fixed string that no value can extend.
_RAISE_SCRIPT = """on run argv
  set pid to (item 1 of argv) as integer
  tell application "System Events"
    set frontmost of (first process whose unix id is pid) to true
    return count of windows of (first process whose unix id is pid)
  end tell
end run"""

# What crom can add to osascript's own account of a failure, keyed by the error number
# osascript prints. Parenthesised because that is how it renders them — `Invalid index.
# (-1719)` — and matching the bare digits would hit a pid that happened to contain them.
#
# A table rather than arms, so an ending crom has nothing to add to flows through as an
# empty remedy and still carries what osascript said. [LAW:dataflow-not-control-flow]
_REMEDIES: tuple[tuple[str, str], ...] = (
    (
        "(-1743)",
        "macOS is withholding Automation access from whatever ran crom. Grant it in\n"
        "System Settings › Privacy & Security › Automation: find your terminal (or the\n"
        "agent that invoked crom) and enable 'System Events' beneath it. Nothing crom\n"
        "can do reaches a window until that box is ticked.",
    ),
    (
        "(-1719)",
        "No window-server process is running under that pid, so the browser most likely\n"
        "exited between crom finding it and raising it. `crom up` will start it again.",
    ),
)


def raise_profile(profile: ResolvedProfile, pids: tuple[int, ...]) -> int:
    """Bring this profile's browsers to the front; report how many windows they hold.

    Takes the PIDs rather than reading them, so the caller raises the same processes it
    observed — `show` reads them inside the profile lock and a re-read here would be
    outside it. [LAW:no-ambient-temporal-coupling]

    The window count is the return value because "frontmost" and "visible" are not the
    same fact: a browser running headless, or one whose last window was closed, is raised
    successfully and shows the user nothing. Reporting zero lets `show` say so instead of
    claiming a window that is not there. [LAW:no-silent-failure]
    """
    return sum(_raise_one(profile, pid) for pid in pids)


def _raise_one(profile: ResolvedProfile, pid: int) -> int:
    try:
        result = subprocess.run(
            ["osascript", "-e", _RAISE_SCRIPT, str(pid)],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as e:
        raise CromError(
            "could not raise a window: `osascript` was not found on PATH.\n"
            "Raising one browser out of several identical ones needs macOS's own window "
            "server, so `crom show` works on macOS alone."
        ) from e

    if result.returncode != 0:
        said = result.stderr.strip()
        remedy = next((text for code, text in _REMEDIES if code in said), "")
        raise CromError(
            "\n".join(filter(None, (f"could not raise '{profile.ref}' (pid {pid}): {said}", remedy)))
        )

    # osascript answered, so the number it printed is the only evidence of what was
    # raised. Parsed rather than trusted: a build that prints something else here would
    # otherwise crash with a bare ValueError outside the CLI's exit-code contract, and the
    # traceback would accuse crom of a bug in arithmetic. [LAW:parse-dont-validate]
    counted = result.stdout.strip()
    try:
        return int(counted)
    except ValueError as e:
        raise CromError(
            f"raised '{profile.ref}' (pid {pid}), but osascript reported its window count "
            f"as {counted!r}, which is not a number."
        ) from e
