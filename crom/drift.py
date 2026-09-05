"""Whether the browser a profile has running is the browser its config now describes.

`launched` writes down what crom spent at launch; this reads it back against what the
same profile resolves to now and says whether the two still agree. It is the whole of
crom's answer to "your config changed and your browser did not" — `crom list` and
`crom config` report the verdict, and the commands that act on it are built on the same
value rather than on a second comparison of their own. [LAW:one-source-of-truth]

Both sides come from `launched.Launch.of`, which is the sole projection of a
`ResolvedProfile` into a launch, so this file never sees `flags`, `features` or
`drop_flags` separately. That matters more than it looks: the three resolve through one
layered computation into a single `argv`, so a comparison assembled out of the three
would read correctly on every machine that uses only one of them and be wrong only on
the machines that mix them. Comparing whole `Launch` values makes that failure
unrepresentable rather than merely avoided. [LAW:types-are-the-program]

Naming the switches that moved is strictly downstream of the verdict. `==` decides
drifted; the entry diff below only says *which* entries a reader should look at. Built
the other way round — drifted iff some named entry differs — a difference the namer
could not name would read as agreement, which is the one wrong answer this file must
never give.
"""

from dataclasses import dataclass
from typing import ClassVar

from . import launched
from .launched import Launch
from .model import Flag, ResolvedProfile

# What the entry map calls the executable, which is `argv[0]` and answers no switch's
# question. A key rather than a special case in the diff: `chrome_binary` is layered
# configuration like everything else here, so a config that repoints it is drift, and it
# has to be nameable in the same sentence as a switch. It cannot collide with the keys
# beside it — a switch starts `--`, an environment entry is prefixed below.
_BINARY = "chrome binary"


@dataclass(frozen=True)
class Change:
    """One question the two launches answer differently.

    Whole values on both sides, not the part they differ on, for the reason
    `cli._effective_flags` renders whole values: a difference has no vocabulary for what
    is unchanged, so `--window-size` reported as `800,600 → 1280,800` quietly denies the
    switch name that makes either number mean anything. `--window-size=800,600` reads
    back in the spelling the config file uses. [FRAMING:representation]

    `None` is the side that does not carry the entry at all — a switch a config added, or
    one it dropped — and it is deliberately not `""`, which is a switch given an empty
    value (`--foo=`) or an environment variable set to nothing. Collapsing the two would
    report a variable cleared to empty as one that was never set.
    """

    subject: str
    launched: str | None
    resolves: str | None

    def describe(self) -> dict:
        return {"subject": self.subject, "launched": self.launched, "resolves": self.resolves}

    def __str__(self) -> str:
        return f"{self.subject}: launched {_said(self.launched)}, now {_said(self.resolves)}"


def _said(value: str | None) -> str:
    """One side of a change, including the side that has nothing to say."""
    return "(absent)" if value is None else value


# --- the four verdicts ---------------------------------------------------------------
# A class per verdict rather than one type carrying a `verdict` string, for the reason
# `doctor`'s standings are shaped this way: the slug and the sentence have to agree, only
# the site that decided knows both, and as fields they could be paired wrongly while as a
# class the slug is not data at all. [LAW:types-are-the-program]
#
# `finding` and `changes` are on all four, so `crom list` and `crom config` render a
# verdict without asking which one they hold. [LAW:dataflow-not-control-flow] Only
# `Drifted` has changes to name, so on the other three `changes` is a class attribute and
# not a field — a `Matches` carrying a list of what changed is unrepresentable rather
# than merely never built.


@dataclass(frozen=True)
class Stopped:
    """Nothing is running for this profile, so there is no browser to be stale.

    A verdict rather than an absence, because every reader of a listing gets one line per
    profile and this is that line's honest content. Answering `Matches` for a stopped
    profile would be true and useless — nothing is running with anything — and answering
    `Unmeasured` would blame the record for a comparison there was never a subject for.
    """

    slug: ClassVar[str] = "stopped"
    changes: ClassVar[tuple[Change, ...]] = ()
    finding: str


@dataclass(frozen=True)
class Unmeasured:
    """A browser is running and crom cannot say what it was launched with.

    Carries `launched.Unknown.why` unchanged rather than rewording it. That sentence is
    already finished and already user-facing, and it is the only thing separating "crom
    never wrote a record here" from "crom wrote one it can no longer read" — a difference
    nothing acts on and a reader still wants. [LAW:one-source-of-truth]

    Never a quieter `Matches`, for the reason `doctor.Unchecked` is never a quieter
    `Orphaned`: `crom up` is about to act on this verdict, and reporting agreement on the
    strength of a record crom never read is how a browser keeps running flags its config
    stopped asking for, with crom saying it checked. [LAW:no-silent-failure]
    """

    slug: ClassVar[str] = "unmeasured"
    changes: ClassVar[tuple[Change, ...]] = ()
    finding: str


