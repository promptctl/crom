"""crom's mutating operations, performed by anything — not only by a command body.

The reading half of crom has been a library for a while: `resolve` returns typed values,
takes a `log=`, and imports no click. This is the mutating half's home — functions over an
already-resolved profile that do the work and hand back what happened, leaving the
sentence a user reads to be written somewhere else.

What lives here rather than in a command body is the critical section. Seeding, the
liveness read, the launch, and the replacement that may follow are one indivisible span,
and `up_cmd` carried a comment saying so — an invariant held by the one caller that
assembled it correctly. A second caller was free to assemble it wrongly, and what that
buys is not an exception but two `crom up` calls racing for one port. Written here, the
span is not something a caller assembles at all: it is what calling `up` is.
[LAW:no-ambient-temporal-coupling] the ordering is a place, not a convention every future
entry point has to be told about.

No click below this line, and nothing printed: what an operation says while it works goes
to `log=`, exactly as `resolve` and `session` already take one, and what it decided comes
back as a value. [LAW:effects-at-boundaries] the decision is computed here and rendered at
the command.
"""

from dataclasses import dataclass
from enum import Enum, auto

from . import chrome, configwrite, drift, report, seed
from .model import ResolvedProfile


class OnDrift(Enum):
    """What `up` does about a browser its config has moved on from — the whole of the
    policy the CLI spells `--no-restart`.

    A named pair rather than that flag's own boolean, because the arms below match on it.
    `case drift.Drifted(), False` asks every reader to hold which way round the negation in
    `--no-restart` runs, at the one site in crom where reading it backwards kills a browser
    the user asked crom to leave alone. Named, the arm states which policy it is and the
    pairing cannot be read wrong. [LAW:types-are-the-program]

    Two values and not three: the policy withholds the stop, never the start. A profile
    with nothing running still launches under it, because launching costs no one the tabs
    this policy exists to protect — so "do not converge at all" is a policy nothing here
    offers and `crom list` already answers.

    `auto()` because these are identities and not text: the flag is the only writer and the
    JSON publishes the verdict crom found, never the policy it found it under. A slug would
    be a published contract that nothing publishes. [LAW:one-source-of-truth]
    """

    REPLACE = auto()
    REPORT = auto()


class Outcome(Enum):
    """Which of `up`'s five endings it reached — the arm that fired, named.

    `up` used to decide and word an ending in the same arm, and that fusion is what kept
    "Relaunched" from ever being printed by a run that stopped nothing. Split across a
    module and a command, the guarantee has to survive the seam, and this is what carries
    it: the arms below decide, the caller renders what they decided, and no caller can
    arrive at `RELAUNCHED` without having gone through the arm that killed. Re-deriving the
    ending from the pair `(found, on_drift)` at the far side would be a second copy of the
    arm map, in another file, free to drift from this one. [LAW:one-source-of-truth]

    A field on `Up` and not a property of it, because it genuinely is not derivable from
    the rest: `REPORTED` and a `RELAUNCHED` whose browser exited between the drift read and
    `chrome.kill` are both a `Drifted` verdict beside an empty `stopped`. The second really
    did replace the browser and has to say so. [LAW:types-are-the-program] the discriminator
    is carried rather than guessed back out of the evidence.

    `auto()` for `OnDrift`'s reason: nothing publishes these. `--json` carries the verdict
    crom found and the PIDs it stopped, and a slug here would be a third published name for
    what those two keys already answer.
    """

    STARTED = auto()
    MATCHED = auto()
    UNMEASURED = auto()
    REPORTED = auto()
    RELAUNCHED = auto()


