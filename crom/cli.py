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
import re
import shlex
import shutil
from pathlib import Path
from stat import S_ISREG

import click

from . import chrome, config, configwrite, mcp, migrate, registry, resolve as resolver, seed
from .config import discover, load_ambient, load_user_scope, parse_flags, parse_port, parse_seed
from .model import (
    NAME_LIMIT,
    Conflict,
    CromError,
    FailedProfile,
    NotFound,
    ProfileSpec,
    ResolvedProfile,
    Scope,
    SeedChrome,
    parse_ref,
    validate_name,
)
from .paths import PROJECT_CONFIG_CANDIDATES, USER_NAMESPACE, user_config_file

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


class CromGroup(click.Group):
    """Turns core errors into the CLI's exit-code contract, in one place."""

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except CromError as error:
            code = next(c for kind, c in _EXIT_CODES if isinstance(error, kind))
            raise _Failure(str(error), code) from error


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
        return resolver.resolve(parse_ref(ref_text, self.scope.namespace), self.scope)


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
    # `ensure_profile`, not `add_profile`: the goal is that the declaration *exist*, not
    # that this process be the one to write it. On a fresh machine two crom invocations
    # both find no user config and both try; `add_profile` raises FileExistsError at the
    # loser, which is not a CromError and so escapes the CLI's exit-code contract as a
    # traceback. Converging makes the race a no-op instead of an error to catch.
    configwrite.ensure_profile(
        user_config_file(),
        ProfileSpec(name="default", seed=SeedChrome()),
        header=configwrite.USER_CONFIG_HEADER,
    )


