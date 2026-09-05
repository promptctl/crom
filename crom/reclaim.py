"""Decide whether one thing `crom doctor` found may be handed back, and say so either way.

`doctor` reads crom's own state and judges it; this asks the one further question a
repair needs — is this particular finding safe to act on? They are separate because
`doctor`'s promise is that it never writes and never raises, and a module that also
repaired would keep that promise only for as long as nobody passed it the other
argument. [LAW:decomposition]

Nothing here performs the act either. Each decision takes a `doctor.Survey` and answers
with a stamped type naming what may be reclaimed, or with `Refused`; the caller holding
the stamp is what spends it, so the whole of the policy is one `match` a test can drive
with a fabricated survey and no machine to fabricate it on. [LAW:effects-at-boundaries]

Every refusal here leans the same way, and it is the only lean available. Releasing a port and
deleting a directory are irreversible — the number goes to the next profile that asks for
one, and the bytes are gone — while every refusal here costs a user one recoverable
action. So crom refuses on any evidence it did not actually get, and the sentence names
which evidence was missing. [LAW:no-silent-failure]
"""

from dataclasses import dataclass
from pathlib import Path

from .doctor import (
    Declared,
    Foreign,
    Idle,
    Orphaned,
    OwnBrowser,
    Row,
    Staged,
    Survey,
    Unchecked,
    Unprobed,
)
from .model import Reason


@dataclass(frozen=True)
class Refused:
    """Crom will not reclaim this, in the slug a script branches on and the sentence a
    person reads.

    Both, from the one site that decided, for the reason `doctor`'s verdicts carry a slug
    beside their finding: the site that knows why crom is refusing is the only site that
    knows which of the two vocabularies the refusal belongs to, and a caller re-deriving
    the reason from the sentence would be reading English to answer a question the
    decision already answered. [LAW:one-source-of-truth]
    """

    reason: Reason
    why: str


@dataclass(frozen=True)
class Releasable:
    """One ledger row whose port crom will hand back, carried whole rather than as its key.

    The row is what the caller needs to report the release — the number that came free is
    the fact a reader acts on, and it is already here — and carrying it keeps the key that
    gets removed the same string `releasable` matched, rather than one the caller spelled
    again. [LAW:one-source-of-truth]
    """

    row: Row


@dataclass(frozen=True)
class Deletable:
    """One staging directory crom will delete, as `doctor` found it.

    The path travels from the survey and never from the argument. A caller may name this
    directory any way the filesystem lets them — a relative path, a route through a
    symlink — and what gets deleted is still the path `doctor` walked to, so the argument
    only ever *selects* a finding and can never widen one. [LAW:parse-dont-validate]
    """

    staged: Staged