@dataclass(frozen=True)
class Up:
    """How `up` ended: the arm it reached, the browser it found, what it stopped, and what
    is running now.

    `found` is how the profile stood when `up` reached it, not how it stands now — after a
    relaunch those differ, and the drift that caused the relaunch is the half worth
    keeping, since the other half is `Matches` by construction. It is the same distinction
    `crom up --json` publishes under `found` rather than under `drift`.

    `stopped` is empty unless this call replaced a browser, and it is the only thing
    separating a browser `up` left alone from one it swapped out under the same "running"
    record. Empty is not proof it left one alone, though — see `Outcome`.
    """

    outcome: Outcome
    found: drift.Verdict
    stopped: tuple[int, ...]
    pids: tuple[int, ...]


def start_under_lock(
    profile: ResolvedProfile, log=report.to_stderr
) -> tuple[drift.Verdict, tuple[int, ...]]:
    """Bring a profile up, for a caller already holding `seed.profile_lock`.

    Hands back how the browser stood when this call found it and the PIDs it is running
    under now, rather than reporting either, because its callers say different things about
    the same outcome: `up` converges on the verdict, `crom restart` has just stopped
    whatever was there, and `crom show` mentions a launch only when it had to make one.
    [LAW:effects-at-boundaries] the decision is computed here and rendered at the command.

    A verdict rather than the `started` boolean it replaces, because `drift.Stopped` is
    precisely "nothing was running", which is precisely the case this launches in — so one
    read of the process table decides both halves of the answer. A boolean beside a verdict
    taken from a second `ps` could disagree with it, the browser quitting between the two
    reads being all that takes, and a caller would then hold two answers to "did this call
    start it". [LAW:one-source-of-truth]

    Written for a caller that already holds the lock, not one that takes it, because
    `seed.profile_lock` is `flock` on a fresh descriptor and blocks even within one process
    — so a `restart` built as "call down, then call up" would deadlock if the two halves
    nested, and would drop the lock between them if they did not. The critical section
    belongs to the operation, which is the only participant that knows how much of the work
    has to be indivisible. [LAW:no-ambient-temporal-coupling]

    `log=` rather than a print, the way `session.begin` and `resolve` take one: the seed
    line below is progress on the way to an answer, and a caller that is not the CLI needs
    somewhere other than this process's stderr to put it.
    """
    if not profile.profile_dir.exists():
        # Say so before the copy, not after: a `chrome` seed moves hundreds of
        # megabytes and an unexplained pause looks like a hang.
        rendered = configwrite.render_seed(profile.seed, profile.config_dir)
        log(f"Creating {profile.ref} from seed '{rendered}' …")
    seed.materialize_under_lock(profile)

    running = chrome.find_pids(profile)
    # `running or launch`, and not a branch on the verdict that follows: both read the one
    # `find_pids` above, so "nothing was running" cannot be true for the launch and false
    # for the verdict reported beside it. [LAW:dataflow-not-control-flow]
    return drift.of(profile, running), running or chrome.launch(profile)


