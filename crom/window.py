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

from .model import Reason, ResolvedProfile

# How long crom waits for `osascript` to answer. A raise costs milliseconds once macOS has
# granted Automation access, so this bound is not sized for the work — it is sized for the
# consent dialog. The first automation call from a new program can sit on a modal TCC
# prompt, and `show` holds `seed.profile_lock` across this call, so an unbounded wait there
# gates every other `up`, `down`, `restart` and `rm` on that profile behind a dialog nobody
# may be looking at. Generous enough that a user who *is* looking at it can answer — that
# click is the legitimate way to grant access, and timing it out would refuse the fix.
# [LAW:no-ambient-temporal-coupling] the wait has a stated ceiling rather than a hope.
RAISE_TIMEOUT_SECONDS = 30.0

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
_REMEDIES: tuple[tuple[str, str, Reason], ...] = (
    (
        "(-1743)",
        "macOS is withholding Automation access from whatever ran crom. Grant it in\n"
        "System Settings › Privacy & Security › Automation: find your terminal (or the\n"
        "agent that invoked crom) and enable 'System Events' beneath it. Nothing crom\n"
        "can do reaches a window until that box is ticked.",
        # The same refusal the `TimeoutExpired` arm below infers from an unanswered
        # consent dialog, except here macOS states it outright. The definite case must not
        # answer with a vaguer slug than the inferred one.
        Reason.AUTOMATION_DENIED,
    ),
    (
        "(-1719)",
        "No window-server process is running under that pid, so the browser most likely\n"
        "exited between crom finding it and raising it. `crom up` will start it again.",
        Reason.WINDOW_RAISE_FAILED,
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
            timeout=RAISE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        raise Reason.AUTOMATION_DENIED.error(
            f"`osascript` did not answer within {RAISE_TIMEOUT_SECONDS:.0f}s while raising "
            f"'{profile.ref}' (pid {pid}).\nThe usual cause is macOS's Automation consent "
            f"dialog waiting on an answer — grant access in System Settings › Privacy & "
            f"Security › Automation, or answer the prompt if it is on screen."
        ) from e
    except OSError as e:
        # `OSError` rather than `FileNotFoundError` alone: a present-but-unexecutable
        # `osascript` raises `PermissionError`, which is no less a reason this cannot work
        # and no less in need of a message that names `osascript`. `chrome.launch`
        # catches the same breadth around its own `Popen` for that reason.
        # One message carrying the error rather than an arm per cause: the sentence below
        # is true of every way the command can fail to run. [LAW:dataflow-not-control-flow]
        # The slug is where they part, which is what the field is for — a macOS box whose
        # `osascript` will not run is not a platform without one.
        #
        # On the class and not on `e.errno`, which is the opposite of the `EPIPE` rule at
        # the CLI boundary and for the reason that rule gives: spell the fact the way its
        # owner spells it. There the class was *wider* than the errno, so the errno was
        # the precise key; `FileNotFoundError` is exactly Python's name for `ENOENT`, and
        # it survives the errno-less construction that both real code and this file's own
        # stubs produce — measured: `subprocess.run` on a missing binary carries errno 2,
        # `FileNotFoundError("osascript")` carries None.
        missing = isinstance(e, FileNotFoundError)
        raise (Reason.PLATFORM_UNSUPPORTED if missing else Reason.WINDOW_RAISE_FAILED).error(
            f"could not run `osascript` to raise a window: {e}\n"
            "Raising one browser out of several identical ones needs macOS's own window "
            "server, so `crom show` works on macOS alone."
        ) from e

    if result.returncode != 0:
        said = result.stderr.strip()
        remedy, reason = next(
            ((text, why) for code, text, why in _REMEDIES if code in said),
            ("", Reason.WINDOW_RAISE_FAILED),
        )
        # The status is carried even when osascript said nothing: killed by a signal it
        # exits non-zero with empty stderr, and the sentence would otherwise stop at the
        # colon, naming a failure while withholding every fact about it.
        # [LAW:no-silent-failure]
        detail = " ".join(filter(None, (f"exit {result.returncode}.", said)))
        problem = f"could not raise '{profile.ref}' (pid {pid}): {detail}"
        raise reason.error("\n".join(filter(None, (problem, remedy))))

    # osascript answered, so the number it printed is the only evidence of what was
    # raised. Parsed rather than trusted: a build that prints something else here would
    # otherwise crash with a bare ValueError outside the CLI's exit-code contract, and the
    # traceback would accuse crom of a bug in arithmetic. [LAW:parse-dont-validate]
    counted = result.stdout.strip()
    try:
        return int(counted)
    except ValueError as e:
        raise Reason.WINDOW_RAISE_FAILED.error(
            f"raised '{profile.ref}' (pid {pid}), but osascript reported its window count "
            f"as {counted!r}, which is not a number."
        ) from e
