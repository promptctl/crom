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
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import click

from . import (
    chrome,
    config,
    configwrite,
    doctor,
    flags,
    mcp,
    migrate,
    reclaim,
    registry,
    report,
    resolve as resolver,
    seed,
    window,
)
from .config import discover, load_ambient, load_user_scope, parse_layer, parse_port, parse_seed
from .model import (
    DEFAULT_SEED,
    DEFAULTS_STANZA,
    USER_NAMESPACE,
    Conflict,
    CromError,
    Emitted,
    FailedProfile,
    Fields,
    Layer,
    NotFound,
    ProfileRef,
    ProfileSpec,
    Reason,
    ResolvedProfile,
    Resolution,
    Scope,
    parse_ref,
    profile_stanza,
    slug_for,
    validate_name,
)
from .paths import PROJECT_CONFIG_CANDIDATES, user_config_file

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
# reads. `Context.meta` and not the `_Session` on `ctx.obj`: meta is one dict shared by
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

    Migration and the user-config bootstrap can both refuse — `_require_all_stopped`
    raises on a legacy install whose Chrome is still up, and bootstrapping fails on a
    home crom cannot write. They ran from the group callback, which click calls *before*
    `make_context` parses the invoked subcommand, so `--json` had not been recorded yet
    and `_answer` had nothing to honour: `crom list --json` answered a refusal with prose
    and an empty stdout. Moving the work here does not catch that failure earlier, it
    makes it happen later — past the parse, where the flag is already known and the
    boundary can answer for it like any other. [LAW:no-ambient-temporal-coupling] the
    ordering is stated as a place rather than left to which of click's phases happens to
    run first.

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
        migrate.run_if_needed()
        _bootstrap_user_config()
        ctx.obj = _Session()
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
        `_delete_directory` wraps `rmtree`, `configwrite._writing` wraps the config
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


class _Session:
    """Lazily-loaded ambient state, so `crom init` need not find a config or a Chrome."""

    def __init__(self):
        self._scope: Scope | None = None

    @property
    def scope(self) -> Scope:
        if self._scope is None:
            self._scope = load_ambient()
            if self._scope.source and not self._scope.is_user:
                # Remembering the namespace here — the moment crom reads a project
                # config — is what lets `crom up thatproject/dev` work from anywhere.
                registry.remember_namespace(self._scope.namespace, self._scope.source)
        return self._scope

    def profile(self, ref_text: str) -> ResolvedProfile:
        """A profile that must already be declared — for `down` and `rm`."""
        return resolver.resolve(parse_ref(ref_text, self.scope.namespace), self.scope)

    def working(self, ref_text: str) -> ResolvedProfile:
        """A profile to work with, declared on the spot if nothing declares it yet.

        The split is the whole of crom's stance on prerequisites, stated as two calls
        rather than as a flag: a command asking *where profile X is* gets it created, a
        command asking crom to *take X away* does not. [LAW:types-are-the-program] a
        `declare=True` parameter would have made "create the profile I am about to
        delete" expressible at every call site.
        """
        return resolver.resolve_or_declare(parse_ref(ref_text, self.scope.namespace), self.scope)


def _emit(as_json: bool, payload, lines: list[str]) -> None:
    """Render one successful result. The last inch of UI, and the only place a *result*
    chooses its format — a failure has two readers at once, so it renders in
    `_answer`."""
    click.echo(_json_text(payload) if as_json else "\n".join(lines))


def _status(profile: ResolvedProfile, live: dict[str, tuple[int, ...]]) -> tuple[bool, tuple[int, ...]]:
    pids = live.get(str(profile.profile_dir), ())
    return bool(pids), pids


