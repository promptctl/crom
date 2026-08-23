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
import re
import shutil
from pathlib import Path

import click

from . import chrome, config, configwrite, mcp, migrate, registry, resolve as resolver, seed
from .config import discover, load_ambient, load_user_scope, parse_flags, parse_seed
from .model import (
    Conflict,
    CromError,
    NotFound,
    ProfileRef,
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
    if user_config_file().exists():
        return
    configwrite.add_profile(
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
    if not profile.profile_dir.exists():
        # Say so before the copy, not after: a `chrome` seed moves hundreds of megabytes
        # and an unexplained pause looks like a hang.
        click.echo(f"Creating {profile.ref} from seed '{configwrite.render_seed(profile.seed)}' …", err=True)
    seed.materialize(profile)

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
    scopes = [session.scope]
    if not session.scope.is_user:
        scopes.append(load_user_scope())
    if everything:
        scopes.extend(
            resolver.scope_for(namespace, session.scope)
            for namespace in sorted(registry.namespaces())
            if namespace != session.scope.namespace
        )

    live = chrome.scan()
    records, lines = [], []
    for scope in scopes:
        for profile in resolver.resolve_all(scope):
            running, pids = _status(profile, live)
            records.append(profile.describe(running=running, pids=pids))
            state = f"running :{profile.port}" if running else f"stopped :{profile.port}"
            lines.append(f"  {str(profile.ref):28s}  {state}")
        if not scope.profiles:
            lines.append(f"  {scope.namespace}/ — no profiles declared in {scope.source or 'user config'}")

    _emit(as_json, records, lines)


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
    spec = ProfileSpec(
        name=name,
        flags=parse_flags(list(flags), f"[profiles.{name}]", target),
        seed=parse_seed(seed_text, f"[profiles.{name}]", target, scope.config_dir),
        port=port,
    )
    # Everything that can refuse this profile runs before anything is written. Declaring
    # first and resolving second would leave a rejected profile sitting in the file: a
    # pinned port that collides is refused *here*, but the declaration carrying it would
    # already be on disk, and the config parser rejects that file wholesale on the next
    # load — every command in the project, `crom rm` included, would then fail to read
    # the very file the user would need crom to fix.
    config.reject_duplicate_ports({**scope.profiles, name: spec}, target)
    profile = resolver.resolve_spec(ProfileRef(scope.namespace, name), scope, spec)

    try:
        configwrite.add_profile(target, spec, header=configwrite.USER_CONFIG_HEADER)
    except FileExistsError as e:
        raise Conflict(str(e)) from e

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
    if chrome.is_running(profile):
        raise Conflict(f"{profile.ref} is running. Run: crom down {profile.ref}")

    if not keep_data and profile.profile_dir.exists() and not yes:
        size = _human_size(profile.profile_dir)
        click.confirm(
            f"Delete {profile.profile_dir} ({size}) — logins, cookies, and history for "
            f"{profile.ref}?",
            abort=True,
        )

    scope = resolver.scope_for(profile.ref.namespace, session.scope)
    configwrite.remove_profile(scope.source or user_config_file(), profile.ref.name)
    registry.forget(profile.ref)
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
    for key, value in {
        "CROM_PROFILE": str(profile.ref),
        "CROM_PORT": str(profile.port),
        "CROM_CDP_URL": profile.cdp_url,
        "CROM_PROFILE_DIR": str(profile.profile_dir),
    }.items():
        click.echo(f"export {key}={value}")


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
    return re.sub(r"[^a-z0-9._-]+", "-", text.lower()).strip("-") or "project"


def _human_size(directory: Path) -> str:
    total = sum(f.stat().st_size for f in directory.rglob("*") if f.is_file())
    for unit in ("B", "KB", "MB", "GB"):
        if total < 1024 or unit == "GB":
            return f"{total:.0f}{unit}"
        total /= 1024
