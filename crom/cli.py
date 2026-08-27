"""Maps commands onto crom's core and renders the result for a human or a machine.

This is the outer boundary: everything below it raises `CromError` and returns data;
everything user-visible — exit codes, stderr, JSON shape — is decided here.

[LAW:effects-at-boundaries] The core computes descriptions; this layer performs and
prints them. [CLI binding] stdout carries the answer, stderr carries diagnostics, and
exit codes are a contract a script can branch on:

    0  success            3  no such profile / namespace / config
    1  failure            4  port or declaration conflict
    2  usage error (click's own)
"""

import json
import os
import shlex
import shutil
from pathlib import Path
from stat import S_ISREG

import click

from . import chrome, config, configwrite, mcp, migrate, registry, resolve as resolver, seed
from .config import discover, load_ambient, load_user_scope, parse_flags, parse_port, parse_seed
from .model import (
    DEFAULT_SEED,
    USER_NAMESPACE,
    Conflict,
    CromError,
    FailedProfile,
    NotFound,
    ProfileSpec,
    ResolvedProfile,
    Scope,
    parse_ref,
    slug_for,
    validate_name,
)
from .paths import PROJECT_CONFIG_CANDIDATES, user_config_file

EXIT_FAILURE = 1
EXIT_NOT_FOUND = 3
EXIT_CONFLICT = 4

# Which exception means which exit code. [LAW:dataflow-not-control-flow] the mapping is
# a table consulted once, not a chain of except clauses repeated per command.
_EXIT_CODES = ((NotFound, EXIT_NOT_FOUND), (Conflict, EXIT_CONFLICT), (CromError, EXIT_FAILURE))


class _Failure(click.ClickException):
    def __init__(self, message: str, exit_code: int):
        super().__init__(message)
        self.exit_code = exit_code


# How `crom --help` groups its commands, as data rather than as prose that has to be
# re-edited alongside every new command. Alphabetical order — click's default — presented
# eleven commands as a flat undifferentiated list, so the help named every piece and
# nothing about how the pieces fit; a reader could learn that `mcp`, `port` and `forget`
# exist without learning that the first two are things you do *to a running profile* and
# the third is not. [FRAMING:representation] a listing is a map of the CLI, and the CLI's
# real structure is these three jobs.
_COMMAND_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Run a browser", ("up", "down", "list")),
    ("Point tools at one", ("mcp", "env", "port")),
    ("Declare what exists", ("init", "add", "rm", "config", "forget")),
)


class CromGroup(click.Group):
    """Turns core errors into the CLI's exit-code contract, in one place."""

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except CromError as error:
            code = next(c for kind, c in _EXIT_CODES if isinstance(error, kind))
            raise _Failure(str(error), code) from error

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
    """Render one result. The last inch of UI, and the only place output format matters."""
    click.echo(json.dumps(payload, indent=2) if as_json else "\n".join(lines))


def _status(profile: ResolvedProfile, live: dict[str, tuple[int, ...]]) -> tuple[bool, tuple[int, ...]]:
    pids = live.get(str(profile.profile_dir), ())
    return bool(pids), pids


def _bootstrap_user_config() -> None:
    """On a machine with no user config, declare the profile a bare `crom up` expects.

    Written explicitly into the file rather than defaulted in code, so `user/default`
    cloning your real Chrome profile is a visible, editable decision and not folklore.
    """
    # Loading the scope first is what makes the write below safe on a user config crom
    # cannot parse. `configwrite._load` raises on such a file, and this function runs
    # before every command — so an unreadable `~/.config/crom/config.toml` failed all of
    # them, the ones that would have repaired it included. `load_user_scope` goes through
    # `load_file`, which resets a config it cannot read, so by the time `ensure_profile`
    # opens the file it is one crom can parse. [LAW:no-ambient-temporal-coupling] the
    # ordering is the repair's, and stating it here is what keeps it from being luck.
    load_user_scope()
    # The seed comes from `model.DEFAULT_SEED`, which the project template renders too.
    # The literal `SeedChrome()` that used to sit here was the half of the disagreement
    # that happened to be right. [LAW:one-source-of-truth]
    #
    # `ensure_profile`, not `add_profile`: the goal is that the declaration *exist*, not
    # that this process be the one to write it. On a fresh machine two crom invocations
    # both find no user config and both try; `add_profile` raises FileExistsError at the
    # loser, which is not a CromError and so escapes the CLI's exit-code contract as a
    # traceback. Converging makes the race a no-op instead of an error to catch.
    configwrite.ensure_profile(
        user_config_file(),
        ProfileSpec(name="default", seed=DEFAULT_SEED),
        header=configwrite.USER_CONFIG_HEADER,
    )


