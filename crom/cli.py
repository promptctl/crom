"""Maps commands onto crom's core and renders the result for a human or a machine.

This is the outer boundary: below it a failure is either a `CromError` or the operating
system refusing; everything user-visible — exit codes, stderr, JSON shape — is decided
here.

[LAW:effects-at-boundaries] The core computes descriptions; this layer performs and
prints them. [CLI binding] stdout carries the answer, stderr carries diagnostics, and
exit codes are a contract a script can branch on:

    0  success            3  no such profile / namespace / config
    1  failure            4  port or declaration conflict
    2  usage error (click's own)

Four codes is as fine as a numeric contract can afford to be, so every failure also
carries a reason slug — one word naming what actually went wrong, enumerated in
`model.Reason`.
"""

import errno
import json
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import click

from . import (
    chrome,
    config,
    configwrite,
    doctor,
    drift,
    mcp,
    operations,
    reclaim,
    registry,
    report,
    resolve as resolver,
    seed,
    window,
)
from .config import discover, load_user_scope, parse_layer, parse_port, parse_seed
from .model import (
    DEFAULTS_STANZA,
    Conflict,
    CromError,
    Emitted,
    FailedProfile,
    Fields,
    Flag,
    NotFound,
    ProfileRef,
    ProfileSpec,
    ResolvedProfile,
    Resolution,
    Scope,
    profile_stanza,
    validate_name,
)
from .session import Session

EXIT_FAILURE = 1
EXIT_NOT_FOUND = 3
EXIT_CONFLICT = 4


class _Detail(NamedTuple):
    """Everything a failure can say past its code and kind: the reason slug, and the
    values crom looked up on the way to refusing."""

    reason: str | None
    fields: Fields


def _crom_detail(error: Exception) -> _Detail:
    """crom's own refusals arrive carrying both, and for the same reason: a `CromError`
    cannot be built without a reason, and `Reason.error` fills the fields from what that
    reason declares it carries. Neither is assembled here."""
    return _Detail(error.reason.value, error.fields)


def _errno_detail(error: Exception) -> _Detail:
    """An OS refusal arrives carrying a slug already, and the OS owns how it is spelled.
    `ENOENT` is a name every language's errno table can already look up, so crom
    inventing a parallel spelling beside it would be a second source of truth for a fact
    it does not own. [LAW:one-source-of-truth]

    `None` where there is no errno — `shutil.Error` is an `OSError` carrying none — since
    at that point crom knows nothing finer than `kind` has already said. `null` is the
    one answer that cannot be mistaken for a slug, where a stand-in like `"unknown"`
    would be an answer-shaped void: a caller could branch on it as though crom had
    identified something. [LAW:parse-dont-validate]

    The path comes from the same place for the same reason. This arm's message is
    `<path>: <reason>` joined below, so without the field a caller would be splitting
    crom's sentence on a colon to learn which file the OS refused — and paths contain
    colons. `filename` is what the OS was asked about rather than what the caller typed,
    which is exactly what a field is for; `None` where it named none.

    Stringified here because `open(Path(...))` hands back the `Path` object it was given,
    and the envelope is JSON. That is this boundary's own job — rendering — and not one a
    raise site could have done, since there is no raise site: the OS built this one.
    """
    return _Detail(
        errno.errorcode.get(error.errno),
        {"path": None if error.filename is None else str(error.filename)},
    )


class _Answer(NamedTuple):
    """What crom answers for one class of error: the code a script branches on, the kind
    that says what happened when the code cannot, and where to read the detail that says
    it finer than either."""

    error: type[Exception]
    code: int
    kind: str
    detail: Callable[[Exception], _Detail]


# Every exception this CLI answers for, and the only place a code or a kind is assigned.
# [LAW:dataflow-not-control-flow] the mapping is a table consulted once, not a chain of
# except clauses repeated per command.
#
# `kind` is not `code` spelled twice. crom refusing and the operating system refusing are
# both exit 1, and `kind` is the only field that separates them — which matters, because
# one means the user's request was wrong and the other means the machine got in the way.
# Exit codes are a published four-value vocabulary that cannot grow without breaking the
# contract, and `kind` sorts the same failures four ways again. Neither can tell a port
# held by a stranger from a Chrome that will not run from a Chrome that died on the way
# up, and those are three different next moves. `reason` is where that lives — a column
# here because a class still decides which vocabulary a reason is drawn from: crom's own
# enumerated one in `model.Reason`, or the errno names the OS already publishes. That is
# what keeps `kind` from being `reason` blurred: it names which of the two you are
# holding.
#
# One column and not two, though it answers with two values. The reason and the fields are
# drawn from the same vocabulary — crom's raise site filled both, or the OS did — so a row
# choosing them separately could pair crom's slug with the OS's fields. Returned together,
# that mispairing has nowhere to live. [LAW:one-source-of-truth]
_ANSWERS = (
    _Answer(NotFound, EXIT_NOT_FOUND, "not_found", _crom_detail),
    _Answer(Conflict, EXIT_CONFLICT, "conflict", _crom_detail),
    _Answer(CromError, EXIT_FAILURE, "failure", _crom_detail),
    _Answer(OSError, EXIT_FAILURE, "os_error", _errno_detail),
)

# Where a parsed `--json` is recorded, under the key `_json_option` writes and `_answer`
# reads. `Context.meta` and not the `Session` on `ctx.obj`: meta is one dict shared by
# click's whole context tree, so it is readable from the group without depending on the
# group callback having already built a session.
_JSON_REQUESTED = "crom.json_requested"


def _json_option(command):
    """The `--json` flag: one declaration, which is also how the boundary comes to know.

    `CromGroup.invoke` sees an empty `ctx.params`, because the flag belongs to the
    subcommand and click hands a parent no handle on its child's context. So the value
    has to be left somewhere the boundary can find it, and click runs this callback while
    parsing — meaning the option and the recording are a single declaration.
    [LAW:single-enforcer] the alternative was every `--json` command assigning the flag
    onto its session on the way past: N places to enforce one rule, which is a rule the
    next command gets written without. Here there is nothing to remember, because there
    is nothing to do.

    One declaration also means one help string. Five of the six commands carrying this
    flag declared it with none, so `crom up --help` documented `--json` and
    `crom list --help` left a reader to guess. [LAW:one-source-of-truth]
    """

    def remember(ctx: click.Context, param: click.Parameter, value: bool) -> bool:
        ctx.meta[_JSON_REQUESTED] = value
        return value

    return click.option(
        "--json", "as_json", is_flag=True, callback=remember, help="Emit the result as JSON."
    )(command)


def _json_text(payload) -> str:
    """How crom spells JSON, for the one reader who cannot tell a result from a failure
    until it has parsed one: both are the same document format, indented the same way."""
    return json.dumps(payload, indent=2)


class _Failure(click.ClickException):
    def __init__(self, message: str, exit_code: int):
        super().__init__(message)
        self.exit_code = exit_code


def _answer(ctx: click.Context, error: Exception, message: str) -> _Failure:
    """crom's whole answer to a failed command: the machine's copy on stdout when one was
    asked for, and the exception carrying the human's copy and the exit code.

    [LAW:one-source-of-truth] the sentence click prints to stderr and the `message` in the
    envelope are one string rendered twice, not two wordings that can drift apart. stderr
    is untouched either way — the flag adds the machine's copy, it never trades the
    human's away.

    The envelope is written here rather than from the exception's own `show` so that it
    stays under the broken-pipe rule. click calls `show` from its own `except
    ClickException` arm, whose sibling `except OSError` — the one installing
    `PacifyFlushWrapper` for `errno.EPIPE` — cannot catch what that arm raises. Measured:
    `crom up nosuchns/x --json` into a closed pipe returned exit 120 and a traceback,
    where the same command without the flag exited 3 in silence. Written here it
    propagates out of `invoke` into the region click does protect, so a reader that left
    gets the quiet ending every other broken pipe gets. [LAW:single-enforcer] the rule
    keeps one home rather than growing a second guard beside it.
    """
    answer = next(a for a in _ANSWERS if isinstance(error, a.error))
    # Absent only where the parse itself failed, since nothing crom does runs ahead of
    # it: a malformed command line reaches the user as prose because the flag on it was
    # never understood either. The envelope answers for a command crom has understood.
    if ctx.meta.get(_JSON_REQUESTED, False):
        detail = answer.detail(error)
        envelope = {
            "code": answer.code,
            "kind": answer.kind,
            "reason": detail.reason,
            # Always present, `{}` where the reason declares no fields, so a caller reads
            # one shape rather than testing for the key first.
            # [LAW:dataflow-not-control-flow]
            "fields": detail.fields,
            "message": message,
        }
        click.echo(_json_text({"error": envelope}))
    return _Failure(message, answer.code)


