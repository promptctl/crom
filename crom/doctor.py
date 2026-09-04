"""Read the state crom owns on this machine, and say where it has leaked.

Two leaks, two questions, one command. A port reservation is judged against the config
the ledger records as its source — `registry` owns the ledger, `config` owns the
declarations, and they are two maps of one territory. A staging directory is a second
kind of leak entirely: `seed._staged` builds a profile beside its final path and moves it
in only once it is whole, so a process killed between those two moments leaves the
half-built copy behind under the namespace's profile root, dot-prefixed and therefore
hidden from `ls`.

This module sits above `registry`, `config` and `seed` rather than inside any of them, so
none has to learn about the others to answer a question only `crom doctor` asks.
[LAW:decomposition]

Nothing here writes, and nothing here raises for a config it dislikes or a directory it
cannot read. A doctor runs on the machine whose state is the problem, so a config crom
cannot load becomes a row that says so — never a repair, never a raise, and never a
namespace mapping quietly dropped on the way past. That is why configs are reached
through `config.load_file` alone: `resolve.scope_for` forgets a stale namespace mapping
as it passes, and `config.load_user_scope` resets a user config that will not tokenize.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISREG
from typing import ClassVar

from . import migrate, registry
from .config import load_file
from .model import USER_NAMESPACE, CromError, ProfileRef, namespace_of
from .paths import default_profiles_root, registry_file, user_config_file
from .registry import Reservation

# --- the three standings -----------------------------------------------------------
# Three classes rather than one carrying a `standing` string, because the slug and the
# sentence must agree and only the site that decided knows both: as a field they could
# be paired wrongly, as a class the slug is not data at all. [LAW:types-are-the-program]
#
# `finding` is on all three, so a caller renders a row without asking which standing it
# holds, and every one of them opens with the config crom consulted — naming what was
# checked is a property of the type rather than of three call sites remembering to.
# [LAW:dataflow-not-control-flow]


@dataclass(frozen=True)
class Declared:
    """The config the ledger names as this reservation's source still declares it."""

    slug: ClassVar[str] = "declared"
    finding: str


@dataclass(frozen=True)
class Orphaned:
    """Crom consulted the config claiming this reservation, and nothing declares it.

    A leak: the port stays held, `crom list` cannot see it, and `registry._allocate`
    steps over the number forever.
    """

    slug: ClassVar[str] = "orphaned"
    finding: str


@dataclass(frozen=True)
class Unchecked:
    """Crom could not consult a config for this reservation, and claims nothing.

    Distinct from `Orphaned` because releasing a reservation is what that verdict is for,
    and a released port does not come back: every checked-in `.mcp.json` and `CDP_URL`
    pointing at the number breaks with no way to recover it. A config that will not load
    may well declare this profile, so answering "orphaned" would turn "I cannot tell" into
    "nothing declares it" on the evidence of a file crom never read.
    [LAW:no-silent-failure]
    """

    slug: ClassVar[str] = "unchecked"
    finding: str


Standing = Declared | Orphaned | Unchecked


@dataclass(frozen=True)
class Row:
    """One ledger reservation, and what crom found when it checked the config for it."""

    ref: str
    held: Reservation
    standing: Standing

    def describe(self) -> dict:
        """The published row: the ledger's own fields, then crom's finding about them.

        Built on `Reservation.describe` rather than beside it, so the fields the ledger
        publishes keep one owner and cannot drift from what it actually holds.
        [LAW:one-source-of-truth]
        """
        return {
            **self.held.describe(ref=self.ref),
            "standing": self.standing.slug,
            "finding": self.standing.finding,
        }


# --- what a seed left behind --------------------------------------------------------
# Deliberately not a fourth `Standing`. A standing is a verdict about declarations —
# whether some config still claims a ledger key — and the question here is whether a
# directory is the residue of a run that died. Different question, different evidence,
# so it gets its own types rather than stretching a vocabulary over a shape it was not
# designed for. [LAW:no-mode-explosion]