def _bootstrap_user_config() -> None:
    """On a machine with no user config, declare the profile a bare `crom up` expects.

    Written explicitly into the file rather than defaulted in code, so `user/default`
    cloning your real Chrome profile is a visible, editable decision and not folklore.
    """
    # Repairing first is what makes the write below safe on a user config crom cannot
    # read. `configwrite._load` raises on such a file, and this function runs before every
    # command — so an unreadable `~/.config/crom/config.toml` failed all of them, the ones
    # that would have repaired it included. [LAW:no-ambient-temporal-coupling] the
    # ordering is the repair's, and stating it here is what keeps it from being luck.
    #
    # `repair_unreadable`, not `load_user_scope`: loading resolves `chrome_binary`, which
    # would make `find_chrome()` a precondition of every command including `crom init` —
    # the one `_Session` exists to keep working on a machine with no Chrome yet. Whether a
    # file tokenizes as TOML is a question about bytes and asks nothing of the machine.
    config.repair_unreadable(user_config_file(), namespace=USER_NAMESPACE)
    # The seed comes from `model.DEFAULT_SEED`, which the project template renders too.
    # The literal `SeedChrome()` that used to sit here was the half of the disagreement
    # that happened to be right. [LAW:one-source-of-truth]
    #
    # `ensure_profile`, not `add_profile`: the goal is that the declaration *exist*, not
    # that this process be the one to write it. On a fresh machine two crom invocations
    # both find no user config and both try; `add_profile` raises FileExistsError at the
    # loser — a reported failure for a race that harmed nothing. Converging makes it a
    # no-op instead of an error to catch.
    configwrite.ensure_profile(
        user_config_file(),
        ProfileSpec(name="default", seed=DEFAULT_SEED),
        header=configwrite.USER_CONFIG_HEADER,
    )


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
    profile already declared, and `crom up` of a browser already running all
    report what is there and exit 0. Only a request for something *different*
    from what exists is refused — `crom add dev --port 9500` when `dev` is
    declared on another port names the difference and changes nothing.
    """


def _start_under_lock(profile: ResolvedProfile) -> tuple[bool, tuple[int, ...]]:
    """Bring a profile up, for a caller already holding `seed.profile_lock`.

    Hands back whether *this* call started the browser and the PIDs it is running under,
    rather than reporting it, because the three callers say different things about the
    same outcome: `up` reports "Started" or "Already running", `restart` has just
    stopped whatever was there, and `show` mentions a launch only when it had to make one.
    [LAW:effects-at-boundaries] the decision is computed here and rendered at the command.

    Written for a caller that already holds the lock, not one that takes it, because
    `seed.profile_lock` is `flock` on a fresh descriptor and blocks even within one
    process — so a `restart` built as "call down, then call up" would deadlock if the two
    halves nested, and would drop the lock between them if they did not. The critical
    section belongs to the command, which is the only participant that knows how much of
    the work has to be indivisible. [LAW:no-ambient-temporal-coupling]
    """
    if not profile.profile_dir.exists():
        # Say so before the copy, not after: a `chrome` seed moves hundreds of
        # megabytes and an unexplained pause looks like a hang.
        rendered = configwrite.render_seed(profile.seed, profile.config_dir)
        click.echo(f"Creating {profile.ref} from seed '{rendered}' …", err=True)
    seed.materialize_under_lock(profile)

    pids = chrome.find_pids(profile)
    started = not pids
    if started:
        pids = chrome.launch(profile)
    return started, pids


@main.command("up")
@click.argument("ref", required=False, default="default")
@_json_option
@click.pass_obj
def up_cmd(session: _Session, ref: str, as_json: bool):
    """Launch a profile, or report the running one. Idempotent."""
    profile = session.working(ref)
    # Seeding, the liveness check, and the launch are one critical section. Split, two
    # concurrent `crom up` calls both see no running Chrome and both launch against the
    # same profile directory and port — and because Chrome binds the CDP port well before
    # it answers on it, the loser's `_require_port_available` reports the port as held by
    # "another process" when that process is the browser it was asking for. Serialized,
    # the second caller finds the first's Chrome and reports it, which is what `up` has
    # always claimed to do.
    with seed.profile_lock(profile):
        started, pids = _start_under_lock(profile)

    verb = "Started" if started else "Already running"
    _emit(
        as_json,
        profile.describe(running=True, pids=pids),
        [f"{verb} {profile.ref} on {profile.cdp_url}"],
    )


@main.command("down")
@click.argument("ref", required=False, default="default")
@_json_option
@click.pass_obj
def down_cmd(session: _Session, ref: str, as_json: bool):
    """Stop a running profile."""
    profile = session.profile(ref)
    # Under the same lock `up` and `rm` hold. `up` keeps it from before seeding until CDP
    # answers, but a launched process is visible to `chrome.scan` as soon as `Popen`
    # returns — so an unlocked `down` could kill a Chrome that was still initialising a
    # freshly-copied user-data-dir, which is precisely the state that is not safe to
    # interrupt, and leave `up` reporting a readiness timeout for a browser someone else
    # killed. [LAW:no-ambient-temporal-coupling] a lock one participant ignores is not
    # serialising anything; `down` waiting for an in-flight `up` is also what makes
    # "down returned" mean "it is stopped" rather than "I killed what I happened to see".
    with seed.profile_lock(profile):
        pids = chrome.kill(profile)
    message = (
        f"Stopped {profile.ref} (pid {', '.join(map(str, pids))})"
        if pids
        else f"{profile.ref} was not running"
    )
    _emit(as_json, profile.describe(running=False, pids=pids), [message])


@main.command("restart")
@click.argument("ref", required=False, default="default")
@_json_option
@click.pass_obj
def restart_cmd(session: _Session, ref: str, as_json: bool):
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
                f"Stopped {profile.ref} (pid {', '.join(map(str, stopped))}); "
                f"starting it again …",
                err=True,
            )
        # The `started` flag is discarded rather than reported: `kill` has just guaranteed
        # nothing is running, so a start here is always a start, and the interesting fact
        # is what was stopped. That is what `stopped` carries.
        _, pids = _start_under_lock(profile)

    was = ", ".join(map(str, stopped))
    now = ", ".join(map(str, pids))
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
def show_cmd(session: _Session, ref: str, as_json: bool):
    """Bring a profile's window to the front, launching it if it is not running."""
    profile = session.working(ref)
    # Starting and raising under one hold, so the PIDs raised are the PIDs observed. A
    # `down` landing between the two would leave `window.raise_profile` asking macOS for a
    # process that no longer exists, and the -1719 it answers with reads as "the browser
    # exited" — true, but it would be describing a race this command could have prevented.
    #
    # The raise is inside the lock rather than after it for that reason alone; it costs one
    # osascript round trip, which is the same order as the `ps` call `_start_under_lock`
    # already makes while holding it.
    with seed.profile_lock(profile):
        started, pids = _start_under_lock(profile)
        if started:
            # Said before the raise rather than assembled with the result afterwards, so
            # the fact survives a raise that fails. Withheld Automation access is likeliest
            # on a first run — the same run likeliest to have started the browser — and a
            # user shown only the raise error would go hunting for a launch failure that
            # never happened. On stderr because for `show` the answer is the raise itself;
            # a launch on the way to it is progress, like `_start_under_lock`'s own
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
def list_cmd(session: _Session, everything: bool, as_json: bool):
    """List the profiles addressable from here."""
    scopes, unavailable = _scopes_to_list(session, everything)

    live = chrome.scan()
    records, lines = [], []
    for scope in scopes:
        for entry in resolver.resolve_all(scope):
            match entry:
                case ResolvedProfile():
                    running, pids = _status(entry, live)
                    records.append(entry.describe(running=running, pids=pids))
                    state = f"running :{entry.port}" if running else f"stopped :{entry.port}"
                    lines.append(f"  {str(entry.ref):28s}  {state}")
                case FailedProfile():
                    records.append(entry.describe())
                    lines.append(f"  {str(entry.ref):28s}  unresolved — {entry.error}")
        if not scope.profiles:
            lines.append(f"  {scope.namespace}/ — no profiles declared in {scope.source or 'user config'}")

    for namespace, error in unavailable:
        records.append({"namespace": namespace, "error": error})
        lines.append(f"  {namespace + '/':28s}  unavailable — {error}")

    _emit(as_json, records, lines)