@click.group(cls=CromGroup, invoke_without_command=True)
@click.pass_context
def main(ctx):
    """crom — per-project Chrome profiles with stable CDP ports."""
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
    profile = session.profile(ref)
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
    default="fresh",
    help="fresh | chrome | chrome:<Profile> | ./path — where the profile's data comes from.",
)
@click.option("--flag", "flags", multiple=True, help="Chrome flag; repeatable.")
@click.option("--port", type=int, default=None, help="Pin the CDP port instead of letting crom assign one.")
@click.pass_obj
def add_cmd(session: _Session, name: str, seed_text: str, flags: tuple[str, ...], port: int | None):
    """Declare a new profile in the config governing this directory."""
    validate_name("profile name", name)
    scope = session.scope
    target = scope.source or user_config_file()
    where = f"[profiles.{name}]"
    spec = ProfileSpec(
        name=name,
        flags=parse_flags(list(flags), where, target),
        seed=parse_seed(seed_text, where, target, scope.config_dir),
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
    # the *user* scope's — which carries no `namespace` key, because only `init_project`'s
    # template does. So recreating a vanished project config from it produces a file the
    # parser rejects wholesale. The scope was read at discovery time and the file can be
    # gone by now (a `git clean`, another agent resetting the workspace), so say so
    # instead of writing a config crom cannot read back.
    header = configwrite.USER_CONFIG_HEADER if target == user_config_file() else ""
    if target != user_config_file() and not target.exists():
        raise NotFound(
            f"{target} no longer exists — the project config crom discovered has been "
            f"removed. Run `crom init` to recreate it."
        )

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
    click.echo(f"  port {profile.port} · {profile.profile_dir}")
    click.echo(f"Run: crom up {profile.ref}")


@main.command("rm")
@click.argument("ref")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--keep-data", is_flag=True, help="Undeclare the profile but leave its directory.")
@click.pass_obj
def rm_cmd(session: _Session, ref: str, yes: bool, keep_data: bool):
    """Undeclare a profile, release its port, and delete its data."""
    profile = session.profile(ref)

    def refuse_if_running() -> None:
        if chrome.is_running(profile):
            raise Conflict(f"{profile.ref} is running. Run: crom down {profile.ref}")

    # Fail before prompting. A running profile is the ordinary reason `rm` is refused,
    # and asking someone to confirm a deletion crom is about to refuse anyway is worse
    # than not asking. The authoritative check is the one under the lock below.
    refuse_if_running()

    if not keep_data and profile.profile_dir.exists() and not yes:
        size = _human_size(profile.profile_dir)
        click.confirm(
            f"Delete {profile.profile_dir} ({size}) — logins, cookies, and history for "
            f"{profile.ref}?",
            abort=True,
        )

    scope = resolver.scope_for(profile.ref.namespace, session.scope)
    # The liveness check and the delete are one critical section, for the same reason
    # `up_cmd` holds this lock across its own check-and-launch: a concurrent `crom up`
    # can seed and launch Chrome in the window between them, and `rm` would then delete
    # a live browser's user-data-dir out from under it — a process crom can no longer
    # find or stop, writing into a directory that no longer exists.
    #
    # The confirmation deliberately sits *outside* the lock: holding it across an
    # interactive prompt would block every other crom process for as long as the human
    # takes to answer. So liveness is re-read here rather than carried over from the
    # check above. [LAW:no-ambient-temporal-coupling] the guarantee comes from state read
    # under the lock, not from an earlier check still happening to hold.
    with seed.profile_lock(profile):
        refuse_if_running()
        # Release the reservation before removing the declaration, not after. Both
        # orderings can be interrupted, but they strand different things: undeclaring
        # first leaves a port held by a profile no longer nameable, so no command can
        # reach it to retry. Releasing first leaves a declared profile without a
        # reservation, which the next resolve heals by assigning one. Between two
        # interruptible steps, take the one whose failure is recoverable.
        registry.forget(profile.ref)
        configwrite.remove_profile(scope.source or user_config_file(), profile.ref.name)
        if not keep_data and profile.profile_dir.exists():
            shutil.rmtree(profile.profile_dir)
    click.echo(f"Removed {profile.ref}")


@main.command("init")
@click.argument("namespace", required=False)
def init_cmd(namespace: str | None):
    """Create a .crom.toml here so this project gets its own namespace."""
    here = Path.cwd()
    existing = next((here / c for c in PROJECT_CONFIG_CANDIDATES if (here / c).is_file()), None)
    if existing:
        raise Conflict(f"{existing} already exists")

    namespace = validate_name("namespace", namespace or _slug(here.name))
    if namespace == USER_NAMESPACE:
        raise Conflict(f'"{USER_NAMESPACE}" is reserved; pass a different namespace')

    target = here / PROJECT_CONFIG_CANDIDATES[0]
    configwrite.init_project(target, namespace)
    click.echo(f"Wrote {target} (namespace '{namespace}')")
    click.echo(f"Run: crom up  # brings up {namespace}/default")


@main.command("config")
@click.argument("ref", required=False)
@click.option("--json", "as_json", is_flag=True)
@click.pass_obj
def config_cmd(session: _Session, ref: str | None, as_json: bool):
    """Show which config is in effect, and how a profile resolves under it."""
    scope = session.scope
    payload = {
        "namespace": scope.namespace,
        "source": str(scope.source) if scope.source else None,
        "discovered_from": str(discover() or ""),
        "profiles_root": str(scope.profiles_root),
        "chrome_binary": str(scope.chrome_binary),
        "profiles": sorted(scope.profiles),
    }
    lines = [
        f"namespace      {scope.namespace}",
        f"config         {scope.source or '(none — implicit user scope)'}",
        f"profiles_root  {scope.profiles_root}",
        f"chrome_binary  {scope.chrome_binary}",
        f"profiles       {', '.join(sorted(scope.profiles)) or '(none)'}",
    ]

    if ref:
        profile = session.profile(ref)
        running, pids = _status(profile, chrome.scan())
        payload["resolved"] = {**profile.describe(running=running, pids=pids), "argv": list(profile.argv)}
        lines += ["", f"{profile.ref} resolves to:", *(f"  {arg}" for arg in profile.argv)]

    _emit(as_json, payload, lines)


@main.command("port")
@click.argument("ref", required=False, default="default")
@click.pass_obj
def port_cmd(session: _Session, ref: str):
    """Print a profile's CDP port and nothing else."""
    click.echo(session.profile(ref).port)


@main.command("env")
@click.argument("ref", required=False, default="default")
@click.pass_obj
def env_cmd(session: _Session, ref: str):
    """Print shell exports for a profile: eval "$(crom env dev)"."""
    profile = session.profile(ref)
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
    profile = session.profile(ref)
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


def _slug(text: str) -> str:
    """A directory name turned into something `validate_name` will accept.

    Stripping `._-` from both ends, not just `-`: `.` and `_` survive the substitution
    because they are inside the allowed class, so a directory named `.dotfiles` or
    `_internal` used to slugify unchanged and then fail name validation — a confusing
    error from a command whose whole promise is that it works in any directory. Stripping
    them also lets an all-punctuation name fall through to the `project` fallback.

    Truncated to the same 64 characters `validate_name` allows, and re-stripped
    afterwards so the cut cannot leave a trailing separator that fails on its own. A
    deeply nested build directory or a long branch checkout is a name crom can handle,
    not a reason to make the user pick one by hand.
    """
    slug = re.sub(r"[^a-z0-9._-]+", "-", text.lower()).strip("._-")
    return slug[:NAME_LIMIT].strip("._-") or "project"


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