@dataclass(frozen=True)
class Staged:
    """A half-built profile a seed left beside its final path and never moved in.

    A seed *currently* running has a staging directory too, and this reports it: the
    evidence on disk is identical, and telling them apart would mean asking whether the
    profile is locked, which means splitting `.<name>.<rand>` back into a name the format
    cannot give up. That is the residue of this approach rather than an oversight —
    whether a leak is live is the axis `crom doctor` measures for ports, and it stays
    orthogonal to what a directory *is* rather than collapsing into it.
    """

    namespace: str
    path: Path
    bytes: int

    def describe(self) -> dict:
        return {"namespace": self.namespace, "path": str(self.path), "bytes": self.bytes}


@dataclass(frozen=True)
class Unscanned:
    """A namespace crom could not look under for staging directories, and why.

    Without this a clean report would mean two different things — "nothing is leaking"
    and "I could not check" — and a reader has no way to pull them back apart.
    [LAW:no-silent-failure] It carries `error` because that is the key `crom list`
    already gives a namespace it could not load; one convention, so a script that learned
    it once reads both commands. [LAW:one-source-of-truth]
    """

    namespace: str
    error: str

    def describe(self) -> dict:
        return {"namespace": self.namespace, "error": self.error}


@dataclass(frozen=True)
class Survey:
    """Everything `crom doctor` found, in the order it reports it."""

    registry: Path
    rows: tuple[Row, ...]
    staged: tuple[Staged, ...]
    unscanned: tuple[Unscanned, ...]

    def describe(self) -> dict:
        """An object, not the bare array `crom list` gives.

        The ledger's path is a fact about the listing rather than about any row in it,
        and an array has nowhere to put it. It is also what lets a second kind of leak sit
        as its own key beside `reservations` instead of as a row pretending to be a
        reservation. [FRAMING:representation]

        `staging` holds two element shapes, the way `crom list` already gives an array
        whose elements are not all alike: a directory crom found, or a namespace it could
        not look under. They are separate fields here because that is what keeps every
        reader of either — this method, the presenter, a test — free of a discrimination
        the survey already made once. [LAW:parse-dont-validate]
        """
        return {
            "registry": str(self.registry),
            "reservations": [row.describe() for row in self.rows],
            "staging": [found.describe() for found in (*self.staged, *self.unscanned)],
        }


def survey() -> Survey:
    """Every reservation in the ledger, and every staging directory under a profile root.

    Reservations are sorted by port, because the port is what the ledger is a ledger of: a
    run of numbers with a hole in it, or two rows landing on one number, is what a reader
    is here to see, and neither is visible in an order sorted by name. The ref breaks ties
    rather than leaving equal ports in dict order — `registry._reject_foreign_claim` keeps
    ports unique through crom's own writers, and a ledger that got past them is this
    command's subject, not its impossible case.

    Both halves consult the same configs, and every distinct one is read exactly once: a
    project declaring twenty profiles spends that one answer over all twenty rows and over
    the scan of its profile root. That sharing is the reason `_consult` reports what
    happened rather than a verdict — the two halves divide the outcomes differently, and
    only they know how. [LAW:decomposition]

    Which namespaces have a profile root is read from the ledger *and* from the mapping
    index, because the index alone is lossy in exactly the case this command is for.
    `registry.forget_mapping` drops a namespace from it whenever `resolve.scope_for` meets
    a config it can no longer load, and deliberately keeps the ports — so a project whose
    config was deleted and then referred to from elsewhere kept reporting `orphaned`
    reservations while its profile root went unscanned and unmentioned. Silence, where the
    whole point of the second half is that a root crom did not check says so.
    [LAW:no-silent-failure]

    Reading both registers costs one dict merge and no new precedence: the index still
    wins for a namespace in both, since it is what makes a namespace a global address, and
    `user` is still added last and cannot be displaced — its config is a fixed path, so a
    project that claimed the name could otherwise send this looking somewhere else.
    A namespace is carried against the source `_consult` takes rather than against a
    `Path`, because the ledger may record no config for a reservation at all — a hand
    repair is the only way to release an orphan today, so this command meets one. That
    namespace has no root to look under and reports as one crom could not check, which is
    an answer; a `Path` could not have held the case at all.
    """
    ledger = registry.reservations()
    namespaces: dict[str, str | None] = {
        **{namespace_of(ref): held.source for ref, held in ledger.items()},
        **{name: str(config) for name, config in registry.namespaces().items()},
        USER_NAMESPACE: str(user_config_file()),
    }
    consulted = {
        source: _consult(source)
        for source in {held.source for held in ledger.values()} | set(namespaces.values())
    }
    found = tuple(
        item
        for namespace, source in sorted(namespaces.items())
        for item in _staging(namespace, consulted[source])
    )
    return Survey(
        registry=registry_file(),
        rows=tuple(
            Row(ref, held, consulted[held.source].standing(ref))
            for ref, held in sorted(ledger.items(), key=lambda entry: (entry[1].port, entry[0]))
        ),
        # Split here and nowhere else. The scan produces the two kinds interleaved by
        # namespace, and separating them once leaves `describe`, the presenter and every
        # test holding a tuple whose element type they can read off the field name.
        staged=tuple(item for item in found if isinstance(item, Staged)),
        unscanned=tuple(item for item in found if isinstance(item, Unscanned)),
    )


