"""crom's mutating operations, performed by anything — not only by a command body.

The reading half of crom has been a library for a while: `resolve` returns typed values,
takes a `log=`, and imports no click. This is the mutating half's home — functions that do
the work and hand back what happened, leaving the sentence a user reads to be written
somewhere else.

They take what is already resolved: a `ResolvedProfile`, never a `Session` and never a ref
string, because parsing a reference is the caller's half of the job. `add` is the one
exception the rule implies rather than admits — it is what *creates* a declaration for the
others to resolve, so it takes the scope and the request instead. A future operation that
mutates an existing profile takes the resolved profile; do not read `add`'s signature as
licence otherwise.

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

import shutil
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

from . import chrome, config, configwrite, drift, flags, registry, report, seed
from . import resolve as resolver
from .model import (
    DEFAULT_SEED,
    DEFAULTS_STANZA,
    USER_NAMESPACE,
    Layer,
    ProfileSpec,
    Reason,
    ResolvedProfile,
    Scope,
    slug_for,
    validate_name,
)


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


class Declaration(Enum):
    """Whether this call wrote the declaration or found the file already holding one.

    Shared by `add` and `init`, which are the same two endings about different subjects:
    a profile's stanza, and the project config that names the namespace. Both are
    idempotent, both hand back a boolean from `configwrite`, and both spend it on one
    choice of verb. Two enums differing only in which subject their members were named
    after is what [LAW:one-type-per-behavior] forbids — the subject is already carried by
    which operation the caller invoked, so it is not a second thing for the value to say.

    An enum and not the `written` boolean `configwrite.ensure_profile` hands back, for the
    reason `OnDrift` is one: a bare bool crossing this seam asks every reader to hold which
    way round it runs, at the one place in crom that decides whether a user is told their
    config changed. Named, the arm says which it is and cannot be read backwards.
    [LAW:types-are-the-program]

    Carried on the result rather than derived from it, for `Outcome`'s reason at a
    different fact: both endings leave the file declaring the same profile, so the
    declaration `add` returns is identical either way and the only witness to which
    happened is the write that did or did not occur. A caller comparing its own picture of
    the scope against the file would be reading a race rather than an answer — another
    `crom add` can declare the name in between, which is the case the read-back inside
    `add` exists for.

    Two values, and there is no third for the config `add` had to recreate along the way:
    that is something that happened on the way to an answer, not one of the answers, and
    it goes out through `log=` where `up`'s seeding line goes.

    `auto()` for `OnDrift`'s reason: `crom add` has no `--json`, so nothing publishes
    these, and a slug would be a published name with no reader. [LAW:one-source-of-truth]
    """

    CREATED = auto()
    ALREADY_PRESENT = auto()


@dataclass(frozen=True)
class Add:
    """What `add` declared: which ending it reached, the profile as the config now resolves
    it, and the file that declares it.

    `profile` comes from the file's declaration and never from the caller's request, so on
    the converging path it describes what the project actually says rather than what this
    call asked for. The two differ exactly when this call lost a race for the name, which
    is the whole reason `add` re-reads before resolving.

    `target` is `profile.source` with the optionality gone. Resolution allows a profile
    with no file behind it — the fileless `user` scope — and `add` has just written to one,
    so the file is a fact by the time this record exists even though the type it travelled
    through cannot say so. Narrowed here, at the one place that knows, rather than left as
    a None every renderer would have to guard against and none could ever see.
    [LAW:parse-dont-validate]
    """

    outcome: Declaration
    profile: ResolvedProfile
    target: Path


@dataclass(frozen=True)
class Init:
    """What a project is configured as: which ending `init` reached, the file that
    configures it, and the two facts that file states.

    `namespace` and `seed` are read back out of the file rather than echoed from the
    request, so on the converging path they are what the project actually declares and
    not what this call would have written. The two differ exactly when the project was
    already initialised — which is the case a caller most needs told apart, and the one
    a request-shaped record could not tell it. [FRAMING:representation]

    `seed` is the file's own spelling of the seed, as text, because that is the only
    form both readers need: it is compared against the rendered request and printed. A
    parsed `Seed` would be the stronger type in general and is the wrong one here —
    `configwrite.value_at` deliberately returns the raw TOML value so that convergence
    does not depend on the rest of the file parsing, and re-parsing it here would put
    that dependence back. An absent `[defaults].seed` is not missing: `config.parse`
    reads that absence as `DEFAULT_SEED`, so it renders as the answer the next command
    will act on.
    """

    outcome: Declaration
    target: Path
    namespace: str
    seed: str


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

def reject_restatement(
    subject: str, facts: tuple[tuple[str, str | None, str | None], ...], remedy: str
) -> None:
    """Refuse to call a request already-done when it asks for something else.

    crom converges: `crom init` in an initialised project and `crom add` of a profile that
    already exists both report what is there and exit 0, because the state the user asked
    for is the state the project is in. That is only honest while the existing thing *is*
    what was asked for — accepting `crom add ci --port 9500` against a `ci` on 9401 and
    reporting success would be crom claiming work it did not do, and the user finding out
    at launch. [LAW:no-silent-failure] convergence reports a satisfied request; it does not
    swallow an unsatisfiable one.

    Each fact is `(label, declared, asked)` in the config file's own vocabulary, so the
    message reads back in the spelling the user typed and the file holds. An `asked` of
    None is a fact the user did not state and therefore cannot contradict: `crom add ci`
    with no options asks only that `ci` exist. That convention is `ProfileSpec`'s — an
    absent `seed` means "inherit `[defaults]`", not "seed is nothing" — so statedness is
    read off the type rather than tracked alongside it. [LAW:types-are-the-program]
    """
    # The contradictions are found once and then rendered twice — a line each for the
    # reader, their labels alone for the script. Filtering a second time to name them would
    # be a second copy of the rule deciding what "differs" means. [LAW:one-source-of-truth]
    differing = tuple(
        (label, declared, asked)
        for label, declared, asked in facts
        if asked is not None and declared != asked
    )
    if differing:
        lines = (
            f"  {label}: declared {declared or '(unset)'}, you asked for {asked}"
            for label, declared, asked in differing
        )
        raise Reason.DECLARATION_DIFFERS.error(
            "\n".join((subject, *lines, remedy)),
            settings=tuple(label for label, _, _ in differing),
        )


def _effective_flags(scope: Scope, *stanzas: Layer) -> str:
    """The flags a profile declaring `stanzas` would have, as one comparable fact.

    Through `flags.compose`, the same call `resolve_spec` makes, so a profile's
    `--disable-blink-features` and `[defaults]`'s are seen as two answers to one question
    rather than two unrelated strings. [LAW:one-source-of-truth]

    The launch policy is deliberately not a layer here, though it is one at launch. The
    doctrine that makes an inherited flag *already* what the user asked for is about the
    config file every checkout shares: a `[defaults]` flag reaches the profile on every
    machine that reads the file, so restating it asks for nothing new. crom's launch
    policy is not in the file at all — it is crom's own behavior, which a crom upgrade
    can change — so `crom add ci --flag --no-pings` is asking for something this config
    does not yet say, exactly as `--port` is judged on the pin rather than on the port
    crom happened to assign.

    Whole values, not the part the two sides differ on. `reject_restatement` renders
    every fact as "declared X, you asked for Y" and spells an empty X `(unset)` — a
    vocabulary of full values, which a difference does not speak: a profile declaring
    `--a=1` asked to also take `--b=2` has nothing unique on its declared side, and
    reported as a difference that read `declared (unset)`, flatly denying the `--a=1`
    that is right there in the file. [FRAMING:representation] the fact has one rendering,
    and it is the one the template promises.
    """
    return " ".join(
        sorted(flags.render(flags.compose(scope.default_flags, *stanzas).flags))
    )


def add(scope: Scope, spec: ProfileSpec, log=report.to_stderr) -> Add:
    """Declare a profile in the config governing `scope`, and resolve what that config says.

    Idempotent, the way `up` is: a config already declaring the name is reported rather
    than rewritten. Convergence stops at the point it would become a lie — a request that
    asks the file for something it does not say is refused, not reported as done, because
    the alternative is crom claiming work it did not do and the user finding out at launch.

    Takes a `Scope` and a `ProfileSpec` where the operations above take a
    `ResolvedProfile`, and not for want of symmetry: this is the operation that creates the
    declaration the others resolve from, so there is nothing resolved to hand it. What it
    does keep is the half of that convention which carries the weight — the parsing is the
    caller's, and what arrives here is a request already in crom's own vocabulary.

    The file is derived from `scope` rather than accepted beside it, so no caller can pair
    a scope with another scope's config and declare a profile into a file that does not
    describe it. The same mismatch `resolve_spec` refuses to let a caller express, and the
    same answer: both halves of the identity come from one argument.
    [LAW:types-are-the-program]
    """
    target = config.write_target(scope)
    # `_declare` creates the file when it is missing, and the header it would write is
    # the *user* scope's — which carries no `namespace` key, because only the project
    # template has one. So recreating a vanished project config from it produces a file
    # the parser rejects wholesale. The scope was read at discovery time and the file can
    # be gone by now (a `git clean`, another agent resetting the workspace), which used to
    # end in "Run `crom init` to recreate it" — a command crom is holding every argument
    # for. `write_default` writes exactly what that `crom init` would have, from the scope
    # already in hand, and is a no-op when the file is still there.
    header = configwrite.USER_CONFIG_HEADER if scope.is_user else ""
    if configwrite.write_default(
        target,
        namespace=None if scope.is_user else scope.namespace,
        seed=scope.default_seed,
    ):
        log(f"Recreated {target}, which had been removed since crom read it")

    # What to declare if this name is free. Only a proposal: on the path where the config
    # already declares the name, `ensure_profile` writes nothing and this value is
    # discarded below for the declaration the file actually holds. It is deliberately not
    # called `declared` — naming this caller's request after the file's contents is what
    # let the two be confused on the race path this operation has to survive.
    proposed = scope.profiles.get(spec.name, spec)

    # Before the write, because `parse` refuses a file that pins one port twice
    # *wholesale*: a declaration rejected only after it landed would break every command
    # in the project — `crom rm` included — on the very file the user needs crom to
    # repair.
    config.reject_duplicate_ports({**scope.profiles, spec.name: proposed}, target)

    # The converging write, so a name another `crom add` declared between this command's
    # read of `scope` and this line is reported as declared rather than as a collision —
    # and the port is left alone, because it belongs to the ref, which the winner and this
    # caller share. The raising twin used to make that race an exit-4 and needed a
    # dedicated handler to keep from stripping the winner's port.
    written = configwrite.ensure_profile(target, proposed, header=header)

    # The file, re-read, because this is the first moment it is known to declare the name
    # — and `scope` is only this process's picture of it from discovery time. A `crom add`
    # that lost the race for the name holds a scope that never saw the winner's
    # declaration, so `proposed` above fell back to this caller's own request; comparing
    # and reporting from that compared the request against itself and stated the loser's
    # guess as the project's fact. `crom add ci --seed fresh` exited 0 reporting
    # "seed fresh" over a file that gives `ci` the user's real Chrome profile — the
    # find-out-at-launch failure the refusal below exists to prevent, reached by the one
    # path that skipped it. [LAW:one-source-of-truth] the file is what the project
    # declares. [LAW:dataflow-not-control-flow] the read is unconditional: on the path
    # that just wrote, it reads back exactly what this command declared, so one sequence
    # serves both and only the values differ.
    scope = config.load_file(target, namespace=USER_NAMESPACE if scope.is_user else None)
    declared = scope.profiles.get(spec.name)
    if declared is None:
        # `ensure_profile` returning at all means the name is declared, so reaching here
        # takes a concurrent `crom rm` — or a `git checkout` over the file — landing in
        # between. Said as a `CromError` rather than left to a `KeyError`, which would
        # leave the CLI's exit-code contract as a traceback — this module raises `CromError`
        # so that the boundary above it can keep answering in codes.
        # [LAW:no-silent-failure]
        raise Reason.PROFILE_VANISHED.error(
            f"{target}: profile '{spec.name}' was removed while crom was declaring it"
        )

    # Resolution comes after the write and reads the file's declaration, not this caller's
    # request. `port_for` writes a reservation the moment it is reached, so resolving the
    # request first was what let `crom add ci --port 9500` move a live `ci` onto 9500 on
    # its way to refusing it — a failed command silently repointing a live profile.
    # Resolving the real declaration reserves that profile's own port, which no refusal
    # below has to take back, and it is where the *effective* seed comes from:
    # `[defaults]` inheritance lives in `resolve_spec`, and re-deriving it here to compare
    # against would be a second copy of that rule. [LAW:one-source-of-truth]
    profile = resolver.resolve_spec(scope, declared)
    reject_restatement(
        f"{target}: profile '{spec.name}' is already declared, and this asks to change it:",
        (
            (
                "seed",
                configwrite.render_seed(profile.seed, scope.config_dir),
                None if spec.seed is None else configwrite.render_seed(spec.seed, scope.config_dir),
            ),
            # The pin, not the port the profile is on — the one fact here that `[defaults]`
            # cannot supply. A seed or a flag inherited from `[defaults]` reaches the
            # profile on every machine that checks the file out, so a profile whose
            # effective seed is already `fresh` *is* the profile `--seed fresh` asked for.
            # A port crom assigned is remembered in a machine-local ledger and nowhere in
            # the file, so `--port 9224` against a profile crom happens to have put on 9224
            # is asking for something the config does not yet promise. Comparing the
            # effective port would have let that through as already-done.
            (
                "port",
                str(declared.port)
                if declared.port is not None
                else f"(unpinned — crom assigned {profile.port})",
                None if spec.port is None else str(spec.port),
            ),
            # An empty tuple is the only way `--flag` can go unmentioned, so emptiness is
            # statedness here — unlike `seed` and `port`, which have a real `None`.
            #
            # Effective, for the reason the seed fact is effective: a flag reaching the
            # profile from `[defaults]` reaches it on every machine that checks the file
            # out, so a profile already running `--headless` *is* the profile
            # `--flag --headless` asked for. This comparison used to build its own
            # set-union of the defaults and the declared flags, which was a second,
            # independent statement of what flags a profile has — and it disagreed with
            # the launcher the moment either layer overrode a switch rather than adding
            # one. [LAW:one-source-of-truth]
            (
                "flags",
                # Both sides are resolved under the *same* drop policy — the declaration's
                # — because `--flag` cannot express `drop_flags`, so a request is silent
                # about drops rather than asserting there are none. Composed beside the
                # declaration instead, the asked side kept a `[defaults]` switch the
                # profile drops, and a restatement identical to the file exited 4 citing a
                # flag the user never typed.
                #
                # The drops arrive as their own layer under the request's flags, which is
                # the layering rule applied to the two speakers: the file's policy governs
                # the switches the command is silent about, and the command answers for the
                # ones it names. So asking for a switch the profile drops still differs
                # from the declaration and is still refused — that is a real disagreement
                # between the command and the file — while asking for nothing new converges.
                #
                # Not composed *on top of* the whole declaration, which would have hidden
                # the other half of this comparison's job: a request that omits a declared
                # flag asks for a profile without it, and a superset laid over the
                # declaration can never differ from it.
                _effective_flags(scope, declared.flags),
                (
                    _effective_flags(
                        scope,
                        # The declaration's own drops, so named after the declaration: this
                        # layer is the file's drop policy on loan to the request, not a
                        # stanza of its own. [LAW:one-source-of-truth]
                        Layer(drops=declared.flags.drops, origin=declared.flags.origin),
                        spec.flags,
                    )
                    if spec.flags.sets
                    else None
                ),
            ),
        ),
        f"Edit {target} directly, or `crom rm {profile.ref}` and add it again.",
    )

    # No reservation can be stranded by a failed write, so nothing here has to release
    # one: `resolve_spec` runs only after `ensure_profile` returned, and everything in it
    # that can fail runs before `port_for` reserves. A write that raises therefore raises
    # before a port was ever claimed, which is what retired the `registry.forget` handler
    # this ordering used to need.
    return Add(Declaration.CREATED if written else Declaration.ALREADY_PRESENT, profile, target)


def delete_directory(directory: Path, retry: str) -> None:
    """Remove one directory crom owns, as a `CromError` rather than a traceback.

    `shutil.rmtree` raises a bare `OSError` — `FileNotFoundError` for an entry that
    vanishes mid-walk, `ENOTEMPTY` for one that appears — and the path alone does not say
    what is left to try. Both callers meet that same race for the same reason: `rm`
    deletes a directory a Chrome helper outliving `chrome.kill` can still be writing to,
    and `crom clean` deletes one a seed that is still running may still be filling.

    The retry sentence is the caller's, because only the caller knows what it left
    reachable. `rm` deletes before it undeclares precisely so that a retry exists;
    `clean` names a path that stays exactly as `crom doctor` prints it.

    One reason for both, and it keeps `profile_dir_undeletable` as its published spelling:
    a staging directory is a profile directory that never finished being built, the failure
    is the same one, and the next move — fix what the sentence names, run the command
    again — does not divide. A slug earns its place by separating next moves.
    """
    try:
        shutil.rmtree(directory)
    except OSError as e:
        raise Reason.PROFILE_DIR_UNDELETABLE.error(
            f"could not delete {directory}: {e}\n{retry}"
        ) from e


def rm(
    profile: ResolvedProfile, ambient: Scope, *, keep_data: bool, log=report.to_stderr
) -> tuple[int, ...]:
    """Take a profile away: stop it, delete its data, release its port, undeclare it.

    Hands back the PIDs it stopped — empty if nothing was running — because that is the
    most surprising thing this does and the caller is the one with somewhere to say it.

    Converges rather than refusing: this used to reject a running profile and tell the
    user to run `crom down` first, which made the caller responsible for establishing a
    state `rm` needs and can reach on its own, under the very lock it already takes.
    Exporting it as a two-command ritual left an orphan-shaped hole in the other
    direction too, since `keep_data` was the one path that refused without ever
    explaining that the browser it left running was about to lose its declaration.
    [LAW:no-ambient-temporal-coupling] the phase transition is this operation's to own.

    The scope is derived from the profile's own namespace rather than accepted beside it,
    so no caller can pair a profile with another scope's config and undeclare the wrong
    file. Both halves of the identity come from one argument, the same closure `add` makes
    over `write_target`. [LAW:types-are-the-program]

    `keep_data` is required and keyword-only rather than an `OnDrift`-shaped enum, and
    that is a judgement rather than an oversight: what `OnDrift` fixes is a *negation*
    crossing the seam — `--no-restart` read backwards kills a browser someone asked crom
    to spare — and `--keep-data` states what it does. Required, because the difference
    between its values is a user's logins, cookies and history; keyword-only, because a
    bare `True` sitting third in a call reads as nothing at all. Do not replace it with an
    enum for symmetry with `up`.
    """
    scope = resolver.scope_for(profile.ref.namespace, ambient, log=log)
    # Stopping and deleting are one critical section, for the same reason `up` holds this
    # lock across its own check-and-launch: a concurrent `crom up` can seed and launch
    # Chrome in the window between them, and this would otherwise delete a live browser's
    # user-data-dir out from under it — a process crom can no longer find or stop, writing
    # into a directory that no longer exists.
    #
    # The lock starts here and not before, so a caller may take as long as it likes asking
    # its user whether to go ahead: holding it across an interactive prompt would block
    # every other crom process for as long as the human takes to answer. Which is why
    # `kill` runs unconditionally rather than under a liveness read taken before that
    # question — such a read is stale by construction, and a Chrome started while the
    # question was on screen must not survive the answer.
    # [LAW:dataflow-not-control-flow] `chrome.kill` returns an empty tuple when there was
    # nothing to stop, so the same operation runs every time and only its result varies.
    with seed.profile_lock(profile):
        stopped = chrome.kill(profile)
        # Data first, while the profile is still fully declared. `rm` resolves by name
        # before it does anything, so a delete that failed partway after the declaration
        # was gone left a half-removed directory belonging to a profile no command could
        # name — unreachable by the `crom rm` that would have retried it. Deleting first
        # inverts that: a failure here leaves everything nameable and the command
        # repeatable, and a failure *after* a successful delete leaves a declared profile
        # whose directory the next `crom up` simply re-seeds. Same principle the comment
        # below applies to the other two steps — between interruptible steps, take the
        # one whose failure is recoverable.
        if not keep_data and profile.profile_dir.exists():
            delete_directory(
                profile.profile_dir,
                f"'{profile.ref}' is still declared — run `crom rm {profile.ref}` again.",
            )
        # Release the reservation before removing the declaration, not after. Both
        # orderings can be interrupted, but they strand different things: undeclaring
        # first leaves a port held by a profile no longer nameable, so no command can
        # reach it to retry. Releasing first leaves a declared profile without a
        # reservation, which the next resolve heals by assigning one. Between two
        # interruptible steps, take the one whose failure is recoverable.
        registry.forget(str(profile.ref))
        configwrite.remove_profile(config.write_target(scope), profile.ref.name)
    return stopped


def init(here: Path, namespace: str | None, seed_text: str | None) -> Init:
    """Give a directory its own namespace by writing a project config into it.

    Idempotent, the way `add` is: a directory that already has a config is reported
    rather than rewritten, and a request asking that file for something it does not say
    is refused rather than reported as done.

    Takes the directory and nothing derived from it, so every fact about the config file
    — where it goes, whether the project is already initialised, and what a relative
    `--seed` is anchored on — comes from one argument and from one look at the disk.
    `config.init_target` reads the filesystem, so a caller that computed the target and
    passed it alongside `here` would be handing over a second, older answer: a `crom
    init` racing this one can create `.crom/config.toml` in between, and the seed would
    then be rendered against a directory this call is not writing to — `render_seed`
    falling back to this machine's absolute path in a file meant to be committed. Passing
    the target instead of `here` closes the race and opens a worse hole, since a target
    outside `here` becomes expressible. [LAW:types-are-the-program]

    Which is why the seed arrives as *text* where `add` takes a parsed `ProfileSpec`. It
    is still crom's own vocabulary and not argv — `fresh`, `chrome:<Profile>`, `./path`
    is the spelling the config file itself holds, and `config.parse_seed` is the one
    border both readers cross. What a caller cannot supply is the anchor a relative path
    is resolved against, because that is the target's own directory. [LAW:single-enforcer]

    `namespace` is what the user *stated*, `None` when they stated nothing, and that
    absence is load-bearing rather than a convenience: it is what makes a bare `crom
    init` in an initialised project a no-op instead of a rename. The fallback below is
    crom's guess, and a guess must not be able to contradict a name the project chose.
    [LAW:no-silent-failure]

    No `log=`: unlike `add`, which converges a vanished config on its way past, this has
    nothing to say before its answer — the one write it makes *is* the answer. A
    parameter offered and never used would advertise a channel that carries nothing, the
    reason `down` declines one too. [LAW:polishing-by-subtraction]
    """
    target = config.init_target(here)
    # Everything downstream anchors on the config's own directory rather than on `here`:
    # a relative seed path is parsed and rendered against it everywhere else, and
    # `write_default` renders it back with `render_seed(seed, path.parent)`. The two agree
    # today only because `PROJECT_CONFIG_CANDIDATES[0]` is the bare `.crom.toml`, whose
    # parent *is* `here`; under the `.crom/config.toml` candidate they diverge, and
    # `--seed ./fixtures` would parse to `here/fixtures`, fail `relative_to(here/'.crom')`,
    # and be written as this machine's absolute path into a file meant to be committed.
    # Taken from the same `target` the write uses, so no interleaving can put the parse
    # and the write in different directories. [LAW:one-source-of-truth]
    base = target.parent
    # The one read of "is this project already initialised". `init_target` answers with
    # the file it found, so asking the returned path is asking about that same file
    # rather than repeating its search under a second name.
    existing = target.is_file()

    # crom's guess, and the value that reaches disk when the user named nothing. Derived
    # from the directory through `slug_for`, which is the one rule for turning arbitrary
    # text into a name — shared with `config`'s repair path so that a reset config cannot
    # claim a different namespace from the one `crom init` gave the project.
    chosen = validate_name("namespace", namespace or slug_for(here.name))

    # The namespace this call *claims*: what the user typed, else crom's guess — and
    # nothing at all when a config already exists, because then the namespace is the
    # file's and the guess is discarded a few lines below without ever reaching disk
    # (`write_default` creates with `O_CREAT | O_EXCL`, so it cannot overwrite the name
    # the project chose). Refusing on the discarded guess is precisely the guess
    # contradicting that name: in a directory named `user`, `crom init myproj` wrote
    # `namespace = "myproj"` and a bare `crom init` afterwards exited 4 saying `"user"` is
    # reserved — about a value the user never typed and the file never held.
    # [LAW:dataflow-not-control-flow] the refusal always runs; only the value it reads
    # differs. A reserved namespace *in the file* is not re-litigated here either —
    # `config.parse` refuses that at the read boundary for every command.
    # [LAW:single-enforcer]
    claimed = namespace or (None if existing else chosen)
    if claimed == USER_NAMESPACE:
        raise Reason.NAMESPACE_RESERVED.error(
            f'"{USER_NAMESPACE}" is reserved; pass a different namespace'
        )

    # After the namespace is settled and not before, because the order is what a script
    # branching on the exit code sees: `crom init user --seed chorme` names the reserved
    # namespace (a conflict) rather than the unrecognised seed (a failure), and the two
    # answer on different codes. Parsed here rather than by the caller for the reason the
    # docstring gives — `base` is this function's to derive. [LAW:parse-dont-validate] the
    # border still runs before the write, so a misspelt seed cannot reach the file.
    stated_seed = (
        None if seed_text is None else config.parse_seed(seed_text, DEFAULTS_STANZA, target, base)
    )

    # The refusal here is the kernel's `O_CREAT | O_EXCL`, not a check of ours, so two
    # `crom init` calls racing in one directory produce one file rather than one clobbering
    # the other. What changes is only what the loser does with the answer: it reads the
    # winner's file back below and reports it, since a project that has the config it was
    # asked for has had the request met.
    wrote = configwrite.write_default(
        target,
        namespace=chosen,
        seed=DEFAULT_SEED if stated_seed is None else stated_seed,
    )

    # Read back rather than echoed from the values above, because on the converging path
    # those are what crom *would* have written and the file is what the project actually
    # declares — and reporting a guessed namespace as though it were the project's would be
    # the same lie the comment above refuses to write. On the path that just created the
    # file the two are identical, so one read serves both and doubles as a check on the
    # write. [FRAMING:representation]
    declared_namespace = configwrite.value_at(target, "namespace")
    # Reading a fact obliges this operation to handle every shape the file can hold it in.
    # A hand-written `.crom.toml` with no `namespace`, or one holding a number, is a file
    # that exists without configuring anything — so converging on it would report a
    # namespace of `None` and send the user to a `crom up` that `config.parse` is about to
    # refuse. Said here instead, naming the fix. [LAW:parse-dont-validate] the full
    # diagnosis stays `config.parse`'s; this is only the checkpoint for the value this
    # record is about to state as fact.
    if not isinstance(declared_namespace, str):
        raise Reason.CONFIG_INVALID.error(
            f"{target} exists but declares no usable `namespace` "
            f"({declared_namespace!r}), so it does not configure this project.\n"
            f'Add namespace = "{chosen}" to it, or delete it and run '
            f"`crom init` again."
        )

    declared = configwrite.value_at(target, "defaults", "seed")
    # Narrowed to text once, here, rather than left raw for two readers to each coerce:
    # the comparison below and the caller's rendering are the same fact and must not be
    # two spellings of it. [LAW:parse-dont-validate]
    declared_seed = (
        configwrite.render_seed(DEFAULT_SEED, base) if declared is None else str(declared)
    )

    reject_restatement(
        f"{target} already configures this project, and this asks to change it:",
        (
            ("namespace", declared_namespace, namespace),
            (
                "seed",
                declared_seed,
                None if stated_seed is None else configwrite.render_seed(stated_seed, base),
            ),
        ),
        f"Edit {target} directly — crom writes a project config once and leaves it "
        f"yours after that. Changing the namespace also moves this project's ports and "
        f"profile directories, which is why crom will not do it for you.",
    )

    return Init(
        Declaration.CREATED if wrote else Declaration.ALREADY_PRESENT,
        target,
        declared_namespace,
        declared_seed,
    )
