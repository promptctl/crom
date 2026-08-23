"""Discovers a project's crom config and parses any config file into a `Scope`.

This module is the border checkpoint for everything a human writes on disk
([LAW:parse-dont-validate]): a config file goes in as untyped TOML and comes out as a
`Scope` whose every field is already legal — names validated, seeds recognised, paths
made absolute, reserved Chrome switches rejected. Nothing downstream re-checks a key,
because downstream code only ever sees a `Scope`.
"""

import os
import tomllib
from pathlib import Path

from .browser import find_chrome
from .model import (
    Conflict,
    CromError,
    NotFound,
    ProfileSpec,
    Scope,
    Seed,
    SeedChrome,
    SeedFresh,
    SeedPath,
    validate_name,
)
from .paths import (
    PROJECT_CONFIG_CANDIDATES,
    USER_NAMESPACE,
    default_profiles_root,
    user_config_file,
)

# Switches crom owns: they carry the profile's identity and its CDP contract, and
# `chrome.find_pids` matches on --user-data-dir to decide what is running. A config
# that set them would make crom's map of reality wrong, so the checkpoint rejects them.
# [LAW:single-enforcer] this is the one place that decision is made.
RESERVED_SWITCHES = frozenset(
    {
        "--user-data-dir",
        "--remote-debugging-port",
        "--remote-debugging-pipe",
    }
)

_SCOPE_KEYS = frozenset({"namespace", "chrome_binary", "state_dir", "defaults", "profiles"})
_DEFAULTS_KEYS = frozenset({"flags", "env", "seed"})
_PROFILE_KEYS = frozenset({"flags", "env", "seed", "port"})


# --- discovery ---------------------------------------------------------------------


def discover(start: Path | None = None) -> Path | None:
    """Find the project config governing `start`, walking up to the filesystem root.

    First hit wins and the walk stops there. [LAW:one-source-of-truth] ancestor configs
    are never merged into descendants: a profile's declaration has one file behind it,
    and `crom config` can always name that file.
    """
    override = os.environ.get("CROM_CONFIG")
    if override:
        path = Path(override).expanduser().resolve()
        if not path.exists():
            raise NotFound(f"CROM_CONFIG points at {path}, which does not exist")
        return path

    directory = (start or Path.cwd()).resolve()
    for candidate_dir in (directory, *directory.parents):
        for relative in PROJECT_CONFIG_CANDIDATES:
            candidate = candidate_dir / relative
            if candidate.is_file():
                return candidate
    return None


# --- parsing -----------------------------------------------------------------------


def _reject_unknown(table: dict, allowed: frozenset[str], where: str, source: Path) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise CromError(
            f"{source}: unknown key(s) in {where}: {', '.join(unknown)} "
            f"(known: {', '.join(sorted(allowed))})"
        )


def parse_flags(raw, where: str, source: Path) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(f, str) for f in raw):
        raise CromError(f"{source}: {where}.flags must be a list of strings")
    for flag in raw:
        switch = flag.split("=", 1)[0]
        if switch in RESERVED_SWITCHES:
            raise CromError(
                f"{source}: {where}.flags may not set {switch} — crom owns it "
                f"(it defines the profile's data directory and CDP port)"
            )
    return tuple(raw)


def parse_env(raw, where: str, source: Path) -> dict[str, str]:
    if not isinstance(raw, dict) or not all(isinstance(v, str) for v in raw.values()):
        raise CromError(f"{source}: {where}.env must be a table of string values")
    return dict(raw)


def parse_seed(raw, where: str, source: Path, config_dir: Path) -> Seed:
    """Turn a seed string into one of the three real sources.

    The vocabulary is closed: `fresh`, `chrome`, `chrome:<Profile Name>`, or a path
    (which must start with `.`, `/`, or `~` so a typo'd keyword can never be mistaken
    for a relative path that happens not to exist yet).
    """
    if not isinstance(raw, str):
        raise CromError(f"{source}: {where}.seed must be a string")
    if raw == "fresh":
        return SeedFresh()
    if raw == "chrome":
        return SeedChrome()
    if raw.startswith("chrome:"):
        return SeedChrome(profile=raw.split(":", 1)[1])
    if raw[:1] in (".", "/", "~"):
        return SeedPath((config_dir / Path(raw).expanduser()).resolve())
    raise CromError(
        f"{source}: {where}.seed = {raw!r} is not recognised. Use 'fresh', 'chrome', "
        f"'chrome:<Profile Name>', or a path beginning with './', '/', or '~'."
    )


def parse_port(raw, where: str, source: Path) -> int | None:
    if raw is None:
        return None
    if not isinstance(raw, int) or isinstance(raw, bool) or not (1 <= raw <= 65535):
        raise CromError(f"{source}: {where}.port must be an integer in 1..65535")
    return raw


