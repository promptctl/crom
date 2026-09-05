"""Records how crom launched a profile's browser, so a later crom can read it back.

`chrome` answers "is this profile running" from the process table and keeps no shadow
state, because the process table cannot drift from reality. This file is not an exception
to that rule; it is where the rule runs out. The one thing the process table cannot say is
*how* a running browser was launched: Chrome re-execs itself and rewrites its own argv —
observed, and written down at `chrome._USER_DATA_DIR_RE` — so the live command line is a
map of Chrome's current invocation, not of crom's. A flag crom passed can simply not be
there. [FRAMING:representation] the only moment that fact is knowable is the moment crom
spends it, so that is where it gets written down.

`Launch.of` is the sole definition of what "how crom launched it" means, and it is meant
to supply both sides of any later comparison: what crom recorded at launch, and what the
profile's current configuration would launch now. One producer, so a comparison cannot
quietly leave out a layer that only some machines populate. [LAW:one-source-of-truth]

The record lives inside the profile's user-data-dir beside `crom-stderr.log`, and for the
same reason: its lifetime is exactly the directory's, so `crom rm` takes it with the
profile and `crom down` leaves it. Neither needs a cleanup rule of its own.
[LAW:single-enforcer] one place deletes a profile's data.
"""

import contextlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import report
from .model import ResolvedProfile

# What the record is called inside the profile's user-data-dir. Prefixed for the reason
# `chrome.STDERR_FILENAME` is: the directory is Chrome's, and an operator looking at it
# should be able to tell at a glance which files in it crom put there.
FILENAME = "crom-launch.json"

# Bumped when the shape below changes in a way an older crom would misread. A version it
# does not speak is refused rather than ignored, so this number is a promise in both
# directions rather than a note to the writer.
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Launch:
    """The two inputs that decide which browser a launch produces.

    Everything else a `ResolvedProfile` carries is either already inside `argv` — the
    binary, the port, the user-data-dir, which `resolve.build_argv` frames into it — or is
    about how crom *found* the profile rather than what it ran. `provenance` is
    deliberately absent: it says which config layer supplied each flag, which is derived
    from the config file and which a later crom re-derives from that same file, so keeping
    a copy here would be a second map of one territory. [LAW:one-source-of-truth]

    `env` sits beside `argv` because `[defaults].env` and a profile's `env` are layered
    configuration exactly as `flags` is — editing either changes the browser crom would
    launch. A record holding only the flags would be a map missing half its territory, and
    the half it left out is a config edit no later crom could notice.

    No pids and no timestamp. The process table is the sole authority on which processes
    exist (`chrome`'s own header), so a pid here would be a second, drift-prone answer to
    a question already owned; and nothing asks when the launch happened.
    """

    argv: tuple[str, ...]
    env: dict[str, str]

    @classmethod
    def of(cls, profile: ResolvedProfile) -> "Launch":
        """Read the launch out of a resolved profile — the one place this projection lives."""
        return cls(argv=tuple(profile.argv), env=dict(profile.env))


@dataclass(frozen=True)
class Unknown:
    """Why crom cannot say what the browser in a directory was launched with.

    Two ways to arrive here — nothing was ever written down, or what was written cannot be
    used — and one behavior, which is that no comparison against the profile's current
    configuration is possible. So which of the two it was is a sentence rather than a
    variant: no caller acts differently on it. [LAW:one-type-per-behavior]

    Not `None`, and not an empty `Launch`. Either would be an answer-shaped void: "this
    browser was launched with no flags" is a thing that can be true, and it is not this.
    A caller that wants an `argv` cannot get one from here without noticing.
    """

    why: str


def path(profile_dir: Path) -> Path:
    return profile_dir / FILENAME