def up(profile: ResolvedProfile, on_drift: OnDrift, log=report.to_stderr) -> Up:
    """Bring a profile up on its current config, whatever is running.

    Idempotent: a browser already running what this config resolves to is reported, not
    restarted. One running something else is stopped and started again on the current
    config under `OnDrift.REPLACE`, with what moved named before anything stops. A browser
    crom holds no launch record for is left running and reported as unmeasured — crom
    cannot tell what it was started with, which is no grounds to kill a browser that may
    already match.

    `on_drift` is required rather than defaulted, because it is the one decision a caller
    makes here and the difference between the two values is somebody's open tabs.
    """
    # Seeding, the liveness check, and the launch are one critical section. Split, two
    # concurrent `up` calls both see no running Chrome and both launch against the same
    # profile directory and port — and because Chrome binds the CDP port well before it
    # answers on it, the loser's `_require_port_available` reports the port as held by
    # "another process" when that process is the browser it was asking for. Serialized, the
    # second caller finds the first's Chrome and reports it, which is what `up` has always
    # claimed to do.
    #
    # The replacement below is inside the same hold for the reason `restart`'s two halves
    # are: released in between, a concurrent `up` lands in the gap and starts the browser on
    # the configuration this one is replacing, and an `rm` deletes the directory it is about
    # to launch against. [LAW:no-ambient-temporal-coupling]
    with seed.profile_lock(profile):
        verdict, running = start_under_lock(profile, log=log)
        # The browser this call found is the browser it keeps, and exactly one arm below
        # says otherwise. Stated once as the standing answer rather than repeated in the
        # four that agree with it. [LAW:polishing-by-subtraction]
        stopped, pids = (), running
        # Five endings, five arms, each deciding AND naming one of them: the ending is a
        # function of the pair only because the policy is, and split across two matches —
        # or across a match and an `if` on the policy nested inside an arm — the pair could
        # come apart, leaving an arm that names `RELAUNCHED` with no arm that stopped
        # anything. [LAW:one-source-of-truth]
        #
        # Matched as a pair, the way `drift.of` matches the process table against the
        # record, so the policy arrives as a value the arms read rather than as a branch
        # wrapped around them. Five and not eight because `OnDrift` withholds exactly one
        # action and only one verdict has that action to withhold: the three arms that
        # answer `_` say so themselves. No arm's correctness depends on the order the arms
        # sit in. [LAW:dataflow-not-control-flow]
        #
        # `Unmeasured` is emphatically not a quiet `Drifted`. It says crom cannot tell what
        # the running browser was launched with — no record, or none it can read — which is
        # neither "nothing changed" nor "something did". Relaunching on it would kill a
        # browser that may already match, and take the user's tabs with it, on the evidence
        # of a crom upgrade. It is not a quiet `Matches` either, which is why the CLI says
        # so out loud. [LAW:no-silent-failure]
        match verdict, on_drift:
            case drift.Stopped(), _:
                outcome = Outcome.STARTED
            case drift.Matches(), _:
                outcome = Outcome.MATCHED
            case drift.Unmeasured(), _:
                outcome = Outcome.UNMEASURED
            case drift.Drifted(), OnDrift.REPORT:
                outcome = Outcome.REPORTED
            case drift.Drifted(), OnDrift.REPLACE:
                # Said before acting, so the reason survives a relaunch whose start half
                # fails: that user is left with no browser at all, and an error naming only
                # the start would hide that crom stopped the working one they had. It
                # doubles as the progress line for the pause while Chrome comes back.
                log(f"{profile.ref} {verdict.finding}; replacing it …")
                stopped = chrome.kill(profile)
                # `kill` returns only once the profile holds neither process nor CDP port,
                # so the start can follow it directly rather than racing its own teardown.
                # Its verdict is discarded because it can only be `Stopped`: nothing is
                # running by construction, and what a reader wants named is the drift that
                # sent us here, which the outer `verdict` still holds.
                _, pids = start_under_lock(profile, log=log)
                outcome = Outcome.RELAUNCHED

    return Up(outcome, verdict, stopped, pids)


def down(profile: ResolvedProfile) -> tuple[int, ...]:
    """Stop a running profile, handing back the PIDs it stopped — empty if it was not up.

    Under the same lock `up` and `rm` hold. `up` keeps it from before seeding until CDP
    answers, but a launched process is visible to `chrome.scan` as soon as `Popen` returns
    — so an unlocked `down` could kill a Chrome that was still initialising a freshly-copied
    user-data-dir, which is precisely the state that is not safe to interrupt, and leave
    `up` reporting a readiness timeout for a browser someone else killed.
    [LAW:no-ambient-temporal-coupling] a lock one participant ignores is not serialising
    anything; `down` waiting for an in-flight `up` is also what makes "down returned" mean
    "it is stopped" rather than "I killed what I happened to see".

    No `log=`, because unlike every other operation here this one has nothing to say on the
    way: `chrome.kill` either establishes the stop or raises, and what it stopped is the
    return value. A parameter offered and never used would advertise a channel that carries
    nothing. [LAW:polishing-by-subtraction]
    """
    with seed.profile_lock(profile):
        return chrome.kill(profile)