# How `crom --help` groups its commands, as data rather than as prose that has to be
# re-edited alongside every new command. Alphabetical order — click's default — presented
# the commands as one flat undifferentiated list, so the help named every piece and
# nothing about how the pieces fit; a reader could learn that `mcp`, `port` and `forget`
# exist without learning that the first two are things you do *to a running profile* and
# the third is not. [FRAMING:representation] a listing is a map of the CLI, and the CLI's
# real structure is these jobs.
_COMMAND_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Run a browser", ("up", "down", "restart", "show", "list")),
    ("Point tools at one", ("mcp", "env", "port")),
    ("Declare what exists", ("init", "add", "rm", "config", "forget")),
    ("Look after crom's own state", ("doctor", "release", "clean")),
)


class CromCommand(click.Command):
    """Gets crom ready to run a command, once click knows which command that is.

    `Session.begin` can refuse — a legacy install whose Chrome is still up, a home crom
    cannot write — and *where* that refusal lands is this class's whole subject; what it
    is refusing over belongs to `session.py` and is not spelled a second time here.
    Readying ran from the group callback, which click calls *before* `make_context`
    parses the invoked subcommand, so `--json` had not been recorded yet and `_answer`
    had nothing to honour: `crom list --json` answered a refusal with prose and an empty
    stdout. Calling it here does not catch that failure earlier, it makes it happen later
    — past the parse, where the flag is already known and the boundary can answer for it
    like any other. [LAW:no-ambient-temporal-coupling] the ordering is stated as a place
    rather than left to which of click's phases happens to run first.

    `CromGroup.command_class` is what makes it a rule instead of a habit: every command
    built by `@main.command` is this class, so a command added tomorrow is ready without
    anyone remembering to ready it. [LAW:single-enforcer]

    What this changes for a reader: an eager option answers while the command line is
    still parsing, so it never arrives here. `crom up --help` used to fail on a machine
    whose home crom could not use — the group callback bootstrapped before click could
    descend into the subcommand — while `crom --help` worked, splitting the two things a
    confused user reaches for on exactly the machine where they are confused.
    """

    def invoke(self, ctx):
        ctx.obj = Session.begin()
        return super().invoke(ctx)


class CromGroup(click.Group):
    """Turns a failed command into the CLI's exit-code contract, in one place."""

    command_class = CromCommand

    def parse_args(self, ctx, args):
        """A bare `crom` is `crom up`, said as an argument rather than as a second way in.

        The group used to carry `invoke_without_command` and call `ctx.invoke(up_cmd,
        ref="default", as_json=False)` — a dispatch path that bypasses
        `Command.invoke`, so whatever readies a command would have had to be spelled a
        second time to cover it, and `ref="default"` was already `up`'s own default
        written out again. [LAW:one-source-of-truth] Supplying the name instead leaves one
        road into a command body, which is the whole of what this ticket is about.

        `resilient_parsing` is click's own discriminator for a parse that is not an
        invocation — shell completion working out what `crom <TAB>` could mean, where
        offering the command list is not a request to run `up`.
        [LAW:dataflow-not-control-flow] the branch is on click's own enum rather than on
        a condition crom invented to tell the two apart.
        """
        default_command = [] if ctx.resilient_parsing else ["up"]
        return super().parse_args(ctx, args or default_command)

    def invoke(self, ctx):
        """Answer for both ways a command fails: crom's own refusals, and the OS's.

        Catching `CromError` alone left the second half escaping as a raw traceback, and
        the codebase had begun closing that one call site at a time —
        `operations.delete_directory` wraps `rmtree`, `configwrite._writing` wraps the config
        save, `seed._copy` wraps the seed stat — while `crom mcp --path <a-directory>`
        still printed a stack trace out of `read_text`. [LAW:single-enforcer] a rule kept
        per command is a rule the next command is written without; kept here, a new
        command cannot reintroduce the hole, because it never had to remember.

        Those wrappers stay. They say more than this floor can — which retry is left,
        which seed could not be read — and enrichment is not enforcement.

        The line stops at `OSError` on purpose: that is the world refusing crom, and it
        names something the user can go fix. Anything else arriving here is crom being
        wrong about its own state, and a traceback is the honest report of that.
        [LAW:no-silent-failure]

        A broken pipe is neither, which is why it is handed back: `crom list | head` is
        a reader leaving on purpose, and the conventional end to that is silence.
        """
        try:
            return super().invoke(ctx)
        except CromError as error:
            raise _answer(ctx, error, str(error)) from error
        except OSError as error:
            # `errno` and not `BrokenPipeError`, because click's own handler keys on
            # `errno.EPIPE` for any `OSError` while `BrokenPipeError` also carries
            # `ESHUTDOWN`: re-raising the wider class hands click an error it declines
            # too, and the traceback comes back. [LAW:one-source-of-truth] the set click
            # owns is spelled the way click spells it.
            if error.errno == errno.EPIPE:
                raise
            # The path and the reason, in the shape crom's own filesystem errors already
            # take (`configwrite._writing`). An `OSError` carries neither reliably —
            # `os.kill` names no file, `shutil.Error` carries no `strerror` — so the
            # missing halves drop out as values rather than as branches.
            # [LAW:dataflow-not-control-flow]
            parts = (error.filename, error.strerror or error)
            raise _answer(ctx, error, ": ".join(str(part) for part in parts if part)) from error

    def format_commands(self, ctx, formatter) -> None:
        """Render the command list in sections, and never omit a command.

        The leftover section is what makes the grouping safe to curate: a command added
        to the group but not to `_COMMAND_SECTIONS` still appears, under a heading whose
        blankness is the bug report. [LAW:no-silent-failure] the alternative — iterating
        the curated names alone — deletes a real command from the only place users look
        for it, and does so silently, on the machine of someone who does not know the
        command exists. `test_help_sections_cover_every_command` keeps that heading
        empty; this keeps it honest when the test has not run.
        """
        listed = {name for _, names in _COMMAND_SECTIONS for name in names}
        leftover = tuple(n for n in self.list_commands(ctx) if n not in listed)

        for title, names in (*_COMMAND_SECTIONS, ("Other", leftover)):
            rows = [
                (name, self.get_command(ctx, name).get_short_help_str(limit=68))
                for name in names
                if self.get_command(ctx, name) is not None
            ]
            if rows:
                with formatter.section(title):
                    formatter.write_dl(rows)


def _emit(as_json: bool, payload, lines: list[str]) -> None:
    """Render one successful result. The last inch of UI, and the only place a *result*
    chooses its format — a failure has two readers at once, so it renders in
    `_answer`."""
    click.echo(_json_text(payload) if as_json else "\n".join(lines))


def _pid_list(pids: tuple[int, ...]) -> str:
    """How a profile's PIDs read in a message — one spelling, for the three that print them."""
    return ", ".join(map(str, pids))


def _layer_notes(profile: ResolvedProfile) -> dict[str, str]:
    """Where each switch this profile resolves to comes from, keyed by the text it emits.

    The sole builder of that mapping, for the two commands that annotate a flag with its
    provenance: `crom config` beside every line of the resolved command, and `crom up`
    beside a switch a relaunch moved. [LAW:single-enforcer] spelled twice, the collision
    rule `_supplied` documents would have to be changed in both places to stay true, with
    nothing making them agree.

    Keyed by the whole flag text rather than the switch name because that is the spelling
    both readers already hold: `crom config` looks up lines of `profile.argv`, and a
    `drift.Change.resolves` is one of those same strings. Keying by switch name would also
    reintroduce the collision `drift` keys its own entries by kind to rule out — a config
    may legally name a flag `env TZ`.
    """
    return {str(item.flag): _note(item) for item in profile.provenance.emitted}