# --- consulting a config ------------------------------------------------------------
# What happened when crom went to read one config, in three types — not a verdict, which
# is why they are separated this way. The two halves of the survey divide these outcomes
# differently: an absent config has *answered* the declarations question (it declares
# nothing) and answered the profile-root one too (it overrides nothing, so the root is
# crom's default), while a config that will not load has answered neither. Collapsing the
# absent and the unloadable would make one of those two halves wrong whichever way it
# fell. [LAW:types-are-the-program]
#
# `standing` is on all three, so a reservation row asks whatever came back and never
# learns which it is holding. [LAW:dataflow-not-control-flow]


@dataclass(frozen=True)
class _Read:
    """A config crom loaded: the ledger keys it declares, and where it puts profiles.

    `declares` is computed once here rather than on each ask, so a config declaring twenty
    profiles is walked once and not once per row.
    """

    config: Path
    declares: frozenset[str]
    profiles_root: Path

    def standing(self, ref: str) -> Standing:
        if ref in self.declares:
            return Declared(f"{self.config} declares it")
        return Orphaned(f"{self.config} no longer declares it")


@dataclass(frozen=True)
class _Gone:
    """A project config that is not there.

    What it declares is settled — a file that is not there declares nothing — and where
    it kept its profiles is not. `state_dir` was a fact when the directories were made,
    and deleting the file does not move the bytes; the two questions are settled by
    absence to different degrees, which is why this is a separate outcome from `_Read`
    and not a `_Read` with an assumed root.

    *Project* is the whole of it, and the user config does not want a fourth outcome
    beside this one. `cli._bootstrap_user_config` writes it from
    `CromGroup.command_class.invoke`, before the body of every command including this
    one, so by the time `survey` looks there is a file there to read. A separate outcome
    for the user config that was never written describes a state this program cannot
    reach, and the arm every `match` would carry for it is dead the day it is written —
    which is how a union stops being the strongest true theorem about its domain.
    [LAW:types-are-the-program]
    """

    config: Path

    def standing(self, ref: str) -> Standing:
        return Orphaned(f"{self.config} no longer exists")


@dataclass(frozen=True)
class _Unread:
    """A config crom got no answer out of, so every question about it stands the same."""

    why: str

    def standing(self, ref: str) -> Standing:
        return Unchecked(self.why)