def parse(text: str, source: Path, *, namespace: str | None = None) -> Scope:
    """Parse config text into a Scope. `namespace` forces one (the `user` scope)."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise CromError(f"{source}: invalid TOML: {e}") from e

    _reject_unknown(data, _SCOPE_KEYS, "the top level", source)

    if namespace is None:
        declared = data.get("namespace")
        if not isinstance(declared, str):
            raise CromError(
                f'{source}: missing required key `namespace`. Add e.g. namespace = "myapp" — '
                f"it is how this project's profiles and ports stay clear of every other project's."
            )
        namespace = validate_name("namespace", declared)
        if namespace == USER_NAMESPACE:
            raise CromError(
                f'{source}: namespace "{USER_NAMESPACE}" is reserved for your personal '
                f"profiles in {user_config_file()}. Choose another name."
            )
    elif "namespace" in data:
        raise CromError(
            f"{source}: this file always defines the `{namespace}` namespace; "
            f"remove the `namespace` key."
        )

    config_dir = source.parent

    state_dir = data.get("state_dir")
    if state_dir is not None and not isinstance(state_dir, str):
        raise CromError(f"{source}: state_dir must be a string path")
    profiles_root = (
        (config_dir / Path(state_dir).expanduser()).resolve()
        if state_dir
        else default_profiles_root()
    )

    chrome_binary = data.get("chrome_binary")
    if chrome_binary is not None and not isinstance(chrome_binary, str):
        raise CromError(f"{source}: chrome_binary must be a string path")
    binary = Path(chrome_binary).expanduser() if chrome_binary else find_chrome()

    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        raise CromError(f"{source}: [defaults] must be a table")
    _reject_unknown(defaults, _DEFAULTS_KEYS, "[defaults]", source)

    default_seed = (
        parse_seed(defaults["seed"], "[defaults]", source, config_dir)
        if "seed" in defaults
        else SeedFresh()
    )

    raw_profiles = data.get("profiles", {})
    if not isinstance(raw_profiles, dict):
        raise CromError(f"{source}: [profiles] must be a table of tables")

    profiles: dict[str, ProfileSpec] = {}
    for name, raw in raw_profiles.items():
        where = f"[profiles.{name}]"
        if not isinstance(raw, dict):
            raise CromError(f"{source}: {where} must be a table")
        _reject_unknown(raw, _PROFILE_KEYS, where, source)
        validate_name("profile name", name)
        profiles[name] = ProfileSpec(
            name=name,
            flags=parse_flags(raw.get("flags", []), where, source),
            env=parse_env(raw.get("env", {}), where, source),
            seed=parse_seed(raw["seed"], where, source, config_dir) if "seed" in raw else None,
            port=parse_port(raw.get("port"), where, source),
        )

    reject_duplicate_ports(profiles, source)

    return Scope(
        namespace=namespace,
        source=source,
        profiles_root=profiles_root,
        chrome_binary=binary,
        default_flags=parse_flags(defaults.get("flags", []), "[defaults]", source),
        default_env=parse_env(defaults.get("env", {}), "[defaults]", source),
        default_seed=default_seed,
        profiles=profiles,
    )


def reject_duplicate_ports(profiles: dict[str, ProfileSpec], source: Path) -> None:
    """Refuse a set of declarations in which two profiles pin the same port.

    Public because `crom add` checks the declarations it is *about* to write through
    this same function. [LAW:single-enforcer] one rule, one implementation — a config
    that would be rejected on load is rejected before it reaches the file, rather than
    written and then discovered to be unloadable.
    """
    claimed: dict[int, str] = {}
    for spec in profiles.values():
        if spec.port is None:
            continue
        if spec.port in claimed:
            # Conflict, not a bare CromError: two profiles claiming one port is the
            # exit-4 case the CLI contract promises, whether it reaches us from a file
            # on load or from `crom add` checking a declaration before it writes it.
            raise Conflict(
                f"{source}: profiles '{claimed[spec.port]}' and '{spec.name}' both "
                f"pin port {spec.port}"
            )
        claimed[spec.port] = spec.name


def load_file(source: Path, *, namespace: str | None = None) -> Scope:
    if not source.is_file():
        raise NotFound(f"config file not found: {source}")
    return parse(source.read_text(), source, namespace=namespace)


def load_user_scope() -> Scope:
    """The `user` namespace — your personal profiles, from a fixed path.

    A machine with no user config still has a `user` scope; it simply declares no
    profiles yet. [LAW:no-defensive-null-guards] callers never ask whether the file
    exists, because a Scope is always what they get.
    """
    source = user_config_file()
    if source.is_file():
        return load_file(source, namespace=USER_NAMESPACE)
    return Scope(
        namespace=USER_NAMESPACE,
        source=None,
        profiles_root=default_profiles_root(),
        chrome_binary=find_chrome(),
    )


def load_ambient(start: Path | None = None) -> Scope:
    """The scope governing the current directory: the discovered project, else `user`."""
    found = discover(start)
    return load_file(found) if found else load_user_scope()