def _supplied(change: drift.Change, notes: dict[str, str]) -> str:
    """Which layers decided the current value of a changed switch, as a clause beside it.

    `drift` carries no provenance and could not: it compares a record written at launch
    against what the config resolves to now, and only the current side still has layers to
    name — the recorded side was decided by a config file this crom no longer has. So the
    clause is attached here, from the same `_note` `crom config` prints, and only to the
    side that resolves. [LAW:one-source-of-truth] one rendering of where a flag came from.

    Joined on the whole flag text and confirmed against the subject, because `notes` holds
    only what a launch *emits*: a change about `chrome binary` or an `env` variable has no
    layer clause to find, and its value could spell a flag text that does. `drift` keys its
    own entries by kind to make that collision unrepresentable and deliberately does not
    publish the kind, so re-deriving it from the switch is what keeps a variable set to
    `--window-size=800,600` from borrowing `--window-size`'s provenance.
    """
    # `or ""` only to keep the parse total. Both mean "no flag text to look up" here; the
    # `None`-is-not-`""` distinction `Change` protects is about what a config said, not
    # about whether a layer clause exists to find.
    resolves = change.resolves or ""
    supplies = notes.get(resolves, "") * (Flag.parse(resolves).switch == change.subject)
    return f" ({supplies})" * bool(supplies)


def _moved(changes: tuple[drift.Change, ...], notes: dict[str, str]) -> list[str]:
    """The switches a drift names, one indented line each, beside the layer supplying the
    new value.

    One spelling for the two answers `crom up` gives a drifted browser — the relaunch that
    applied the drift and the `--no-restart` report that declined to. Spelled twice, a
    change to how a moved switch reads lands in one and not the other, and the two outputs
    a user compares to decide whether to relaunch disagree about what moved.
    [LAW:single-enforcer]

    The changes rather than the verdict holding them, because the caller now selects its
    arm on an `operations.Outcome` and so holds a `drift.Verdict` no pattern has narrowed
    to `Drifted`. `changes` is on all four verdicts and empty on three of them by `drift`'s
    own design — the same reason `crom list` renders a verdict without asking which one it
    holds — so taking the tuple is the honest parameter and needs no narrowing to be total.

    A comprehension over a tuple that is empty exactly when the flags moved only in order —
    a real drifted state `Drifted.finding` already spells out on the line above — so the
    section appears when it has something to say and no branch decides whether it exists.
    [LAW:dataflow-not-control-flow]
    """
    return [f"  {change}{_supplied(change, notes)}" for change in changes]


def _status(profile: ResolvedProfile, live: dict[str, tuple[int, ...]]) -> tuple[bool, tuple[int, ...]]:
    pids = live.get(str(profile.profile_dir), ())
    return bool(pids), pids


@click.group(cls=CromGroup)
# The version is read from the installed distribution's metadata, which the build copies
# out of pyproject.toml, so crom holds no second spelling of it to fall behind a release.
# [LAW:one-source-of-truth]
#
# `package_name` is named rather than left to click, which otherwise infers it by
# inspecting the caller's frame globals — making the distribution crom asks about a fact
# about which module this decorator happens to sit in.
#
# The version alone, as `crom port` prints a port alone: stdout carries the answer, so a
# script reads it without splitting a sentence for it. [CLI binding] click's default,
# `<prog>, version <v>`, would also spell that answer differently from one invocation to
# the next, since `prog` is argv[0].
#
# Eager, as click makes every `--version`: it answers and exits while the command line is
# still being parsed, so it never reaches the readying `CromCommand.invoke` does. That is
# the difference between a version crom can always state and one it can state only on a
# machine whose home crom can already use. [LAW:no-ambient-temporal-coupling]
@click.version_option(package_name="crom", message="%(version)s")
def main():
    """crom — a real Chrome per project, each on a port that never moves.

    \b
    The three words
      profile    one Chrome user-data-dir, plus the CDP port crom assigns it.
      namespace  the profiles belonging to one project, so two projects never
                 collide on a port or a directory.
      ref        how you name a profile. `dev` means dev in the namespace you
                 are standing in; `myapp/dev` names it from anywhere.

    Which namespace you are standing in is decided by the directory you run from:
    it is `user` — your personal profiles — unless a `.crom.toml` sits here or
    above, in which case it is the one that file declares. `crom config` always
    says which, and `crom list` shows both.

    \b
    Start here
      crom up            bring up `default` and print its CDP URL
      crom mcp           point chrome-devtools-mcp at it, here
      crom init          give this project its own namespace and profiles
      crom config        what is in effect here, and what `crom up` will do

    A new profile starts as a copy of your real Chrome profile, so it has your
    logins and extensions. `--seed fresh` on `init` or `add` gets an empty one.

    A `.crom.toml` written by `crom init` sets the namespace, then `[defaults]`
    and a `[profiles.<name>]` for each profile. Where both answer, the profile
    wins, and for flags it wins one Chrome switch at a time, so each switch
    reaches Chrome exactly once. `crom config <profile>` shows the resolved
    command with the layer behind each flag; `crom config --help` is the
    reference for every key a config may set.

    crom does the setup step for you rather than naming it: a profile you refer
    to but never declared is declared, and a config file crom cannot read is
    reset to the default with your original kept beside it as `<name>.broken`.
    Both are reported on stderr as they happen.

    Every command asks for a state, not a change, so asking twice is not an
    error: `crom init` in a project that has a .crom.toml, `crom add` of a
    profile already declared, and `crom up` of a browser already running what
    this config resolves to all report what is there and exit 0. A browser
    running something else is not that state, so `crom up` stops it and
    starts it again on the current config: edit a flag, run `crom up`, and
    the edit is live. Only a request for something *different* from what
    exists is refused — `crom add dev --port 9500` when `dev` is declared on
    another port names the difference and changes nothing.
    """


@main.command("up")
@click.argument("ref", required=False, default="default")
@click.option(
    "--no-restart",
    is_flag=True,
    help="Name a drifted browser's changes instead of replacing it, keeping its session.",
)
@_json_option
@click.pass_obj
def up_cmd(session: Session, ref: str, no_restart: bool, as_json: bool):
    """Bring a profile up on its current config, whatever is running.

    Idempotent: a browser already running what this config resolves to is reported, not
    restarted. One running something else is stopped and started again on the current
    config, with what moved named before anything stops. A browser crom holds no launch
    record for is left running and reported as unmeasured — crom cannot tell what it was
    started with, which is no grounds to kill a browser that may already match.

    --no-restart takes only the stop off the table: a drifted browser is named and left
    running, tabs and logins intact, and a profile with nothing running still launches. It
    is how a script keeps a profile up without ever costing its user a session, and the
    one way `crom up` exits 0 on a browser it did not bring onto the current config.
    """
    profile = session.working(ref)
    where = f"{profile.ref} on {profile.cdp_url}"
    # Built out here because it is a fact about the resolution, and nothing the operation
    # does can change it.
    notes = _layer_notes(profile)
    # A boolean is click's spelling of this flag and not the domain's, so the crossing
    # happens once, here, and nothing past this line holds a `no_restart` whose negation a
    # reader has to run backwards. [LAW:types-are-the-program]
    on_drift = operations.OnDrift.REPORT if no_restart else operations.OnDrift.REPLACE
    ran = operations.up(profile, on_drift)
    # One arm per ending, wording the ending the operation already decided. The pair
    # `(verdict, policy)` is matched once, inside `operations.up`, and deliberately not
    # again here: matched twice this would be a second copy of the arm map, in a second
    # file, and the day they disagree a run that replaced a browser reports that it left
    # one alone. [LAW:one-source-of-truth]
    #
    # `MATCHED` is the one ending whose `finding` goes unsaid — "running with what this
    # configuration resolves to" is the headline above it in other words. The two endings
    # that do print one are saying something the headline does not already claim.
    match ran.outcome:
        case operations.Outcome.STARTED:
            lines = [f"Started {where}"]
        case operations.Outcome.MATCHED:
            lines = [f"Already running {where}"]
        case operations.Outcome.UNMEASURED:
            # Said rather than swallowed: a bare "Already running" here would be crom
            # reporting an agreement it never established, and is how a browser goes on
            # running flags its config stopped asking for with crom appearing to have
            # checked. [LAW:no-silent-failure]
            lines = [f"Already running {where}", f"  {ran.found.finding}"]
        case operations.Outcome.REPORTED:
            # The one ending where `crom up` exits 0 without having converged, so it reports
            # in the shape `UNMEASURED` reports in — "running, and here is what crom will
            # not act on" — and adds the switches, which is the whole of what `--no-restart`
            # has over the `drift` `crom list` already publishes. Claiming "Already running"
            # alone would be the stale-browser silence this epic exists to end, from the
            # command that ended it. [LAW:no-silent-failure]
            lines = [
                f"Already running {where}",
                f"  {ran.found.finding}",
                *_moved(ran.found.changes, notes),
            ]
        case operations.Outcome.RELAUNCHED:
            # No finding line here: `operations.up` said what moved on stderr before it
            # stopped anything, so printing it again on stdout would give a user watching
            # one terminal the reason twice.
            lines = [
                f"Relaunched {where} (was pid {_pid_list(ran.stopped)}, "
                f"now pid {_pid_list(ran.pids)})",
                *_moved(ran.found.changes, notes),
            ]

    _emit(
        as_json,
        {
            **profile.describe(running=True, pids=ran.pids),
            # The verdict crom *found*, under a key that says so. `crom list` and `crom
            # config` publish `drift`, which is how a profile stands right now; this
            # command's answer is how the profile stood when this command reached it, and
            # reusing the name would have one key meaning two things across the JSON
            # surface. Deliberately not "what crom acted on" either — under `--no-restart`
            # crom acts on none of it, and a key that narrowed to the acted-on cases would
            # go silent on the drift in exactly the run that was asked to report it.
            # [FRAMING:representation]
            "found": drift.describe(ran.found),
            # What the convergence replaced, the way `restart` carries it: empty unless this
            # run relaunched, and what separates a browser this command left alone from one
            # it swapped out under the same "running" record. Nearly the whole of it, not
            # quite: a replacement whose browser exited before `chrome.kill` reached it
            # publishes an empty `stopped` too. `operations.Outcome` is where that
            # difference is exact, and this pair of keys is the JSON's long-standing
            # approximation of it — carried across this move unchanged rather than widened
            # under cover of a refactor. [LAW:one-source-of-truth]
            "stopped": list(ran.stopped),
        },
        lines,
    )