@click.group(cls=CromGroup, invoke_without_command=True)
@click.pass_context
def main(ctx):
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

    crom does the setup step for you rather than naming it: a profile you refer
    to but never declared is declared, and a config file crom cannot read is
    reset to the default with your original kept beside it as `<name>.broken`.
    Both are reported on stderr as they happen.
    """
    migrate.run_if_needed()
    _bootstrap_user_config()
    ctx.obj = _Session()
    if ctx.invoked_subcommand is None:
        ctx.invoke(up_cmd, ref="default", as_json=False)


@main.command("up")
@click.argument("ref", required=False, default="default")
@click.option("--json", "as_json", is_flag=True, help="Emit the profile record as JSON.")
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

    verb = "Started" if started else "Already running"
    _emit(
        as_json,
        profile.describe(running=True, pids=pids),
        [f"{verb} {profile.ref} on {profile.cdp_url}"],
    )


@main.command("down")
@click.argument("ref", required=False, default="default")
@click.option("--json", "as_json", is_flag=True)
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


@main.command("list")
@click.option("--all", "everything", is_flag=True, help="Include every namespace crom knows.")
@click.option("--json", "as_json", is_flag=True)
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
    from `scope_for` — and `crom forget` is the documented cleanup for exactly that. One
    stale entry used to abort the entire listing, so the command that would have shown
    the user which namespace was broken was the one command that could not run. Each
    namespace is isolated and reported by name instead. [LAW:no-silent-failure] nothing
    is skipped quietly: the failure is a row in the output, human and JSON alike.
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
@click.option("--flag", "flags", multiple=True, help="Chrome flag; repeatable.")
@click.option("--port", type=int, default=None, help="Pin the CDP port instead of letting crom assign one.")
@click.pass_obj
def add_cmd(session: _Session, name: str, seed_text: str | None, flags: tuple[str, ...], port: int | None):
    """Declare a new profile in the config governing this directory."""
    validate_name("profile name", name)
    scope = session.scope
    target = scope.source or user_config_file()
    where = f"[profiles.{name}]"
    spec = ProfileSpec(
        name=name,
        flags=parse_flags(list(flags), where, target),
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
    # Every reason to refuse this profile is checked before anything is persisted, and
    # the two orderings that look equivalent are not:
    #
    # Writing before resolving would leave a refused profile in the file — a colliding
    # pinned port is rejected during resolution, but its declaration would already be on
    # disk, and the parser rejects such a file wholesale on the next load. Every command
    # in the project, `crom rm` included, would then fail on the file the user needs crom
    # to repair.
    #
    # Resolving before checking the name would persist a port for a profile that is about
    # to be refused: resolution reserves one, so `crom add ci --port 9500` against an
    # existing `ci` would move the real `ci` onto 9500 and break whatever already points
    # at its old port — a failed command silently repointing a live profile.
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

    if configwrite.declares(target, name):
        raise Conflict(f"{target}: profile '{name}' is already declared")
    config.reject_duplicate_ports({**scope.profiles, name: spec}, target)
    profile = resolver.resolve_spec(scope, spec)

    # Resolution above already persisted a port reservation. If the declaration does not
    # land, that reservation is for a profile no config declares — unreachable by `crom
    # rm` (which resolves by name first) and able to refuse a legitimate profile the port
    # later via `_reject_foreign_claim`. Any failure releases it, not only the
    # anticipated one: a permission error or a full disk strands it exactly the same way.
    try:
        configwrite.add_profile(target, spec, header=header)
    except FileExistsError as e:
        # Deliberately no cleanup. `profile.ref` is the profile's shared identity, not
        # this attempt's, and FileExistsError means a declaration for that name now
        # exists — written by whichever concurrent `crom add` won. That declaration owns
        # the reservation, so releasing it here would strip a live profile of its port
        # and silently move it on the next resolve.
        raise Conflict(str(e)) from e
    except BaseException:
        # The write failed for some other reason, so no declaration of ours landed. Only
        # release the port if nothing else claimed the name in the meantime.
        if not configwrite.declares(target, name):
            registry.forget(profile.ref)
        raise

    click.echo(f"Declared {profile.ref} in {target}")
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

    # Assembled rather than templated because `_human_size` walks the whole profile
    # directory: folding it into a comprehension over both consequences would measure a
    # gigabyte of Chrome data on the `--keep-data` path that is not going to delete it.
    consequences = []
    if running:
        consequences.append(f"stop the browser running on port {profile.port}")
    if deletes_data:
        size = _human_size(profile.profile_dir)
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
            _delete_profile_data(profile)
        # Release the reservation before removing the declaration, not after. Both
        # orderings can be interrupted, but they strand different things: undeclaring
        # first leaves a port held by a profile no longer nameable, so no command can
        # reach it to retry. Releasing first leaves a declared profile without a
        # reservation, which the next resolve heals by assigning one. Between two
        # interruptible steps, take the one whose failure is recoverable.
        registry.forget(profile.ref)
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
    """Create a .crom.toml here so this project gets its own namespace."""
    here = Path.cwd()
    existing = next((here / c for c in PROJECT_CONFIG_CANDIDATES if (here / c).is_file()), None)
    if existing:
        raise Conflict(f"{existing} already exists")

    namespace = validate_name("namespace", namespace or slug_for(here.name))
    if namespace == USER_NAMESPACE:
        raise Conflict(f'"{USER_NAMESPACE}" is reserved; pass a different namespace')

    target = here / PROJECT_CONFIG_CANDIDATES[0]
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
    # the config's own directory everywhere else, and `init_project` renders it back with
    # `render_seed(seed, path.parent)`. Those agree today only because
    # `PROJECT_CONFIG_CANDIDATES[0]` is the bare `.crom.toml`, so its parent *is* `here`.
    # Under the `.crom/config.toml` candidate they diverge, and `--seed ./fixtures` would
    # parse to `here/fixtures`, fail `relative_to(here/'.crom')`, and be written as this
    # machine's absolute path into a file meant to be committed — the exact outcome
    # `render_seed`'s docstring exists to prevent. [LAW:one-source-of-truth]
    base = target.parent
    chosen_seed = DEFAULT_SEED if seed_text is None else parse_seed(seed_text, "[defaults]", target, base)
    configwrite.init_project(target, namespace, chosen_seed)
    click.echo(f"Wrote {target} (namespace '{namespace}')")
    click.echo(
        f"  profiles here start from seed "
        f"'{configwrite.render_seed(chosen_seed, base)}' — change it in [defaults]"
    )
    click.echo(f"Run: crom up  # brings up {namespace}/default")


@main.command("config")
@click.argument("ref", required=False)
@click.option("--json", "as_json", is_flag=True)
@click.pass_obj
def config_cmd(session: _Session, ref: str | None, as_json: bool):
    """Show which config is in effect, and how a profile resolves under it."""
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
        payload["resolved"] = {
            **profile.describe(running=running, pids=pids),
            "argv": list(profile.argv),
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
            *(f"  {arg}" for arg in profile.argv),
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


@main.command("mcp")
@click.argument("ref", required=False, default="default")
@click.option("--path", "path", default=".mcp.json", help="File to write.")
@click.pass_obj
def mcp_cmd(session: _Session, ref: str, path: str):
    """Wire chrome-devtools-mcp at a profile by writing .mcp.json here."""
    profile = session.working(ref)
    try:
        mcp.write(profile.port, Path(path))
    except ValueError as e:
        raise CromError(str(e)) from e
    click.echo(f"Wrote {path} wired to {profile.ref} ({profile.cdp_url})")


@main.command("forget")
@click.argument("namespace")
def forget_cmd(namespace: str):
    """Drop a namespace from the registry, releasing its reserved ports."""
    released = registry.forget_namespace(validate_name("namespace", namespace))
    click.echo(f"Forgot namespace '{namespace}' ({released} port reservation(s) released)")


def _delete_profile_data(profile: ResolvedProfile) -> None:
    """Remove a profile's user-data-dir, as a `CromError` rather than a traceback.

    `shutil.rmtree` raises a bare `OSError` — `FileNotFoundError` for an entry that
    vanishes mid-walk, `ENOTEMPTY` for one that appears — and `CromGroup.invoke` catches
    only `CromError`, so those escaped the CLI's exit-code contract entirely. That became
    reachable when `rm` started stopping the browser itself instead of refusing: a Chrome
    helper outliving `chrome.kill` can still be writing here for a moment.

    The message names the retry because the caller ordered the delete first precisely so
    that one exists — the profile is still declared when this raises.
    """
    try:
        shutil.rmtree(profile.profile_dir)
    except OSError as e:
        raise CromError(
            f"could not delete {profile.profile_dir}: {e}\n"
            f"'{profile.ref}' is still declared — run `crom rm {profile.ref}` again."
        ) from e


def _human_size(directory: Path) -> str:
    """What deleting this directory would reclaim, for the confirmation prompt.

    `lstat` rather than `stat`: a symlink's target is not deleted with the profile, so
    counting the target's size would overstate what is about to be lost — and a dangling
    link would raise rather than measure. Only the profile's own regular files count.

    A file that vanishes mid-walk is skipped. A raw `OSError` here is not a `CromError`,
    so it would escape the CLI's exit-code contract as a traceback — thrown by a prompt
    whose only job is to be helpful before a destructive act.

    `os.walk(followlinks=False)` states the no-following guarantee at the call site.
    `rglob` happens to behave the same way on 3.12, but that is a property of pathlib's
    recursive selector rather than something this code asks for.
    """
    total = 0
    for dirpath, _dirnames, filenames in os.walk(directory, followlinks=False):
        for name in filenames:
            try:
                info = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if S_ISREG(info.st_mode):
                total += info.st_size
    for unit in ("B", "KB", "MB", "GB"):
        if total < 1024 or unit == "GB":
            return f"{total:.0f}{unit}"
        total /= 1024
