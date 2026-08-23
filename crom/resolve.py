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
    FailedProfile,
    NotFound,
    ProfileEntry,
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


def _variables(ref: ProfileRef, profile_dir: Path, config_dir: Path, port: int | None) -> dict[str, str]:
    """The closed vocabulary a config may interpolate, in one place.

    `port` is None while we are still checking *which* names a config refers to — the
    set of legal names is the same either way, and deriving both the check and the
    expansion from this one function keeps them from drifting into two vocabularies.
    [LAW:one-source-of-truth]
    """
    return {
        "CROM_NAMESPACE": ref.namespace,
        "CROM_PROFILE": ref.name,
        "CROM_PORT": "" if port is None else str(port),
        "CROM_PROFILE_DIR": str(profile_dir),
        "CROM_CONFIG_DIR": str(config_dir),
    }


def _reject_unknown_variables(texts: tuple[str, ...], known: dict[str, str], where: str) -> None:
    """Raise for any `${VAR}` outside the vocabulary, expanding nothing.

    Exists so the one way resolution can fail is reachable *before* a port is reserved.
    A `${CROM_PROFIL_DIR}` typo used to raise after `port_for` had already written to the
    machine-wide ledger, stranding a reservation for a profile that never resolved —
    which then blocked an unrelated profile from claiming that port, with no command
    able to release it.
    """
    for text in texts:
        for match in _VAR_RE.finditer(text):
            if match.group(1) not in known:
                names = ", ".join(f"${{{k}}}" for k in sorted(known))
                raise CromError(f"{where}: unknown variable ${{{match.group(1)}}} (known: {names})")


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

    scope = load_file(source)
    if scope.namespace != namespace:
        # `remember_namespace` is additive: it refuses a *different* file claiming a name
        # someone else owns, but nothing removes the old entry when a config renames its
        # own namespace in place. Without this the ledger's stale mapping would load
        # happily and `resolve_spec` would build a profile directory and port under the
        # name that was asked for — a plausible-looking profile belonging to nothing.
        # [LAW:no-silent-failure]
        raise NotFound(
            f"namespace '{namespace}' is stale: {source} now declares "
            f"'{scope.namespace}'. Run `crom forget {namespace}` to drop the old name."
        )
    return scope


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
    return resolve_spec(scope, ref.name, spec)


def resolve_spec(scope: Scope, name: str, spec: ProfileSpec) -> ResolvedProfile:
    """Resolve one declared profile in the namespace that declares it.

    [LAW:types-are-the-program] The namespace is taken from `scope` and never passed
    alongside it. Accepting a `ProfileRef` *and* a `Scope` made a mismatch expressible —
    the profile directory is built from the ref's namespace while the ledger is keyed on
    the ref, so a caller pairing a ref with a foreign scope would silently create a
    directory and a port reservation under a namespace that scope does not own, with
    nothing raised. Deriving the ref here removes the second namespace rather than
    adding a guard against it: there is no longer a pair that can disagree.
    """
    ref = ProfileRef(scope.namespace, name)
    profile_dir = scope.profiles_root / ref.namespace / ref.name
    where = str(scope.source or "user config")
    raw_flags = (*scope.default_flags, *spec.flags)
    raw_env = {**scope.default_env, **spec.env}

    # [LAW:effects-at-boundaries] Every way this resolution can fail runs first, while
    # it is still pure. `port_for` writes a reservation into the machine-wide ledger the
    # moment it is called, so a failure after it would leave that reservation behind
    # with no profile and no way for the user to reclaim the port.
    _reject_unknown_variables(
        (*raw_flags, *raw_env.values()),
        _variables(ref, profile_dir, scope.config_dir, None),
        where,
    )

    port = registry.port_for(ref, pinned=spec.port, source=scope.source)

    variables = _variables(ref, profile_dir, scope.config_dir, port)
    flags = tuple(_expand(f, variables, where) for f in raw_flags)
    env = {k: _expand(v, variables, where) for k, v in raw_env.items()}

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


def resolve_all(scope: Scope) -> list[ProfileEntry]:
    """Every profile a scope declares, resolved or explained — what `crom list` reports.

    Failures are isolated per profile and returned as `FailedProfile`. Letting one bad
    declaration propagate would abort the whole listing, and `crom list` is exactly what
    a user runs to find out which declaration is bad — the command would fail hardest in
    the situation it exists for.
    """
    entries: list[ProfileEntry] = []
    for name, spec in sorted(scope.profiles.items()):
        try:
            entries.append(resolve_spec(scope, name, spec))
        except CromError as error:
            entries.append(FailedProfile(ProfileRef(scope.namespace, name), str(error)))
    return entries