def _consult(source: str | None) -> _Read | _Gone | _Unread:
    """Read one recorded config, or say why crom could not.

    Keys are *composed* through `ProfileRef` rather than ledger keys being taken apart.
    `ProfileRef.__str__` owns the `namespace/name` format, and a ledger key a hand-edit
    invented — the only way to release an orphan today, and a key `_read` checks entries
    but never names, so one really does reach here — would make `parse_ref` raise, turning
    the command that shows the mess into the command that dies on it. Asking the
    declarations which keys they produce answers for every string a ledger can hold, and
    it costs nothing extra: a config that has renamed its own namespace stops producing
    the old key, which is exactly the orphan it now is. [LAW:types-are-the-program]

    A config that is not there declares nothing, which is an answer — see `Unchecked` for
    why that must never collapse into the unloadable case, which answers nothing at all.

    `OSError` beside `CromError` because `load_file` guards `is_file` and then reads: a
    config whose permissions were changed, or which turned into a directory between the
    two calls, escapes as a bare traceback out of a command that exists for machines in
    exactly that condition.
    """
    if source is None:
        return _Unread("the ledger records no config for it")
    config = Path(source)
    # The `user` namespace is a property of the path crom fixed, never of the file's
    # contents — `parse` refuses a user config that names one — so it is supplied below
    # exactly as `load_user_scope` supplies it. Without it every personal profile read
    # `unchecked`, on a config that was missing the one key it may not have. The split is
    # a value the same call takes either way, not a second way of loading a config.
    # [LAW:dataflow-not-control-flow]
    namespace = USER_NAMESPACE if config == user_config_file() else None
    if not config.is_file():
        return _Gone(config)
    try:
        scope = load_file(config, namespace=namespace)
    except (CromError, OSError) as e:
        # The first line only, and the same one in both renderings: a row is a row, and
        # a caller handed a different sentence from the human reading over its shoulder is
        # the divergence `Survey.describe` exists to avoid. `split` and not `splitlines`,
        # which answers `[]` for the empty string and would index out of range.
        first_line = str(e).split("\n", 1)[0]
        return _Unread(f"{config} could not be checked: {first_line}")
    return _Read(
        config=config,
        declares=frozenset(str(ProfileRef(scope.namespace, name)) for name in scope.profiles),
        profiles_root=scope.profiles_root,
    )


def _staging(
    namespace: str, consulted: _Read | _Gone | _Unread
) -> tuple[Staged | Unscanned, ...]:
    """Look under one namespace's profile root, and say what the looking did not cover.

    The root is `profiles_root / namespace` — the same composition `resolve.resolve_spec`
    makes, so this looks exactly where crom would have put the profile rather than
    somewhere that resembles it. The namespace comes from the ledger's index and the root
    from the config that index names, which is what settles a config that renamed itself:
    its old namespace's directories are still under the root that config uses today.

    The three outcomes are three states of knowledge, and the middle one is why finding
    and saying-so are not alternatives here. A config crom read names the root outright. A
    config crom could not load names nothing, so there is no root and no scan. A *deleted*
    config sits between them: `state_dir` was a fact when the directories were made and
    deleting the file did not move the bytes, so the default root is where they are unless
    that config said otherwise — and crom can no longer ask. Scanning it reports every
    leak genuinely there; the caveat beside it is what stops finding none from reading as
    proof there are none. Answering only one of those two discards a true finding or
    claims a completeness crom does not have. [LAW:no-silent-failure]
    """
    match consulted:
        case _Read(profiles_root=root):
            return _leaks(namespace, root / namespace)
        case _Gone(config=config):
            root = default_profiles_root() / namespace
            return (
                *_leaks(namespace, root),
                Unscanned(
                    namespace,
                    f"{config} is gone, so crom checked {root} — a state_dir that config "
                    f"declared would have put them somewhere crom can no longer name",
                ),
            )
        case _Unread(why=why):
            return (Unscanned(namespace, why),)