def _scopes_to_list(session: _Session, everything: bool) -> tuple[list[Scope], list[tuple[str, str]]]:
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

    Whole values, not the part the two sides differ on. `_reject_restatement` renders
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


def _reject_restatement(
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
def add_cmd(session: _Session, name: str, seed_text: str | None, flag_texts: tuple[str, ...], port: int | None):
    """Declare a profile in the config governing this directory. Idempotent."""
    validate_name("profile name", name)
    scope = session.scope
    target = scope.source or user_config_file()
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
        # every command in the project, which is the failure the comment below is about
        # to describe going to lengths to avoid. [LAW:single-enforcer] the range rule has
        # one home; this path was bypassing it rather than needing a copy.
        port=parse_port(port, where, target),
    )
    # `_declare` creates the file when it is missing, and the header it would write is
    # the *user* scope's — which carries no `namespace` key, because only the project
    # template has one. So recreating a vanished project config from it produces a file
    # the parser rejects wholesale. The scope was read at discovery time and the file can
    # be gone by now (a `git clean`, another agent resetting the workspace), which used to
    # end in "Run `crom init` to recreate it" — a command crom is holding every argument
    # for. `write_default` writes exactly what that `crom init` would have, from the scope
    # already in hand, and is a no-op when the file is still there.
    header = configwrite.USER_CONFIG_HEADER if target == user_config_file() else ""
    if configwrite.write_default(
        target,
        namespace=None if scope.is_user else scope.namespace,
        seed=scope.default_seed,
    ):
        click.echo(f"Recreated {target}, which had been removed since crom read it", err=True)

    # What to declare if this name is free. Only a proposal: on the path where the config
    # already declares the name, `ensure_profile` writes nothing and this value is
    # discarded below for the declaration the file actually holds. It is deliberately not
    # called `declared` — naming this caller's request after the file's contents is what
    # let the two be confused on the race path this command has to survive.
    proposed = scope.profiles.get(name, spec)

    # Before the write, because `parse` refuses a file that pins one port twice
    # *wholesale*: a declaration rejected only after it landed would break every command
    # in the project — `crom rm` included — on the very file the user needs crom to
    # repair.
    config.reject_duplicate_ports({**scope.profiles, name: proposed}, target)

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
    declared = scope.profiles.get(name)
    if declared is None:
        # `ensure_profile` returning at all means the name is declared, so reaching here
        # takes a concurrent `crom rm` — or a `git checkout` over the file — landing in
        # between. Said as a `CromError` rather than left to a `KeyError`, which would
        # leave this module's exit-code contract as a traceback. [LAW:no-silent-failure]
        raise Reason.PROFILE_VANISHED.error(
            f"{target}: profile '{name}' was removed while crom was declaring it"
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
    _reject_restatement(
        f"{target}: profile '{name}' is already declared, and this asks to change it:",
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
    click.echo(f"{'Declared' if written else 'Already declared'} {profile.ref} in {target}")
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
def rm_cmd(session: _Session, ref: str, yes: bool, keep_data: bool):
    """Stop a profile if it is running, undeclare it, release its port, delete its data."""
    profile = session.profile(ref)

    # `rm` used to refuse a running profile and tell the user to run `crom down` first,
    # which made the caller responsible for establishing a state `rm` needs and `rm` can
    # reach on its own — under the very lock it already takes to make stopping safe.
    # [LAW:no-ambient-temporal-coupling] the phase transition is this command's to own;
    # exporting it as a two-command ritual left an orphan-shaped hole in the other
    # direction too, since `--keep-data` was the one path that refused without ever
    # explaining that the browser it left running was about to lose its declaration.
    #
    # Read here only to compose the prompt. The authoritative act is `chrome.kill` under
    # the lock below, which converges a profile to stopped whether or not this saw it run.
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

    scope = resolver.scope_for(profile.ref.namespace, session.scope)
    # Stopping and deleting are one critical section, for the same reason `up_cmd` holds
    # this lock across its own check-and-launch: a concurrent `crom up` can seed and
    # launch Chrome in the window between them, and `rm` would otherwise delete a live
    # browser's user-data-dir out from under it — a process crom can no longer find or
    # stop, writing into a directory that no longer exists.
    #
    # The confirmation deliberately sits *outside* the lock: holding it across an
    # interactive prompt would block every other crom process for as long as the human
    # takes to answer. Which is why `kill` runs unconditionally here rather than under
    # the `running` read taken before the prompt — that read is stale by construction,
    # and a Chrome started while the question was on screen must not survive the answer.
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
            _delete_directory(
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
        configwrite.remove_profile(scope.source or user_config_file(), profile.ref.name)
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
    # The config this project already has, else the name a new one takes. Running `crom
    # init` twice is not an error to report but a state to converge on, so the second run
    # aims at the file the first one wrote — including under the `.crom/config.toml`
    # spelling, which is a config crom must not shadow with a second one beside it.
    existing = next((here / c for c in PROJECT_CONFIG_CANDIDATES if (here / c).is_file()), None)
    target = existing or here / PROJECT_CONFIG_CANDIDATES[0]

    # `namespace` stays as the user typed it — None when they typed nothing — because
    # that absence is what makes a bare `crom init` in an initialised project a no-op
    # instead of a rename: a namespace derived from the directory name is crom's guess,
    # and a guess must not be able to contradict a name the project chose. Only the
    # written value falls back. [LAW:no-silent-failure]
    chosen_namespace = validate_name("namespace", namespace or slug_for(here.name))

    # The namespace this command *claims*: what the user typed, else crom's guess — and
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
    claimed_namespace = namespace or (None if existing else chosen_namespace)
    if claimed_namespace == USER_NAMESPACE:
        raise Reason.NAMESPACE_RESERVED.error(
            f'"{USER_NAMESPACE}" is reserved; pass a different namespace'
        )

    # Parsed here, before the file exists, through the same checkpoint that reads the
    # value back on the next command — so `crom init --seed chorme` fails naming the
    # vocabulary rather than writing a config that every later command rejects.
    # [LAW:parse-dont-validate]
    #
    # Named `chosen_seed` rather than `seed` because this module imports the `seed`
    # module, and a local of that name shadows it for the rest of the function — working
    # today only because `init_cmd` happens not to need it, and failing with an
    # AttributeError the first time someone adds a line that does.
    #
    # Anchored on `target.parent`, not on `here`: a relative seed path is parsed against
    # the config's own directory everywhere else, and `write_default` renders it back with
    # `render_seed(seed, path.parent)`. Those agree today only because
    # `PROJECT_CONFIG_CANDIDATES[0]` is the bare `.crom.toml`, so its parent *is* `here`.
    # Under the `.crom/config.toml` candidate they diverge, and `--seed ./fixtures` would
    # parse to `here/fixtures`, fail `relative_to(here/'.crom')`, and be written as this
    # machine's absolute path into a file meant to be committed — the exact outcome
    # `render_seed`'s docstring exists to prevent. [LAW:one-source-of-truth]
    base = target.parent
    stated_seed = None if seed_text is None else parse_seed(seed_text, DEFAULTS_STANZA, target, base)

    # The refusal here is the kernel's `O_CREAT | O_EXCL`, not a check of ours, so two
    # `crom init` calls racing in one directory produce one file rather than one clobbering
    # the other. What changes is only what the loser does with the answer: it reads the
    # winner's file back below and reports it, since a project that has the config it was
    # asked for has had the request met.
    wrote = configwrite.write_default(
        target,
        namespace=chosen_namespace,
        seed=DEFAULT_SEED if stated_seed is None else stated_seed,
    )

    # Read back rather than echoed from the variables above, because on the converging
    # path those variables are what crom *would* have written and the file is what the
    # project actually declares — and reporting a guessed namespace as though it were the
    # project's would be the same lie the comment above refuses to write. On the path that
    # just created the file the two are identical, so one read serves both and doubles as
    # a check on the write. [FRAMING:representation]
    declared_namespace = configwrite.value_at(target, "namespace")
    # Reading a fact obliges this command to handle every shape the file can hold it in.
    # A hand-written `.crom.toml` with no `namespace`, or one holding a number, is a file
    # that exists without configuring anything — so converging on it would print
    # "namespace 'None'" and tell the user to run a `crom up` that `config.parse` is about
    # to refuse. Said here instead, naming the fix. [LAW:parse-dont-validate] the full
    # diagnosis stays `config.parse`'s; this is only the checkpoint for the value the
    # three lines below are about to state as fact.
    if not isinstance(declared_namespace, str):
        raise Reason.CONFIG_INVALID.error(
            f"{target} exists but declares no usable `namespace` "
            f"({declared_namespace!r}), so it does not configure this project.\n"
            f'Add namespace = "{chosen_namespace}" to it, or delete it and run '
            f"`crom init` again."
        )
    # An absent `[defaults].seed` is not a missing fact: `config.parse` reads that absence
    # as `DEFAULT_SEED`, so this renders the same answer the next command will act on.
    declared_seed = configwrite.value_at(target, "defaults", "seed")
    if declared_seed is None:
        declared_seed = configwrite.render_seed(DEFAULT_SEED, base)

    _reject_restatement(
        f"{target} already configures this project, and this asks to change it:",
        (
            ("namespace", declared_namespace, namespace),
            (
                "seed",
                str(declared_seed),
                None if stated_seed is None else configwrite.render_seed(stated_seed, base),
            ),
        ),
        f"Edit {target} directly — crom writes a project config once and leaves it "
        f"yours after that. Changing the namespace also moves this project's ports and "
        f"profile directories, which is why crom will not do it for you.",
    )

    click.echo(
        f"Wrote {target} (namespace '{declared_namespace}')"
        if wrote
        else f"{target} already configures this project (namespace '{declared_namespace}')"
    )
    click.echo(f"  profiles here start from seed '{declared_seed}' — change it in [defaults]")
    click.echo(f"Run: crom up  # brings up {declared_namespace}/default")


@main.command("config")
@click.argument("ref", required=False)
@_json_option
@click.pass_obj
def config_cmd(session: _Session, ref: str | None, as_json: bool):
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
        notes = {str(item.flag): _note(item) for item in profile.provenance.emitted}
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
        ]

    _emit(as_json, payload, lines)


@main.command("port")
@click.argument("ref", required=False, default="default")
@click.pass_obj
def port_cmd(session: _Session, ref: str):
    """Print a profile's CDP port and nothing else."""
    click.echo(session.working(ref).port)


@main.command("env")
@click.argument("ref", required=False, default="default")
@click.pass_obj
def env_cmd(session: _Session, ref: str):
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
def mcp_cmd(session: _Session, ref: str, path: str):
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
            _delete_directory(
                staged.path, f"Run `crom clean {staged.path}` again once that is fixed."
            )
            click.echo(f"Deleted {staged.path} ({size} reclaimed)")


def _delete_directory(directory: Path, retry: str) -> None:
    """Remove one directory crom owns, as a `CromError` rather than a traceback.

    `shutil.rmtree` raises a bare `OSError` — `FileNotFoundError` for an entry that
    vanishes mid-walk, `ENOTEMPTY` for one that appears — and the path alone does not say
    what is left to try. Both callers meet that same race for the same reason: `crom rm`
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