@main.command("down")
@click.argument("ref", required=False, default="default")
@_json_option
@click.pass_obj
def down_cmd(session: Session, ref: str, as_json: bool):
    """Stop a running profile."""
    profile = session.profile(ref)
    pids = operations.down(profile)
    message = (
        f"Stopped {profile.ref} (pid {_pid_list(pids)})"
        if pids
        else f"{profile.ref} was not running"
    )
    _emit(as_json, profile.describe(running=False, pids=pids), [message])


@main.command("restart")
@click.argument("ref", required=False, default="default")
@_json_option
@click.pass_obj
def restart_cmd(session: Session, ref: str, as_json: bool):
    """Stop a profile and start it again on its current config."""
    profile = session.working(ref)
    # Both halves under one hold of the lock, which is the whole of what this command adds
    # over typing `crom down && crom up`. Released in between, another crom process is free
    # to land in the gap: a concurrent `up` sees nothing running and starts the browser, so
    # this command's own start then finds a live Chrome and reports a restart it did not
    # perform — on the old configuration, which is the one thing a restart exists to
    # replace. `rm` in the gap is worse, and deletes the directory this is about to launch
    # against. [LAW:no-ambient-temporal-coupling] the indivisible span is stated here, by
    # the only participant that knows how wide it is.
    #
    # `chrome.kill` is what makes the start safe to follow it directly: it returns only
    # once the profile holds neither a process nor its CDP port, so this cannot race its
    # own socket teardown and lose the port to the corpse of the browser it just stopped.
    with seed.profile_lock(profile):
        stopped = chrome.kill(profile)
        if stopped:
            # Said before the start rather than assembled with the result afterwards, so
            # the fact survives a start that fails. A restart whose launch half fails
            # leaves the user with no browser at all, and an error naming only the start
            # would hide that crom stopped the working one they had. It doubles as the
            # progress line for the pause while Chrome comes up. [CLI binding] stderr.
            click.echo(
                f"Stopped {profile.ref} (pid {_pid_list(stopped)}); starting it again …",
                err=True,
            )
        # The verdict is discarded rather than reported: `kill` has just guaranteed nothing
        # is running, so it can only be `Stopped` and a start here is always a start. The
        # interesting fact is what was stopped, and that is what `stopped` carries.
        _, pids = operations.start_under_lock(profile)

    was, now = _pid_list(stopped), _pid_list(pids)
    message = (
        f"Restarted {profile.ref} on {profile.cdp_url} (was pid {was}, now pid {now})"
        if stopped
        else f"{profile.ref} was not running; started it on {profile.cdp_url}"
    )
    # `stopped` rides alongside the record rather than inside it: what a restart replaced is
    # a fact about this command, not about the profile, and `describe()` is the shape every
    # command's JSON shares. Without it a `--json` caller cannot tell a browser that was
    # replaced from one that was merely started, which is the single distinction this
    # command exists to report. [LAW:one-source-of-truth] `describe()` stays canonical.
    _emit(
        as_json,
        {**profile.describe(running=True, pids=pids), "stopped": list(stopped)},
        [message],
    )


@main.command("show")
@click.argument("ref", required=False, default="default")
@_json_option
@click.pass_obj
def show_cmd(session: Session, ref: str, as_json: bool):
    """Bring a profile's window to the front, launching it if it is not running."""
    profile = session.working(ref)
    # Starting and raising under one hold, so the PIDs raised are the PIDs observed. A
    # `down` landing between the two would leave `window.raise_profile` asking macOS for a
    # process that no longer exists, and the -1719 it answers with reads as "the browser
    # exited" — true, but it would be describing a race this command could have prevented.
    #
    # The raise is inside the lock rather than after it for that reason alone; it costs one
    # osascript round trip, which is the same order as the `ps` call `operations.start_under_lock`
    # already makes while holding it.
    with seed.profile_lock(profile):
        verdict, pids = operations.start_under_lock(profile)
        # `Stopped` is the verdict for a profile with nothing running, which is exactly the
        # case `operations.start_under_lock` launches in — so this reads the launch off the same
        # observation that caused it rather than asking after the fact. `show` raises a
        # window and does not converge: a browser whose config has moved on is the user's
        # to replace with `crom up`, and killing it here would cost them the very window
        # they asked to be shown. [LAW:decomposition] one purpose, said in one sentence.
        started = isinstance(verdict, drift.Stopped)
        if started:
            # Said before the raise rather than assembled with the result afterwards, so
            # the fact survives a raise that fails. Withheld Automation access is likeliest
            # on a first run — the same run likeliest to have started the browser — and a
            # user shown only the raise error would go hunting for a launch failure that
            # never happened. On stderr because for `show` the answer is the raise itself;
            # a launch on the way to it is progress, like `operations.start_under_lock`'s own
            # "Creating … from seed" line. [CLI binding]
            click.echo(f"Started {profile.ref} on {profile.cdp_url}", err=True)
        windows = window.raise_profile(profile, pids)

    raised = (
        f"Raised {profile.ref}"
        if windows
        else f"Raised {profile.ref}, but it has no open windows to show — it is running "
        f"headless, or its last window was closed."
    )
    # The window count rides alongside the record for the same reason `restart` carries
    # what it stopped: the human line already distinguishes a raise that found a window
    # from one that did not, and a `--json` caller confirming the window actually came
    # forward — the whole point of the command — could otherwise only get there by parsing
    # the prose. [FRAMING:representation] one result, and both maps of it say the same.
    _emit(
        as_json,
        {**profile.describe(running=True, pids=pids), "started": started, "windows": windows},
        [raised],
    )