def releasable(key: str, found: Survey) -> Releasable | Refused:
    """Whether crom will hand back the port held under one ledger key.

    The raw key, matched against the ledger as a string and never taken apart. A hand
    repair is how a single reservation was released before this existed, and it leaves
    keys like `a/b/c` that `model.parse_ref` refuses — so the keys most in need of
    releasing are exactly the ones a `ProfileRef` argument could not have named.
    `doctor` already judges them; this reaches them. [LAW:types-are-the-program]

    Six arms over thirteen states, and every state is named rather than left to a
    catch-all, the way `_staging` names all three of its outcomes. Release acts on
    `orphaned` and on the two livenesses that establish nobody is on the number — which
    is the whole policy, stated once here rather than as a chain of guards at the call
    site. [LAW:parse-dont-validate]

    Adding a `Standing` or a `Liveness` to `doctor` means answering it here too: a verdict
    with no arm falls out of this match, and there is no type checker in this repo to say
    so. That obligation is why the permitted pair is spelled out instead of being whatever
    the other arms did not catch — a new verdict silently joining the releasable set is
    the one mistake here that cannot be undone.

    Every refusal says what crom will not do before it says what it saw, and the evidence
    follows on its own lines. A `finding` is not reliably a fragment — `_Unread` folds a
    config parser's own first line into one, ending in that sentence's full stop — so a
    refusal that opened with a finding and continued with a comma printed `project's., so
    crom cannot say`. Composing the other way round costs nothing and cannot break,
    whatever a `doctor` verdict is worded like next. [LAW:one-source-of-truth] the finding
    stays `doctor`'s to word.

    The two refusals crom cannot argue itself out of are the last two below. `unchecked`
    and `unprobed` both mean crom never established the fact — one about the config, one
    about the port — and treating either as its confident sibling would be releasing a
    port on evidence crom does not have. `own` is different and refused anyway: crom knows
    exactly what is happening, and a declaration that vanished while its browser kept
    running is far more often a config mid-edit than a profile someone meant to delete.
    """
    match {row.ref: row for row in found.rows}.get(key):
        case None:
            return Refused(
                Reason.RESERVATION_UNKNOWN,
                f"the port ledger holds no reservation under {key!r}. `crom doctor` lists "
                f"every key it does hold, spelled as it has to be typed here — a hand "
                f"repair can leave one that is not a legal namespace/name at all.",
            )
        case Row(standing=Declared(finding=why), held=held):
            return Refused(
                Reason.RESERVATION_DECLARED,
                f"port {held.port} is still promised to a profile that exists, so "
                f"releasing it would hand the number to the next profile that asks for "
                f"one while this one goes on handing it out. Run `crom rm {key}` to "
                f"undeclare the profile and release its port together."
                f"\n{why}",
            )
        case Row(standing=Unchecked(finding=why), held=held):
            return Refused(
                Reason.RESERVATION_UNSETTLED,
                f"crom could not read a config for {key!r}, so it cannot say whether "
                f"anything still declares it. A released port never comes back — every "
                f"checked-in `.mcp.json` and `CDP_URL` pointing at {held.port} breaks "
                f"with it — and a config that will not load may declare this profile "
                f"perfectly well."
                f"\n{why}",
            )
        case Row(
            standing=Orphaned(finding=nothing_declares), liveness=Unprobed(finding=why), held=held
        ):
            return Refused(
                Reason.RESERVATION_UNSETTLED,
                f"nothing declares {key!r}, but crom could not tell who holds port "
                f"{held.port}. A released number goes to the next profile that asks for "
                f"one, so crom will not hand back a port it could not account for."
                f"\n{nothing_declares}"
                f"\n{why}",
            )
        case Row(
            standing=Orphaned(finding=nothing_declares), liveness=OwnBrowser(finding=why), held=held
        ):
            return Refused(
                Reason.RESERVATION_IN_USE,
                f"nothing declares {key!r} any more, but its own browser is still running "
                f"on port {held.port}. A declaration that vanished while its browser kept "
                f"running is far more often a config mid-edit than a profile someone "
                f"deleted, and a released port never comes back. Close that browser and "
                f"run this again — `crom down` cannot reach it, because it resolves "
                f"profiles through the config that no longer declares this one."
                f"\n{nothing_declares}"
                f"\n{why}",
            )
        case Row(standing=Orphaned(), liveness=Idle() | Foreign()) as row:
            return Releasable(row)


def deletable(target: str, found: Survey) -> Deletable | Refused:
    """Whether crom will delete the staging directory a caller named.

    Selection is by resolved path on both sides, so the argument may be spelled however a
    shell produced it — pasted from `crom doctor`, completed as a relative path, routed
    through a symlinked state directory — and still name the same directory. Resolving is
    a filesystem question rather than a string one, which is why it is asked of the
    filesystem. What comes back is `doctor`'s own finding, so normalising the spelling can
    never widen what gets deleted. [LAW:parse-dont-validate]

    Nothing outside the survey is deletable, and that is the whole of the safety here
    rather than a test this repeats. `doctor._leaks` already refuses a lock file, a
    committed profile directory, and — the one that costs data — the `.partial` copy
    `migrate._move_staged` leaves, which on a same-filesystem move briefly holds the only
    copy of a profile. Re-deriving those tests here would be a second reader of what a
    leak is, free to drift from the one the report was drawn from. [LAW:single-enforcer]
    """
    wanted = Path(target).resolve()
    match {item.path.resolve(): item for item in found.staged}.get(wanted):
        case None:
            return Refused(
                Reason.STAGING_UNKNOWN,
                f"`crom doctor` found no abandoned staging directory at {wanted}, and "
                f"only one it found can be deleted here. A migration stages a legacy "
                f"profile under the same dot-prefixed shape and that copy is sometimes "
                f"the only one there is, so crom deletes what it reported and nothing "
                f"it merely recognises.",
            )
        case Staged() as item:
            return Deletable(item)