def _leaks(namespace: str, root: Path) -> tuple[Staged | Unscanned, ...]:
    """Every staging directory sitting in one profile root, each with its size.

    Three tests, and each is exact rather than approximate. The leading dot is decisive
    because `model.validate_name` requires a profile name to start alphanumeric, so no
    directory crom ever commits here can begin with one. `is_dir` is decisive because one
    other dot-prefixed resident is `locking.exclusive`'s `.<name>.lock`, a regular file
    that every command touching the profile leaves behind — reported as a leak it would
    fire on every machine, every run. [LAW:types-are-the-program]

    The third test is the one that keeps this command from costing someone their data.
    `seed._staged` is not the only writer here: `migrate._move_staged` stages a legacy
    profile as `.<name>.partial` in this very root, and on a same-filesystem move it
    renames `old_dir` away *before* that exists — so an interruption in that window leaves
    the `.partial` holding the only copy of the profile. This command's own sentence calls
    what it lists "left behind by an interrupted seed", beside a size `measure` calls what
    deleting it would reclaim. Both are false for a migration's staging directory, and a
    reader who acted on them would lose their cookies and logins. Migration owns the
    suffix and this reads it from there. [LAW:one-source-of-truth]

    No test takes a name apart, which is what makes them total: `.<name>.<rand>` cannot be
    split back into its halves, because `_NAME_RE` lets a profile name contain dots of its
    own. `endswith` on a constant asks nothing of the name's structure.

    The whole read sits under one handler, classification included. `is_dir` stats, and on
    3.12 — the floor `requires-python` promises — `Path.is_dir` re-raises anything outside
    `pathlib._ignore_error`, so a root that is readable but not searchable lists fine and
    then raises EACCES on the first entry. Outside the `try` that escaped as a traceback
    from the command whose docstring promises it never raises for a directory it cannot
    read. `Path.is_dir` swallows every `OSError` from 3.13 on, so the bug was invisible on
    a new interpreter and live on the supported one. [LAW:single-enforcer]

    A root that was never created holds nothing, and that is an answer. A root crom cannot
    read is not one.
    """
    try:
        staged = tuple(
            entry
            for entry in sorted(root.iterdir())
            if entry.name.startswith(".")
            and not entry.name.endswith(migrate.STAGING_SUFFIX)
            and entry.is_dir()
        )
    except FileNotFoundError:
        return ()
    except OSError as e:
        return (Unscanned(namespace, f"{root} could not be read: {e.strerror}"),)
    # `measure` stays outside the handler: it already skips an entry it cannot stat and
    # documents its number as a floor, so a permission tightened under it costs a byte
    # count rather than the row that says the directory is there.
    return tuple(Staged(namespace, entry, measure(entry)) for entry in staged)


def measure(directory: Path) -> int:
    """The bytes in the regular files under `directory` — what deleting it would reclaim.

    Here rather than in the presenter that first needed it, because it is a fact about
    crom's state on disk and this is the module that reads those. `crom rm`'s confirmation
    prompt and `crom doctor`'s listing then quote one measurement instead of two that
    could come to disagree about what a directory's size means. [LAW:one-source-of-truth]

    `lstat` rather than `stat`: a symlink's target is not deleted along with the
    directory, so counting the target would overstate what is about to be lost, and a
    dangling link would raise rather than measure. `followlinks=False` states the same
    guarantee for the walk itself — `rglob` happens to behave that way on 3.12, but that
    is a property of pathlib's recursive selector rather than something this code asks
    for.

    An entry that cannot be stat'ed is skipped rather than raising, and the number is
    therefore the floor rather than the total. That is the honest shape of a size: the
    finding a caller acts on is that the directory is *there*, which is established before
    this is called, and a doctor that refused to print a number for a tree it could not
    fully read would be withholding the report it exists to give.
    """
    total = 0
    for parent, _directories, files in os.walk(directory, followlinks=False):
        for name in files:
            try:
                info = os.lstat(os.path.join(parent, name))
            except OSError:
                continue
            if S_ISREG(info.st_mode):
                total += info.st_size
    return total
