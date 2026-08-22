"""Turns a profile reference into a fully-resolved launch spec.

This is where the three inputs meet — the scope that declares the profile, the ledger
that owns its port, and crom's launch policy — and where they stop being separate
concerns. Everything after this point takes a `ResolvedProfile` and needs to know
nothing about config files, discovery, or ports.
"""

import re
from pathlib import Path

from . import registry
from .config import load_file, load_user_scope
from .model import (
    CromError,
    NotFound,
    ProfileRef,
    ProfileSpec,
    ResolvedProfile,
    Scope,
    Seed,
)
from .paths import USER_NAMESPACE
from .policy import LAUNCH_POLICY_FLAGS

# The closed vocabulary a config may interpolate into flags and env values. Closed on
# purpose: an unknown ${...} is an error, never an empty string silently spliced into a
# Chrome switch. [LAW:no-silent-failure]
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(text: str, variables: dict[str, str], where: str) -> str:
    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in variables:
            known = ", ".join(f"${{{k}}}" for k in sorted(variables))
            raise CromError(f"{where}: unknown variable ${{{key}}} (known: {known})")
        return variables[key]

    return _VAR_RE.sub(replace, text)


def build_argv(
    chrome_binary: Path,
    profile_dir: Path,
    port: int,
    flags: tuple[str, ...],
) -> tuple[str, ...]:
    """Compose the complete Chrome command line.

    crom's own switches go last so no configured flag can displace them: they carry the
    profile's identity (`--user-data-dir`, which is also how `chrome.find_pids` knows
    what is running) and its CDP contract. `config.RESERVED_SWITCHES` already rejects a
    config that tries; ordering means even a bug there cannot corrupt crom's map.
    """
    return (
        str(chrome_binary),
        *LAUNCH_POLICY_FLAGS,
        *flags,
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={port}",
    )


def scope_for(namespace: str, ambient: Scope) -> Scope:
    """Load the scope that declares `namespace`, from anywhere on the machine."""
    if namespace == ambient.namespace:
        return ambient
    if namespace == USER_NAMESPACE:
        return load_user_scope()

    known = registry.namespaces()
    source = known.get(namespace)
    if source is None:
        options = ", ".join(sorted({USER_NAMESPACE, ambient.namespace, *known}))
        raise NotFound(f"unknown namespace '{namespace}'. Known namespaces: {options}")
    if not source.is_file():
        raise NotFound(
            f"namespace '{namespace}' was declared in {source}, which no longer exists. "
            f"Run `crom forget {namespace}` to drop it."
        )
    return load_file(source)


def spec_for(ref: ProfileRef, scope: Scope) -> ProfileSpec:
    spec = scope.profiles.get(ref.name)
    if spec is None:
        declared = ", ".join(sorted(scope.profiles)) or "(none)"
        where = scope.source or "your user config"
        raise NotFound(
            f"profile '{ref}' is not declared in {where}. Declared there: {declared}"
        )
    return spec


def resolve(ref: ProfileRef, ambient: Scope) -> ResolvedProfile:
    scope = scope_for(ref.namespace, ambient)
    spec = spec_for(ref, scope)
    return resolve_spec(ref, scope, spec)


def resolve_spec(ref: ProfileRef, scope: Scope, spec: ProfileSpec) -> ResolvedProfile:
    profile_dir = scope.profiles_root / ref.namespace / ref.name
    port = registry.port_for(ref, pinned=spec.port, source=scope.source)

    variables = {
        "CROM_NAMESPACE": ref.namespace,
        "CROM_PROFILE": ref.name,
        "CROM_PORT": str(port),
        "CROM_PROFILE_DIR": str(profile_dir),
        "CROM_CONFIG_DIR": str(scope.config_dir),
    }
    where = str(scope.source or "user config")
    flags = tuple(_expand(f, variables, where) for f in (*scope.default_flags, *spec.flags))
    env = {k: _expand(v, variables, where) for k, v in {**scope.default_env, **spec.env}.items()}

    seed: Seed = spec.seed if spec.seed is not None else scope.default_seed

    return ResolvedProfile(
        ref=ref,
        port=port,
        profile_dir=profile_dir,
        chrome_binary=scope.chrome_binary,
        argv=build_argv(scope.chrome_binary, profile_dir, port, flags),
        env=env,
        seed=seed,
        source=scope.source,
    )


def resolve_all(scope: Scope) -> list[ResolvedProfile]:
    """Every profile a scope declares, resolved — the list `crom list` reports."""
    return [
        resolve_spec(ProfileRef(scope.namespace, name), scope, spec)
        for name, spec in sorted(scope.profiles.items())
    ]
