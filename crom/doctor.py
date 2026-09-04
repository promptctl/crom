"""Judge every port reservation against the config the ledger records as its source.

`registry` owns the ledger and `config` owns the declarations. They are two maps of one
territory — which profiles exist — and this is the one place that reads either against
the other. It sits above both rather than inside one, so neither has to learn about the
other to answer a question only `crom doctor` asks. [LAW:decomposition]

Nothing here writes, and nothing here raises for a config it dislikes. A doctor runs on
the machine whose state is the problem, so a config crom cannot load becomes a row that
says so — never a repair, never a raise, and never a namespace mapping quietly dropped
on the way past. That is why configs are reached through `config.load_file` alone:
`resolve.scope_for` forgets a stale namespace mapping as it passes, and
`config.load_user_scope` resets a user config that will not tokenize.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from . import registry
from .config import load_file
from .model import USER_NAMESPACE, CromError, ProfileRef
from .paths import registry_file, user_config_file
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


@dataclass(frozen=True)
class Survey:
    """Everything `crom doctor` found, in the order it reports it."""

    registry: Path
    rows: tuple[Row, ...]

    def describe(self) -> dict:
        """An object, not the bare array `crom list` gives.

        The ledger's path is a fact about the listing rather than about any row in it,
        and an array has nowhere to put it. It is also what lets the leaks this command
        has yet to learn — a staging directory, a port a foreign process now holds — sit
        as their own keys beside `reservations`. [FRAMING:representation]
        """
        return {
            "registry": str(self.registry),
            "reservations": [row.describe() for row in self.rows],
        }


def survey() -> Survey:
    """Every reservation in the ledger, each judged against the config claiming it.

    Sorted by port, because the port is what the ledger is a ledger of: a run of numbers
    with a hole in it, or two rows landing on one number, is what a reader is here to
    see, and neither is visible in an order sorted by name. The ref breaks ties rather
    than leaving equal ports in dict order — `registry._reject_foreign_claim` keeps ports
    unique through crom's own writers, and a ledger that got past them is this command's
    subject, not its impossible case.

    The sources are made distinct before they are consulted, so a project declaring twenty
    profiles reads its config once and spends that one answer over all twenty rows.
    """
    ledger = registry.reservations()
    consulted = {source: _consult(source) for source in {held.source for held in ledger.values()}}
    return Survey(
        registry=registry_file(),
        rows=tuple(
            Row(ref, held, consulted[held.source].standing(ref))
            for ref, held in sorted(ledger.items(), key=lambda entry: (entry[1].port, entry[0]))
        ),
    )


@dataclass(frozen=True)
class _Consulted:
    """A config crom read, and the exact ledger keys it currently declares."""

    config: Path
    declares: frozenset[str]

    def standing(self, ref: str) -> Standing:
        if ref in self.declares:
            return Declared(f"{self.config} declares it")
        return Orphaned(f"{self.config} no longer declares it")


@dataclass(frozen=True)
class _Settled:
    """A config crom never got an answer out of, so every row under it stands the same.

    The pair with `_Consulted` is what keeps `survey` free of a match: a source is
    consulted once, and the row asks whatever came back for its standing without knowing
    which of the two it is holding. [LAW:dataflow-not-control-flow]
    """

    fixed: Standing

    def standing(self, ref: str) -> Standing:
        return self.fixed


def _consult(source: str | None) -> _Consulted | _Settled:
    """Ask one recorded config which ledger keys it declares, or settle every row under it.

    Keys are *composed* through `ProfileRef` rather than ledger keys being taken apart.
    `ProfileRef.__str__` owns the `namespace/name` format, and a ledger key a hand-edit
    invented — the only way to release an orphan today, and a key `_read` checks entries
    but never names, so one really does reach here — would make `parse_ref` raise, turning
    the command that shows the mess into the command that dies on it. Asking the
    declarations which keys they produce answers for every string a ledger can hold, and
    it costs nothing extra: a config that has renamed its own namespace stops producing
    the old key, which is exactly the orphan it now is. [LAW:types-are-the-program]

    A config that is not there has been checked, and the answer is that nothing declares
    this reservation. A config crom cannot load has not been checked at all — see
    `Unchecked` for why the two must never collapse.

    `OSError` beside `CromError` because `load_file` guards `is_file` and then reads: a
    config whose permissions were changed, or which turned into a directory between the
    two calls, escapes as a bare traceback out of a command that exists for machines in
    exactly that condition.
    """
    if source is None:
        return _Settled(Unchecked("the ledger records no config for it"))
    config = Path(source)
    if not config.is_file():
        return _Settled(Orphaned(f"{config} no longer exists"))
    # The `user` namespace is a property of the path crom fixed, never of the file's
    # contents — `parse` refuses a user config that names one — so it is supplied here
    # exactly as `load_user_scope` supplies it. Without it every personal profile read
    # `unchecked`, on a config that was missing the one key it may not have. The split is
    # a value the same call takes either way, not a second way of loading a config.
    # [LAW:dataflow-not-control-flow]
    namespace = USER_NAMESPACE if config == user_config_file() else None
    try:
        scope = load_file(config, namespace=namespace)
    except (CromError, OSError) as e:
        # The first line only, and the same one in both renderings: a row is a row, and
        # a caller handed a different sentence from the human reading over its shoulder is
        # the divergence `Survey.describe` exists to avoid. `split` and not `splitlines`,
        # which answers `[]` for the empty string and would index out of range.
        first_line = str(e).split("\n", 1)[0]
        return _Settled(Unchecked(f"{config} could not be checked: {first_line}"))
    return _Consulted(
        config, frozenset(str(ProfileRef(scope.namespace, name)) for name in scope.profiles)
    )