@main.command("list")
@click.option("--all", "everything", is_flag=True, help="Include every namespace crom knows.")
@_json_option
@click.pass_obj
def list_cmd(session: Session, everything: bool, as_json: bool):
    """List the profiles addressable from here."""
    scopes, unavailable = _scopes_to_list(session, everything)

    live = chrome.scan()
    records, lines = [], []
    for scope in scopes:
        for entry in resolver.resolve_all(scope):
            match entry:
                case ResolvedProfile():
                    running, pids = _status(entry, live)
                    # Every row carries a verdict, including the rows whose verdict is
                    # that there is nothing to compare — so the listing has no per-row
                    # branch asking whether this line has one. [LAW:dataflow-not-control-flow]
                    verdict = drift.of(entry, pids)
                    record = entry.describe(running=running, pids=pids)
                    records.append({**record, "drift": drift.describe(verdict)})
                    state = f"running :{entry.port}" if running else f"stopped :{entry.port}"
                    lines.append(f"  {str(entry.ref):28s}  {state:15s}  {verdict.finding}")
                case FailedProfile():
                    records.append(entry.describe())
                    lines.append(f"  {str(entry.ref):28s}  unresolved — {entry.error}")
        if not scope.profiles:
            lines.append(f"  {scope.namespace}/ — no profiles declared in {scope.source or 'user config'}")

    for namespace, error in unavailable:
        records.append({"namespace": namespace, "error": error})
        lines.append(f"  {namespace + '/':28s}  unavailable — {error}")

    _emit(as_json, records, lines)


def _scopes_to_list(session: Session, everything: bool) -> tuple[list[Scope], list[tuple[str, str]]]:
    """The scopes `crom list` should report, plus the namespaces it could not load.

    A remembered namespace whose config file has been deleted or moved raises `NotFound`
    from `scope_for`, which drops crom's record of where it lives on the way past. One
    stale entry used to abort the entire listing, so the command that would have shown
    the user which namespace was broken was the one command that could not run. Each
    namespace is isolated and reported by name instead. [LAW:no-silent-failure] nothing
    is skipped quietly: the failure is a row in the output, human and JSON alike, and
    `scope_for` narrates the drop on stderr with the file it could no longer find.
    """
    scopes = [session.scope]
    if not session.scope.is_user:
        scopes.append(load_user_scope())

    unavailable: list[tuple[str, str]] = []
    if everything:
        for namespace in sorted(registry.namespaces()):
            if namespace == session.scope.namespace:
                continue
            try:
                scopes.append(resolver.scope_for(namespace, session.scope))
            except CromError as error:
                unavailable.append((namespace, str(error)))
    return scopes, unavailable


# How wide the flag column grows before the notes beside it stop lining up. A cap rather
# than the true widest flag: one long `--host-resolver-rules=...` would otherwise push
# every note on the listing off the right of an ordinary terminal, to align with a line
# nobody was reading the note for.
_NOTE_COLUMN = 46


def _resolution(answered: Resolution, *, named: bool) -> str:
    """One question's history, in a clause that can sit beside the flag it decided.

    The one place a resolution becomes prose, so the three shapes the report has — an
    ordinary switch, a feature name inside a switch that carries several, and a switch a
    drop removed — read the same way rather than each inventing a phrasing.
    [LAW:single-enforcer]

    `named` because the question is worth printing only where the line does not already
    carry it: an ordinary flag's question *is* the switch printed to its left.

    That comparison spans an expansion — the question is the switch as the file spells it,
    the flag has been through `resolve._expand` — and it is sound because `flags.layer`
    refuses a `${` in a switch name, so the two spellings cannot differ. If that border
    rule is ever relaxed, this reduces an ordinary flag to the feature shape and prints the
    pre-expansion switch beside its expanded self. [LAW:parse-dont-validate] the border is
    what makes this safe to read, not care taken here.
    """
    # The value first and the layer last, so one phrasing carries both vocabularies: a
    # replaced flag reads "over --window-size=800,600 from [defaults]" and a replaced
    # feature reads "over false from [defaults]". Layer-first put a bare `false` against a
    # layer name and left the reader to guess which word was the value.
    over = "".join(f", over {answer.said} from {answer.layer}" for answer in answered.replaced)
    return f"{answered.question + ' ' if named else ''}from {answered.stands.layer}{over}"


def _note(item: Emitted) -> str:
    """Where one emitted switch came from — one clause, or one per feature it carries."""
    return " · ".join(
        _resolution(answered, named=answered.question != item.flag.switch)
        for answered in item.why
    )


@main.command("add")
@click.argument("name")
@click.option(
    "--seed",
    "seed_text",
    default=None,
    help=(
        "default | chrome:<Profile> | fresh | ./path — where this profile's data comes "
        "from. Omit to inherit [defaults].seed from the config."
    ),
)
@click.option("--flag", "flag_texts", multiple=True, help="Chrome flag; repeatable.")
@click.option("--port", type=int, default=None, help="Pin the CDP port instead of letting crom assign one.")
@click.pass_obj
def add_cmd(session: Session, name: str, seed_text: str | None, flag_texts: tuple[str, ...], port: int | None):
    """Declare a profile in the config governing this directory. Idempotent."""
    validate_name("profile name", name)
    scope = session.scope
    target = config.write_target(scope)
    where = profile_stanza(name)
    spec = ProfileSpec(
        name=name,
        # No drops: `crom add` has no `--drop-flag`, so the request it builds cannot state
        # one. The empty list is the request, not a placeholder — a stanza that drops
        # nothing is what `--flag` alone asks for.
        flags=parse_layer(list(flag_texts), [], where, target),
        # None when `--seed` was not given, which `configwrite` writes as no `seed` key
        # and `resolve_spec` reads as `scope.default_seed`. The old `default="fresh"`
        # meant every added profile carried an explicit `seed = "fresh"` nobody had asked
        # for, so a project that set `[defaults].seed` found it applied to the profile
        # `crom init` wrote and to no profile added afterwards.
        seed=None if seed_text is None else parse_seed(seed_text, where, target, scope.config_dir),
        # Through `parse_port`, the same validator a port from the file goes through.
        # click only proves this is an int, so `--port 0` or `--port 99999` used to be
        # written to disk and then rejected by the parser on the next load — bricking
        # every command in the project, which is the failure `operations.add` goes to
        # lengths to avoid where it refuses a duplicate pin before the write rather than
        # after. [LAW:single-enforcer] the range rule has one home; this path was
        # bypassing it rather than needing a copy.
        port=parse_port(port, where, target),
    )
    declaration = operations.add(scope, spec)
    profile = declaration.profile
    # The arm `operations.add` reached, rendered rather than re-derived. "Declared" over a
    # call that wrote nothing is the one sentence this seam exists to keep unsayable, and
    # the only other way to reach it from here is to ask `scope.profiles` whether the name
    # was already there — a picture taken before the write, and wrong for exactly the
    # caller that lost the race for the name. [LAW:one-source-of-truth]
    match declaration.outcome:
        case operations.Declaration.CREATED:
            verb = "Declared"
        case operations.Declaration.ALREADY_PRESENT:
            verb = "Already declared"
    click.echo(f"{verb} {profile.ref} in {declaration.target}")
    # The seed is reported even when it came from `[defaults]` rather than from `--seed`:
    # it decides whether the browser opens with the user's logins or empty, which is the
    # one thing about a new profile that surprises people, and inheriting it silently is
    # how it stays a surprise until launch.
    click.echo(
        f"  seed {configwrite.render_seed(profile.seed, profile.config_dir)}"
        f" · port {profile.port} · {profile.profile_dir}"
    )
    click.echo(f"Run: crom up {profile.ref}")


@main.command("rm")
@click.argument("ref")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--keep-data", is_flag=True, help="Undeclare the profile but leave its directory.")
@click.pass_obj
def rm_cmd(session: Session, ref: str, yes: bool, keep_data: bool):
    """Stop a profile if it is running, undeclare it, release its port, delete its data."""
    profile = session.profile(ref)

    # Read only to compose the prompt, which is why these two reads stay in the command
    # rather than moving with the removal: they are the wording of a question crom asks a
    # human, and a caller that is not a terminal has nobody to ask. Exported as a record
    # for `operations.rm` to hand back, they would be a shape whose only consumer is
    # `click.confirm`. [LAW:composability] The authoritative act is `chrome.kill` under
    # `operations.rm`'s lock, which converges a profile to stopped whether or not this saw
    # it run — so this read is stale by construction and says so.
    running = chrome.is_running(profile)
    deletes_data = not keep_data and profile.profile_dir.exists()

    # Assembled rather than templated because `doctor.measure` walks the whole profile
    # directory: folding it into a comprehension over both consequences would measure a
    # gigabyte of Chrome data on the `--keep-data` path that is not going to delete it.
    consequences = []
    if running:
        consequences.append(f"stop the browser running on port {profile.port}")
    if deletes_data:
        size = _human_size(doctor.measure(profile.profile_dir))
        consequences.append(
            f"delete {profile.profile_dir} ({size}) — its logins, cookies, and history"
        )

    if consequences and not yes:
        click.confirm(
            f"Removing {profile.ref} will:\n"
            + "\n".join(f"  · {line}" for line in consequences)
            + "\nContinue?",
            abort=True,
        )

    # The prompt above is deliberately the last thing before the call: `operations.rm`
    # takes the profile lock for the whole removal, and holding it across an interactive
    # question would block every other crom process for as long as the human takes to
    # answer.
    stopped = operations.rm(profile, session.scope, keep_data=keep_data)
    # The stop is reported rather than performed quietly: killing someone's browser is
    # the most surprising thing this command does, and `--yes` skips the prompt that
    # would otherwise have been its only mention. [LAW:no-silent-failure]
    stopped_note = f" (stopped pid {', '.join(map(str, stopped))})" if stopped else ""
    click.echo(f"Removed {profile.ref}{stopped_note}")