def record(profile_dir: Path, launch: Launch, log=report.to_stderr) -> None:
    """Write down a launch that has already succeeded, or say why it could not be written.

    Reported rather than raised, and `read` takes the same stance at the other end of the
    file: a launch record is bookkeeping the next launch rebuilds, so nothing about a
    browser's life turns on it. Here the timing makes it sharper still — the browser is up
    by the time this is called, so a raise would have `launch` report a failed launch for a
    running Chrome and leave it running with nobody told it exists, trading a small loss for
    a wrong answer. [LAW:no-silent-failure] is served the way every other repair below the
    CLI serves it: on stderr, naming the file and what its absence will cost, which is
    strictly louder than the traceback it replaces.

    Replaced atomically, so an interrupted write leaves the previous record standing rather
    than a half-written one that would cost the next reader the launch it still describes.
    """
    document = {
        "version": SCHEMA_VERSION,
        "argv": list(launch.argv),
        "env": dict(launch.env),
    }
    destination = path(profile_dir)
    staging = destination.with_suffix(".json.tmp")
    try:
        staging.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
        os.replace(staging, destination)
    except OSError as e:
        log(
            f"Could not record how this browser was launched at {destination}: {e}. The "
            f"browser is running; what crom cannot now tell is whether a later run matches "
            f"the configuration it was started from."
        )
        # After the report, never instead of it — a raise from tidying up would replace a
        # loss the user was told about with one they were not. The leftover is the one
        # failure here nothing downstream can act on. [LAW:no-silent-failure]
        with contextlib.suppress(OSError):
            staging.unlink(missing_ok=True)


def read(profile_dir: Path) -> Launch | Unknown:
    """What crom launched the browser in this directory with, or why it cannot say.

    Never raises, which is a stance rather than an omission, and the same one `record`
    takes at the other end of the file: a launch record is bookkeeping a launch rebuilds,
    so nothing about a browser's life should stop over it. Refusing would hand the user a
    chore for a mess crom made — "delete this file, then run your command again" — which
    is the shape README's convergence promise exists to rule out, and it would fall
    hardest on `crom up`, the one command that would have replaced the damaged file had it
    been allowed to run.

    Deliberately not the ledger's stance. `registry._read` raises over a damaged port
    table because what is in it cannot be regenerated: rebuild it and every profile takes
    a new port. Nothing here is irreplaceable, so nothing here is worth stopping for.

    [LAW:no-silent-failure] is carried by the return type instead of by a raise. `Unknown`
    has no `argv`, so no caller can read it as a launch or drift past it into a comparison,
    and it holds a finished sentence rather than a code, because the only useful thing to
    do with it is say it.

    [LAW:parse-dont-validate] the checks below are a border, not inland guards: this is the
    only unit that reads the file, `Launch` is a type that could not exist before they ran,
    and the failure arm is a separate variant. Nothing downstream re-asks — it has a
    `Launch` or it never got one.

    Shape and not merely syntax, for the reason `registry._read` checks its own: `argv` and
    `env` are read bare, so a truncated or hand-edited record would otherwise surface as a
    `KeyError` or a `TypeError` — from a function whose whole contract is that it does not
    raise.
    """
    source = path(profile_dir)
    if not source.exists():
        return Unknown(f"crom has no record of how the browser in {profile_dir} was launched.")

    try:
        text = source.read_text()
    except OSError as e:
        return _damaged(source, f"could not be read ({e})")
    try:
        document = json.loads(text)
    except json.JSONDecodeError as e:
        return _damaged(source, f"is not valid JSON ({e})")

    if not isinstance(document, dict):
        return _damaged(source, f"is a JSON {type(document).__name__}, not an object")
    if document.get("version") != SCHEMA_VERSION:
        return _damaged(
            source,
            f"was written by a crom speaking version {document.get('version')!r}, not the "
            f"version {SCHEMA_VERSION} this one reads",
        )
    argv, env = document.get("argv"), document.get("env")
    if not (isinstance(argv, list) and all(isinstance(item, str) for item in argv)):
        return _damaged(source, "is missing an `argv`, or has one that is not a list of strings")
    if not (isinstance(env, dict) and all(isinstance(value, str) for value in env.values())):
        return _damaged(source, "is missing an `env`, or has one that is not a table of strings")

    # Back to a tuple, because a `Launch` read from disk exists to be compared against one
    # built by `Launch.of`, and `["--foo"] != ("--foo",)`. Handing back the list would make
    # every such comparison report a difference that is not there.
    return Launch(argv=tuple(argv), env=dict(env))


def _damaged(source: Path, complaint: str) -> Unknown:
    """One sentence for every way the file can be unusable, because they share a consequence.

    Unreadable, unparseable, mis-shaped and version-mismatched are four findings and one
    outcome: no comparison can be made, and the profile's next successful launch replaces
    the file. Four variants would separate no behavior from any other.
    [LAW:one-type-per-behavior]
    """
    return Unknown(
        f"{source} {complaint}, so crom cannot tell what this browser was launched with. "
        f"The next launch of this profile replaces it."
    )