@dataclass(frozen=True)
class Matches:
    """The running browser was launched with exactly what this config resolves to now."""

    slug: ClassVar[str] = "matches"
    changes: ClassVar[tuple[Change, ...]] = ()
    finding: str


@dataclass(frozen=True)
class Drifted:
    """The running browser was launched from a configuration this one no longer is.

    Holds the changes and derives the sentence from them, so the summary cannot come to
    name a switch the detail does not list. [LAW:one-source-of-truth]

    `changes` may be empty, and that is a real state rather than a hole: `==` over whole
    `Launch` values is what decides drift, and it sees one thing the entry map below
    deliberately does not — the order flags sit in on the command line. Two launches
    emitting the same switches in a different order are unequal and have no differing
    entry, so the sentence says so instead of trailing off after a dash — and that is the
    only way here, since neither side can name a switch twice.
    """

    slug: ClassVar[str] = "drifted"
    changes: tuple[Change, ...]

    @property
    def finding(self) -> str:
        named = ", ".join(change.subject for change in self.changes)
        return f"drifted — {named or 'its flags are in a different order'}"


Verdict = Stopped | Unmeasured | Matches | Drifted


def describe(verdict: Verdict) -> dict:
    """The published verdict — the shape `crom list --json` and `crom config --json` carry.

    One function over the union rather than a method on each class, the way
    `doctor.Row.describe` publishes its two verdicts: the key names are a contract, and a
    contract with one writer cannot drift into four spellings of it.
    """
    return {
        "verdict": verdict.slug,
        "finding": verdict.finding,
        "changes": [change.describe() for change in verdict.changes],
    }


def of(profile: ResolvedProfile, pids: tuple[int, ...]) -> Verdict:
    """How the browser running for `profile` stands against `profile`'s current config.

    `pids` is the evidence, taken as an argument rather than gathered here, because the
    process table is an external system and both callers have already read it — `crom
    list` from one `chrome.scan` covering every profile it lists. Re-asking per profile
    would put a `ps` call in a loop and, worse, let a browser that started between the two
    reads be running in one half of a row and stopped in the other.
    [LAW:effects-at-boundaries] the reading happens at the command; this decides.

    An empty `pids` is the whole of "nothing is running", which is the same thing
    `cli._status` derives `running` from — so this takes the evidence itself rather than
    a boolean somebody else already reduced it to. [LAW:one-source-of-truth]
    """
    current = Launch.of(profile)
    match pids, launched.read(profile.profile_dir):
        case (), _:
            return Stopped("not running, so its next launch takes this configuration")
        case _, launched.Unknown(why=why):
            return Unmeasured(why)
        case _, launched.Launch() as recorded:
            # `==` on whole `Launch` values, and nothing narrower. See this file's header:
            # the entry diff exists to name what moved, never to decide whether anything
            # did. [LAW:types-are-the-program]
            return (
                Matches("running with what this configuration resolves to")
                if recorded == current
                else Drifted(_changes(recorded, current))
            )


def _changes(recorded: Launch, current: Launch) -> tuple[Change, ...]:
    """Every question the two launches answer differently, in one order both sides share.

    Sorted by subject rather than left in command-line order, because the two sides have
    two orders and a report built from either one would list a switch the other side puts
    somewhere else. Sorting is the only ordering that is a property of the comparison
    rather than of one of its operands.
    """
    was, now = _entries(recorded), _entries(current)
    return tuple(
        Change(subject, was.get(subject), now.get(subject))
        for subject in sorted(was.keys() | now.keys())
        if was.get(subject) != now.get(subject)
    )


def _entries(launch: Launch) -> dict[str, str]:
    """A launch as answers keyed by the questions they answer, which is what can be diffed.

    Keyed by switch name and not by argv position: a config that changes `--window-size`
    from `800,600` to `1280,800` moved one answer, and a positional diff would report it
    as one switch removed and an unrelated one added. Sound on both sides because a name
    cannot address two entries: `flags.layer` refuses a stanza that sets a switch twice, and
    `launched.read` refuses a record that names one twice.

    `argv[:1]` rather than `argv[0]`: this is the one place a record that has been
    hand-edited down to nothing reaches, and a record with no executable in it should
    surface as an entry the current side has and it does not, which is exactly what an
    empty slice produces. Total by construction, so no guard is needed to make it so.
    [LAW:no-defensive-null-guards]

    `env` is prefixed rather than merged bare, so `crom config` can print a switch and a
    variable in one list and a reader can tell which they are looking at. It is here at
    all because `[defaults].env` and a profile's `env` are layered configuration exactly
    as `flags` is: editing either changes the browser crom would launch, so leaving it out
    would make an env edit a config change no verdict could ever report.
    """
    return {
        **{_BINARY: text for text in launch.argv[:1]},
        **{Flag.parse(text).switch: text for text in launch.argv[1:]},
        **{f"env {name}": value for name, value in launch.env.items()},
    }