@main.command("init")
@click.argument("namespace", required=False)
@click.option(
    "--seed",
    "seed_text",
    default=None,
    help=(
        "default | chrome:<Profile> | fresh | ./path — what this project's profiles start "
        "from. Written into [defaults].seed. Default: default, a copy of your default profile."
    ),
)
def init_cmd(namespace: str | None, seed_text: str | None):
    """Give this project its own namespace by writing a .crom.toml here. Idempotent."""
    here = Path.cwd()
    # Read for the parse below, and read again inside `operations.init` for the write —
    # two call sites of one rule rather than two spellings of it, exactly as `crom add`
    # and `operations.add` both read `config.write_target`. The seed cannot be parsed
    # without it: a relative path is anchored on the config's own directory everywhere
    # else, and `write_default` renders it back with `render_seed(seed, path.parent)`.
    # Those agree today only because `PROJECT_CONFIG_CANDIDATES[0]` is the bare
    # `.crom.toml`, so its parent *is* `here`. Under the `.crom/config.toml` candidate
    # they diverge, and `--seed ./fixtures` would parse to `here/fixtures`, fail
    # `relative_to(here/'.crom')`, and be written as this machine's absolute path into a
    # file meant to be committed — the exact outcome `render_seed`'s docstring exists to
    # prevent. [LAW:one-source-of-truth]
    target = config.init_target(here)
    # Parsed here, before the file exists, through the same checkpoint that reads the
    # value back on the next command — so `crom init --seed chorme` fails naming the
    # vocabulary rather than writing a config that every later command rejects.
    # [LAW:parse-dont-validate]
    #
    # Named `stated_seed` rather than `seed` because this module imports the `seed`
    # module, and a local of that name shadows it for the rest of the function — working
    # today only because `init_cmd` happens not to need it, and failing with an
    # AttributeError the first time someone adds a line that does.
    stated_seed = (
        None if seed_text is None else parse_seed(seed_text, DEFAULTS_STANZA, target, target.parent)
    )

    project = operations.init(here, namespace, stated_seed)

    # The arm `operations.init` reached, worded rather than re-derived. The other way to
    # reach it from here is to ask whether the file existed before the call — a question
    # only answerable by a second stat, taken after the write, which cannot tell this
    # process's creation from a concurrent one's. [LAW:one-source-of-truth]
    match project.outcome:
        case operations.Declaration.CREATED:
            headline = f"Wrote {project.target} (namespace '{project.namespace}')"
        case operations.Declaration.ALREADY_PRESENT:
            headline = (
                f"{project.target} already configures this project "
                f"(namespace '{project.namespace}')"
            )
    click.echo(headline)
    click.echo(f"  profiles here start from seed '{project.seed}' — change it in [defaults]")
    click.echo(f"Run: crom up  # brings up {project.namespace}/default")


@main.command("config")
@click.argument("ref", required=False)
@_json_option
@click.pass_obj
def config_cmd(session: Session, ref: str | None, as_json: bool):
    """Show the config in effect, and how a profile resolves flag by flag.

    With a REF, every flag of the launch command is printed with the layer that
    supplied it and whatever it outranked — the layering rule below, on your own
    config. This help is the reference for writing that config.

    \b
    Where a key may appear
      top level          namespace (required), chrome_binary, state_dir
      [defaults]         flags, drop_flags, features, env, seed
      [profiles.<name>]  flags, drop_flags, features, env, seed, port

    Where two layers answer the same question, the profile's answer wins — per
    switch for `flags`, per feature name for `features`, per variable for `env`,
    and outright for `seed`. `flags` and `features` have a third layer beneath
    both, crom's own launch policy, which they beat in turn. `drop_flags` is the
    one key that never conflicts: every layer's drops apply.

    Flags resolve by switch name rather than by concatenation, so each Chrome
    switch is emitted exactly once — crom composes the command instead of
    handing Chrome two answers to the same question.

    The top-level keys do not layer. They are set once for the whole file, and
    `[defaults]` has no counterpart for them — a `port` under `[defaults]` is an
    unknown key, not an inherited default.

    \b
    What each key accepts
      namespace      this project's name: lowercase letters, digits, and . _ -
                     starting with a letter or digit, at most 64 characters.
                     Required in a project config, and never `user`. Your own
                     config in ~/.config/crom is the `user` namespace and must
                     not set the key at all.
      chrome_binary  path to the Chrome to launch. Default: the one crom finds.
      state_dir      where this namespace's profile directories live.
                     Default: crom's own state directory.
      flags          Chrome switches as you would type them on a command line:
                     ["--window-size=1280,800", "--no-pings"]. A later layer's
                     entry replaces an earlier layer's for the same switch.
      drop_flags     switch names alone, never their values: to drop an
                     inherited --window-size=1280,800, write ["--window-size"].
                     Removes a switch a layer below supplied, crom's launch
                     policy included — ["--disable-sync"] launches Chrome with
                     sync left on. This is the only way to say *less* than a
                     lower layer did; a `flags` entry can only replace it.
      features       Chrome feature name -> true/false. The layers union rather
                     than replace, later layers winning per name, and the whole
                     table is emitted as one --enable-features and one
                     --disable-features. There is deliberately no
                     `drop_features`: a table is already per name, so a layer
                     can say the opposite without erasing anything.
      env            string values put into Chrome's environment. Merged one
                     variable at a time, so a profile adding a variable keeps
                     the rest of `[defaults]` rather than replacing the table.
      seed           where a profile's data comes from the first time crom
                     creates it:
    \b
                       default                your default Chrome profile
                       chrome:<Profile Name>  another profile inside your Chrome
                       fresh                  an empty profile
                       ./dir  /dir  ~/dir     a directory you keep yourself
    \b
      port           pin this profile's CDP port, 1..65535. Left out, crom
                     assigns one and remembers it.

    Paths in `chrome_binary`, `state_dir` and a `seed` resolve against the
    directory the config file is in, so a committed config means the same thing
    on every machine.

    \b
    Switches crom owns, and what to write instead
      --user-data-dir, --remote-debugging-port, --remote-debugging-pipe
        The profile's identity and its CDP contract, which crom sets. Naming one
        in `flags` or `drop_flags` is refused.
      --enable-features, --disable-features
        Write `features` entries instead. crom folds every layer's table into
        these two switches, so naming either in `flags` or `drop_flags` is
        refused.

    Inside `flags` values and `env` values, ${CROM_NAMESPACE}, ${CROM_PROFILE},
    ${CROM_PORT}, ${CROM_PROFILE_DIR} and ${CROM_CONFIG_DIR} expand. A switch
    *name* may not interpolate — crom resolves switches by the spelling your
    file uses, and expands afterwards — and feature names are literal.

    \b
    A config using all of it
      namespace = "myapp"
    \b
      [defaults]
      seed = "default"
      flags = ["--window-size=1280,800", "--disable-blink-features=PIP"]
      features = { SharedStorageAPI = false }
      env = { TZ = "UTC" }
    \b
      [profiles.dev]                            # inherits every default above
    \b
      [profiles.ci]
      seed = "fresh"
      port = 9401
      flags = ["--window-size=800,600"]         # replaces the [defaults] size
      drop_flags = ["--disable-blink-features"] # launches without it at all
      features = { SharedStorageAPI = true }    # flips the default back on
      env = { TZ = "America/Denver" }
    """
    scope = session.scope
    default_seed = configwrite.render_seed(scope.default_seed, scope.config_dir)
    payload = {
        "namespace": scope.namespace,
        "source": str(scope.source) if scope.source else None,
        "discovered_from": str(discover() or ""),
        "profiles_root": str(scope.profiles_root),
        "chrome_binary": str(scope.chrome_binary),
        "profiles": sorted(scope.profiles),
        "default_seed": default_seed,
        "bare_up_ref": f"{scope.namespace}/default",
    }
    # This command is what someone runs when they cannot tell what crom is doing, so it
    # leads with the two facts that decide that — which namespace this directory puts
    # them in, and what a bare `crom up` therefore means — before the paths. The previous
    # ordering opened with `profiles_root` and `chrome_binary`, which are the two facts a
    # confused reader needs last.
    lines = [
        f"Here, crom is in the '{scope.namespace}' namespace.",
        f"  declared by   {scope.source or '(no config file — your implicit user scope)'}",
        f"  profiles      {', '.join(sorted(scope.profiles)) or '(none declared)'}",
        f"  new ones use  seed '{default_seed}'",
        f"  data in       {scope.profiles_root / scope.namespace}",
        f"  chrome        {scope.chrome_binary}",
        "",
        f"`crom up` with no argument here means `crom up {scope.namespace}/default`.",
        (
            "Profiles in other namespaces stay reachable as `<namespace>/<name>`; "
            "`crom list --all` shows them."
        ),
    ]

    if ref:
        profile = session.working(ref)
        running, pids = _status(profile, chrome.scan())
        verdict = drift.of(profile, pids)
        notes = _layer_notes(profile)
        # Measured over the annotated lines alone: the bare ones are the binary path and
        # the profile directory, which are the longest things here and have nothing to line
        # anything up with.
        width = min(max((len(arg) for arg in notes), default=0), _NOTE_COLUMN)
        payload["resolved"] = {
            **profile.describe(running=running, pids=pids),
            "argv": list(profile.argv),
            # Beside `argv` rather than inside `describe()`, for the reason the seed is:
            # this is how the profile came to be what it is, which is `crom config`'s
            # subject, while `up` and `list` report what it is now.
            #
            # Rendered by the report's own types, so the JSON a consumer parses and the
            # lines a human reads are two views of one value rather than two hand-kept
            # descriptions of it. [LAW:one-source-of-truth]
            "flags": [item.describe() for item in profile.provenance.emitted],
            "dropped": [removal.describe() for removal in profile.provenance.dropped],
            # The seed lives here rather than in `describe()` because it is a create-time
            # input, not a property of the profile: once the directory exists it records
            # where the data came from, and every other `describe()` consumer — `up`,
            # `list` — is reporting what the profile *is* right now.
            "seed": configwrite.render_seed(profile.seed, profile.config_dir),
            # Last, because it is the only fact here about the *browser* rather than
            # about the resolution: everything above says what `crom up` would launch,
            # and this says how what is already running stands against it.
            "drift": drift.describe(verdict),
        }
        lines += [
            "",
            f"{profile.ref} resolves to:",
            (
                f"  seed {configwrite.render_seed(profile.seed, profile.config_dir)}"
                f" · port {profile.port} · {profile.profile_dir}"
            ),
            # Every line of the command, each annotated with where it came from. A flag a
            # user wrote can legitimately not be here — a later layer replaced it, or a
            # layer dropped it — and this listing is the only place that difference is
            # visible, so it says which layer supplied each switch and which layers it
            # outranked to get there. [LAW:no-silent-failure]
            #
            # `notes` is keyed by the flag text because the report holds the same expanded
            # strings `argv` was built from; a line crom frames rather than composes — the
            # binary, `--user-data-dir`, `--remote-debugging-port` — is simply not in it and
            # prints bare, with no branch asking which kind of line this is.
            # [LAW:dataflow-not-control-flow]
            *(f"  {arg.ljust(width)}  {notes.get(arg, '')}".rstrip() for arg in profile.argv),
            # A dropped switch is absent from argv and indistinguishable there from one
            # nobody ever set, so the only reader who could tell them apart is the one who
            # wrote `drop_flags` — and they are the reader least in need of being told.
            # Named here with the layer it would have come from, the removal is something
            # the listing shows rather than something the reader has to already know.
            #
            # A generator over a tuple that is usually empty, so the line appears when
            # there is one to print without a branch deciding whether this section exists.
            *(
                # The whole flag as its subject, not the bare switch — the same rule the
                # emitted lines above follow, so the two shapes are parallel rather than
                # this one being a reduced version of them. A switch set once and then
                # dropped has no other channel carrying the value that was lost: the flag
                # is absent from argv, which is the whole reason this line exists.
                f"  (dropped {removal.what.stands.said}, "
                f"{_resolution(removal.what, named=False)} — removed by {removal.by})"
                for removal in profile.provenance.dropped
            ),
            # The verdict, then a line per entry that moved. Every line above is the
            # *current* resolution, so a flag the user has since edited appears there as
            # its new value with nothing saying the running browser never got it — and
            # the old value has no other channel. [LAW:no-silent-failure]
            "",
            f"  {verdict.finding}",
            *(f"    {change}" for change in verdict.changes),
        ]

    _emit(as_json, payload, lines)


@main.command("port")
@click.argument("ref", required=False, default="default")
@click.pass_obj
def port_cmd(session: Session, ref: str):
    """Print a profile's CDP port and nothing else."""
    click.echo(session.working(ref).port)


@main.command("env")
@click.argument("ref", required=False, default="default")
@click.pass_obj
def env_cmd(session: Session, ref: str):
    """Print shell exports for a profile: eval "$(crom env dev)"."""
    profile = session.working(ref)
    # `CROM_PROFILE` is the profile *name*, matching what the same spelling means inside
    # a config's `${CROM_PROFILE}` interpolation. It used to be the full "namespace/name"
    # here and the bare name there, so one identifier named two different things
    # depending on where it was read — and the README presents both as one vocabulary,
    # which is what made the collision misleading rather than merely inconsistent.
    # [LAW:one-source-of-truth] The interpolation vocabulary already decomposes a ref
    # into namespace and name, so that is the meaning that composes; `CROM_REF` carries
    # the joined form under a name that means only that.
    for key, value in {
        "CROM_NAMESPACE": profile.ref.namespace,
        "CROM_PROFILE": profile.ref.name,
        "CROM_REF": str(profile.ref),
        "CROM_PORT": str(profile.port),
        "CROM_CDP_URL": profile.cdp_url,
        "CROM_PROFILE_DIR": str(profile.profile_dir),
    }.items():
        # This output is meant to be `eval`ed, so it is shell source, not text: a profile
        # directory under a path like `~/My Projects` would otherwise end the assignment
        # at the space and the rest of the path would be read as a command. `shlex.quote`
        # leaves ordinary values exactly as they were.
        click.echo(f"export {key}={shlex.quote(value)}")


def _legacy_notes(legacy: mcp.Legacy, ref: ProfileRef, key: str, path: str) -> tuple[str, ...]:
    """What to say about an entry the file already held under crom's old constant key.

    A table over the three outcomes rather than a chain of `if`s, and total rather than
    a `.get(..., ())`, so an outcome added to `mcp.Legacy` later fails here loudly
    instead of quietly printing nothing about itself. [LAW:dataflow-not-control-flow]
    [LAW:no-silent-failure]

    The kept clause claims only that the entry is not what crom writes for this profile,
    which is the whole of what crom knows about it. Both shapes that reach KEPT are in
    it — an entry naming another browser and one whose body a human edited — and telling
    those apart, or naming the port in either, would need a parser for an entry crom may
    not have written, which is exactly the guess `mcp.write` refuses to make. It says
    nothing about which, because what the user can act on is the same either way: this
    file now declares two chrome-devtools servers, and crom will not merge them.
    """
    return {
        mcp.Legacy.ABSENT: (),
        mcp.Legacy.REPLACED: (
            f"Renamed {path}'s '{mcp.LEGACY_KEY}' entry to '{key}' — "
            f"same wiring, now named for {ref}.",
        ),
        mcp.Legacy.KEPT: (
            f"Left {path}'s existing '{mcp.LEGACY_KEY}' entry alone — it is not the entry "
            f"crom writes for {ref}, so the file now declares two chrome-devtools servers.",
        ),
    }[legacy]


@main.command("mcp")
@click.argument("ref", required=False, default="default")
@click.option("--path", "path", default=".mcp.json", help="File to write.")
@click.pass_obj
def mcp_cmd(session: Session, ref: str, path: str):
    """Wire chrome-devtools-mcp at a profile by writing .mcp.json here."""
    profile = session.working(ref)
    legacy = mcp.write(profile, Path(path))
    # Recomputed from the ref rather than carried back from `write`, which is a
    # derivation and not a copy: `entry_key` is pure, so the two callers cannot disagree
    # about the key for one ref the way two stored spellings of it could.
    # [LAW:one-source-of-truth]
    key = mcp.entry_key(profile.ref)
    # Before the answer, and on the other stream: renaming an entry is convergence — work
    # crom did on the user's behalf that they did not ask for — and `report` is where that
    # goes, while the line below is the answer a script parses. [CLI binding]
    for note in _legacy_notes(legacy, profile.ref, key, path):
        report.to_stderr(note)
    click.echo(f"Wrote {path}: '{key}' wired to {profile.ref} ({profile.cdp_url})")


@main.command("forget")
@click.argument("namespace")
def forget_cmd(namespace: str):
    """Drop a namespace from the registry, releasing its reserved ports."""
    released = registry.forget_namespace(validate_name("namespace", namespace))
    click.echo(f"Forgot namespace '{namespace}' ({released} port reservation(s) released)")


@main.command("doctor")
@_json_option
def doctor_cmd(as_json: bool):
    """Show the state crom owns on this machine, and where it has leaked.

    `crom list` reads the config files and reports what they declare; this reports what
    crom is actually holding. The two answers differ exactly where something has leaked,
    which is the only reason to run this.

    Every reservation in the port ledger comes first, each with where it stands against
    the config the ledger names as its source: `declared`, `orphaned` — nothing declares
    it any more — or `unchecked`, which is crom refusing to guess about a config it could
    not read.

    Each also carries who holds its port right now, which is a separate question with a
    separate answer: `idle` if nothing does, `own` if this profile's own browser does,
    `foreign` if something else does, or `unprobed` if crom could not tell. `foreign` is
    the one to act on — only `crom up` checks that a port is still crom's, so `crom port`,
    `crom env` and `crom mcp` will hand out a number a stranger is already answering on.

    Then every staging directory under a profile root. Seeding builds a profile beside its
    final path and moves it in only once it is whole, so a `crom up` killed mid-copy
    leaves the half-built copy behind — dot-prefixed, so `ls` hides it, and the retry
    succeeds, so nothing looks wrong. A seed running right now leaves the same evidence
    and is listed the same way, so a directory here may still be filling rather than
    abandoned; crom does not tell the two apart. Each is reported with its size, and so
    is every namespace crom could not look under.

    Nothing here writes. `crom release <key>` hands back the port under one reservation
    this reports, and `crom clean <path>` deletes one staging directory it found — both
    act only on what this command already named, and both refuse a verdict crom could not
    establish. Releasing a port and deleting a profile copy cannot be undone, which is why
    they are things you ask for rather than things a doctor does on its way past.
    """
    found = doctor.survey()
    _emit(
        as_json,
        found.describe(),
        [
            # All three counts are unconditional, so a clean machine reads as an answer
            # rather than as output that got cut off — and the last of them is what keeps
            # "nothing is leaking" apart from "I could not check".
            # [LAW:dataflow-not-control-flow] `reservation(s)` is how `crom forget`
            # already counts the same noun.
            f"{len(found.rows)} reservation(s) in {found.registry}",
            *(
                f"  {row.held.port:<6}{row.ref:30s}"
                f"{'pinned' if row.held.pinned else '':8s}{row.standing.slug:11s}"
                f"{row.liveness.slug:10s}"
                # Both findings, joined, on every row. The standing's names the config
                # crom consulted or says the ledger records none; the liveness's is the
                # only place a reader learns *why* crom could not tell who holds a port,
                # which no slug can carry. Joining them keeps the line the same shape for
                # every row rather than growing a second line for the rows that have
                # something to add. [LAW:dataflow-not-control-flow]
                f"{row.standing.finding} — {row.liveness.finding}"
                for row in found.rows
            ),
            f"{len(found.staged)} staging directory(s) from a seed interrupted or still running",
            # The size leads: it is what decides whether this is worth acting on, and the
            # paths are long enough to push it off the end of a line if it followed.
            *(f"  {_human_size(item.bytes):>8}  {item.path}" for item in found.staged),
            f"{len({item.namespace for item in found.unscanned})} namespace(s) crom "
            f"could not check for them",
            *(f"  {item.namespace + '/':30s}{item.error}" for item in found.unscanned),
        ],
    )


@main.command("release")
@click.argument("key")
def release_cmd(key: str):
    """Hand one reservation's port back, leaving the rest of its namespace alone.

    The other way to release a port is `crom forget <namespace>`, which releases every
    port under that namespace — the live profiles' included — so it cannot reach one
    orphan without taking its neighbours with it. This reaches exactly one.

    Give it the key `crom doctor` prints, spelled exactly as it prints it. The key is
    taken as written and never taken apart: a hand repair can strand a reservation under
    something that is not a legal `namespace/name` at all, and those are the very ones
    nothing else can reach.

    Crom releases a reservation `crom doctor` calls `orphaned`. It refuses one a config
    still declares — `crom rm <ref>` undeclares and releases together — and one whose
    own browser is still running, which you close first. It also refuses a reservation
    it could not check or could not probe: a released port goes to the next profile that
    asks for one and never comes back, so crom will not release on evidence it never
    got. `crom doctor` says which of those any reservation is.
    """
    match reclaim.releasable(key, doctor.survey()):
        case reclaim.Refused(reason=reason, why=why):
            raise reason.error(why)
        case reclaim.Releasable(row=row):
            # The survey read the ledger and `forget` writes it under its own lock, so a
            # concurrent release can empty the key in between. Which of the two happened
            # is what `forget` reports, and the line says it rather than crediting crom
            # with freeing a number it found already free. The state the caller asked for
            # holds either way, which is why this is an answer and not a failure.
            # [LAW:no-silent-failure]
            act = "Released" if registry.forget(row.ref) else "Already released"
            click.echo(
                f"{act} {row.ref} — port {row.held.port} is free for the next profile "
                f"that asks for one"
            )


@main.command("clean")
@click.argument("path")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def clean_cmd(path: str, yes: bool):
    """Delete one staging directory a seed abandoned, by the path `crom doctor` prints.

    Seeding builds a profile beside its final path and moves it in only once it is whole,
    so a `crom up` killed mid-copy leaves a whole seed's worth of bytes behind under a
    dot-prefixed name `ls` hides. The retry succeeds, so nothing ever looks wrong.

    Only a directory `crom doctor` reported can be deleted here, and that is the whole of
    the safety: a migration stages a legacy profile under the same dot-prefixed shape,
    and for part of its run that copy is the only one there is. The path may be spelled
    however your shell produced it — pasted, relative, through a symlink — because what
    gets deleted is the directory `crom doctor` walked to, not the name you typed.

    A seed running right now leaves identical evidence, and crom cannot tell the two
    apart. The prompt names the size so you can; `--yes` skips it.
    """
    match reclaim.deletable(path, doctor.survey()):
        case reclaim.Refused(reason=reason, why=why):
            raise reason.error(why)
        case reclaim.Deletable(staged=staged):
            size = _human_size(staged.bytes)
            if not yes:
                click.confirm(
                    f"Deleting {staged.path} ({size}) cannot be undone.\nContinue?", abort=True
                )
            operations.delete_directory(
                staged.path, f"Run `crom clean {staged.path}` again once that is fixed."
            )
            click.echo(f"Deleted {staged.path} ({size} reclaimed)")


def _human_size(total: int) -> str:
    """A byte count as a person reads it.

    A number in, a string out: the walk that produces the number belongs to
    `doctor.measure`, which every command that quotes a size now shares, and what is left
    here is the rendering — which is the presenter's whole job. [LAW:effects-at-boundaries]
    """
    size = float(total)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}"
        size /= 1024
