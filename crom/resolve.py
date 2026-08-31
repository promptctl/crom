"""Turns a profile reference into a fully-resolved launch spec.

This is where the three inputs meet — the scope that declares the profile, the ledger
that owns its port, and crom's launch policy — and where they stop being separate
concerns. Everything after this point takes a `ResolvedProfile` and needs to know
nothing about config files, discovery, or ports.
"""

import re
from pathlib import Path

from . import configwrite, flags, registry, report
from .config import load_file, load_user_scope
from .model import (
    DEFAULTS_STANZA,
    USER_NAMESPACE,
    Composed,
    CromError,
    Emitted,
    FailedProfile,
    Flag,
    Layer,
    NotFound,
    ProfileEntry,
    ProfileRef,
    ProfileSpec,
    ResolvedProfile,
    Scope,
    Seed,
    profile_stanza,
)
from .paths import user_config_file
from .policy import LAUNCH_POLICY_FEATURES, LAUNCH_POLICY_FLAGS, LAUNCH_POLICY_ORIGIN

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
    launch_flags: tuple[str, ...],
) -> tuple[str, ...]:
    """Compose the complete Chrome command line from already-resolved flags.

    `launch_flags` arrives composed — crom's launch policy is a layer `flags.compose`
    resolved, not a prefix this function prepends, so a profile that names a policy switch
    replaces crom's entry for it instead of trailing behind it. [LAW:one-source-of-truth]
    the launch list is decided in one place, and this function only frames it.

    crom's own switches still go last, and they are not layer input: they carry the
    profile's identity (`--user-data-dir`, which is also how `chrome.find_pids` knows
    what is running) and its CDP contract. `config.RESERVED_SWITCHES` already rejects a
    config that tries; ordering means even a bug there cannot corrupt crom's map.
    """
    return (
        str(chrome_binary),
        *launch_flags,
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={port}",
    )


def scope_for(namespace: str, ambient: Scope, log=report.to_stderr) -> Scope:
    """Load the scope that declares `namespace`, from anywhere on the machine.

    A registry entry pointing at a config that is gone, or at one that has since renamed
    its own namespace, is crom's memory outliving the thing it remembered. Both used to
    end in "run `crom forget <namespace>`" — a chore handed to the user for a mess crom
    made, and the only possible response to it. So the forget happens here and the
    namespace is simply reported unknown, which is what it now is.

    `forget_mapping`, not `forget_namespace`: only crom's record of *where* the project
    lives is dropped, never the ports reserved under it. An absent file is not proof the
    project is gone — an unmounted volume looks the same — and released ports are
    irreversible. See `registry.forget_mapping`.
    """
    if namespace == ambient.namespace:
        return ambient
    if namespace == USER_NAMESPACE:
        return load_user_scope()

    match _remembered(namespace):
        case Scope() as scope:
            return scope
        case str() as stale:
            registry.forget_mapping(namespace)
            log(
                f"Forgot where namespace '{namespace}' lives: {stale}. Its reserved ports "
                f"are kept — `crom forget {namespace}` releases them if the project is gone."
            )

    options = ", ".join(sorted({USER_NAMESPACE, ambient.namespace, *registry.namespaces()}))
    raise NotFound(f"unknown namespace '{namespace}'. Known namespaces: {options}")


def _remembered(namespace: str) -> Scope | str | None:
    """What the registry knows about `namespace`: the scope that owns it, a sentence
    saying why its entry no longer holds, or None if crom has never heard the name.

    Three outcomes, three types. A `(Scope | None, str | None)` pair has four states for
    three answers, so "both set" and "neither set but stale" would be expressible and
    every caller would have to know by convention that they never happen.
    [LAW:types-are-the-program]

    `load_file`, deliberately not `load_ambient`'s repairing path: this reaches a config
    belonging to a project the user is not standing in, and rewriting it from here meant
    a single `crom list --all` could reset every registered project's `.crom.toml` on the
    machine. A foreign config that will not parse is reported, not repaired.
    """
    source = registry.namespaces().get(namespace)
    if source is None:
        return None
    if not source.is_file():
        return f"{source} no longer exists"

    scope = load_file(source)
    if scope.namespace != namespace:
        # `remember_namespace` is additive: it refuses a *different* file claiming a name
        # someone else owns, but nothing removes the old entry when a config renames its
        # own namespace in place. Without this the ledger's stale mapping would load
        # happily and `resolve_spec` would build a profile directory and port under the
        # name that was asked for — a plausible-looking profile belonging to nothing.
        # [LAW:no-silent-failure]
        return f"{source} now declares '{scope.namespace}'"
    return scope


def spec_for(ref: ProfileRef, scope: Scope) -> ProfileSpec:
    """The declaration `ref` names, for callers that must not create one.

    `down` and `rm` converge a profile toward *not* running and *not* existing, so
    declaring one on their behalf would be crom creating the thing it was asked to take
    away. Every other caller goes through `resolve_or_declare`, which is why this message
    no longer suggests `crom add`: the commands that reach it are the ones for which
    adding the profile is not the repair.
    """
    spec = scope.profiles.get(ref.name)
    if spec is None:
        declared = ", ".join(sorted(scope.profiles)) or "(none)"
        where = scope.source or "your user config"
        raise NotFound(
            f"profile '{ref}' is not declared in {where}.\nDeclared there: {declared}"
        )
    return spec


def resolve(ref: ProfileRef, ambient: Scope, log=report.to_stderr) -> ResolvedProfile:
    """Resolve a profile that must already be declared."""
    scope = scope_for(ref.namespace, ambient, log)
    return resolve_spec(scope, spec_for(ref, scope))


def resolve_or_declare(ref: ProfileRef, ambient: Scope, log=report.to_stderr) -> ResolvedProfile:
    """Resolve a profile, declaring it first if nothing declares it yet.

    What `crom up`, `crom port`, `crom env`, `crom mcp` and `crom config <ref>` use:
    each of them is being asked *where profile X is*, and a profile crom has not been
    told about yet is not a different question, it is the same question one `crom add`
    earlier. Naming that command back at the user was crom making them the courier for a
    step it can take itself, and the case it fires in most is a bare `crom up` in a
    project whose namespace declares no `default` — the profile every one of these
    commands defaults to, and therefore the one crom's own contract promises resolves.

    `down` and `rm` deliberately do not come through here; see `spec_for`.
    """
    scope = scope_for(ref.namespace, ambient, log)
    spec = scope.profiles.get(ref.name)
    return resolve_spec(scope, spec if spec is not None else _declare(ref, scope, log))


def _declare(ref: ProfileRef, scope: Scope, log) -> ProfileSpec:
    """Write the declaration `crom add <name>` would have written, and say so.

    The spec is bare on purpose — no seed, no flags, no pinned port — because that is
    exactly what `crom add <name>` with no options writes, and an absent `seed` key is
    how the format spells "inherit `[defaults]`". Filling any of it in would give a
    profile crom created on the user's behalf properties the user never chose, and freeze
    the project's `[defaults]` out of it.

    `write_default` first, because the config crom discovered can be gone by the time we
    write to it — a `git clean`, another agent resetting the workspace — and
    `configwrite._declare` will not create a project config out of a header that carries
    no `namespace` key. Recreating it from the same template `crom init` uses is the only
    write that leaves a file crom can read back.
    """
    target = scope.source or user_config_file()
    configwrite.write_default(
        target,
        namespace=None if scope.is_user else scope.namespace,
        seed=scope.default_seed,
    )
    spec = ProfileSpec(name=ref.name)
    configwrite.ensure_profile(
        target, spec, header=configwrite.USER_CONFIG_HEADER if scope.is_user else ""
    )
    log(f"Declared {ref} in {target} — it was not declared yet.")
    return spec


def resolve_spec(scope: Scope, spec: ProfileSpec) -> ResolvedProfile:
    """Resolve one declared profile in the namespace that declares it.

    [LAW:types-are-the-program] Both halves of the identity come from arguments that
    already carry them, and neither is passed a second time.

    The namespace comes from `scope`. Accepting a `ProfileRef` *and* a `Scope` made a
    mismatch expressible — the profile directory is built from the ref's namespace while
    the ledger is keyed on the ref, so a caller pairing a ref with a foreign scope would
    silently create a directory and a reservation under a namespace that scope does not
    own, with nothing raised.

    The name comes from `spec`. Taking it separately left the same defect one step over:
    `configwrite._declare` and `config.reject_duplicate_ports` key off `spec.name` — the
    latter iterates `.values()` and has no other identity available — so a caller passing
    a name that disagreed with `spec.name` would resolve as one profile and be declared
    as another. Every call site kept them in sync by convention, which is exactly the
    guarantee a type should be making instead.
    """
    ref = ProfileRef(scope.namespace, spec.name)
    profile_dir = scope.profiles_root / ref.namespace / ref.name
    where = str(scope.source or "user config")
    # The one place the launch list is decided. Composition is by switch name, so a
    # profile's `--disable-blink-features` replaces `[defaults]`'s and `[defaults]`'s
    # replaces the policy's — the `profile > defaults > policy` rule every other key here
    # follows. (`--disable-features` cannot stand as the example any more: `features` owns
    # that switch, and a layer contributes to it rather than replacing it.)
    #
    # `crom add`'s restatement check goes through `flags.compose` too, but over two layers
    # rather than three: it asks what this *config* states, and crom's launch policy is
    # not in the config. Deliberately different layer sets, not a drift — see
    # `cli._effective_flags`, which owns that reasoning.
    #
    # Only the two config stanzas arrive as layers with drops: crom's policy is flags and
    # nothing else, and a `Layer` around it says so. A profile dropping a policy switch is
    # the point — that is how a project launches with sync on without editing crom's own
    # list — while the policy dropping something beneath it would have nothing to drop.
    #
    # The feature switches are folded beside the composition rather than through it, and
    # appended last. `config.RESERVED_SWITCHES` refuses `--enable-features` and
    # `--disable-features` inside any `flags` list and inside any `drop_flags` list, so no
    # layer can set or remove one and `compose` never had a conflict to resolve about them
    # — while attributing them *would* have been a conflict it could only get wrong, since
    # one `--disable-features` carries names from every layer at once. See `flags.features`.
    #
    # The three feature tables are named with the same strings their stanzas' `flags` are
    # named with, because they are the same stanzas. Read from the shared constants rather
    # than from `default_flags.origin` and `spec.flags.origin`, which do carry the right
    # strings: a stanza's name belongs to the stanza, not to whichever of its keys happened
    # to be parsed, and taking the `features` label out of the `flags` layer would make one
    # half of a stanza depend on a sibling half for no reason but that it is holding the
    # string. [LAW:one-source-of-truth] the name has one home, and both keys read it there.
    composed = flags.compose(
        Layer(LAUNCH_POLICY_FLAGS, origin=LAUNCH_POLICY_ORIGIN),
        scope.default_flags,
        spec.flags,
    )
    emitted = (
        *composed.emitted,
        *flags.features(
            (LAUNCH_POLICY_ORIGIN, LAUNCH_POLICY_FEATURES),
            (DEFAULTS_STANZA, scope.default_features),
            (profile_stanza(spec.name), spec.features),
        ),
    )
    raw_env = {**scope.default_env, **spec.env}

    # [LAW:effects-at-boundaries] Every way this resolution can fail runs first, while
    # it is still pure. `port_for` writes a reservation into the machine-wide ledger the
    # moment it is called, so a failure after it would leave that reservation behind
    # with no profile and no way for the user to reclaim the port.
    #
    # Every layer's text is checked, not just the composed result: a `${CROM_PROFIL_DIR}`
    # typo in a `[defaults]` flag that this profile happens to override is still a typo,
    # and reporting it only for the profiles that don't override it would make the
    # diagnostic depend on which stanza you were resolving. [LAW:no-silent-failure]
    #
    # Drops are absent for the same reason features are, and not the one first written
    # here: it is not that a switch name is the half a variable never lives in — this check
    # covers the switch half of a `flags` entry, since `render` yields the whole string. It
    # is that `flags.drops` refuses a `${` in a drop outright, so a drop carries no variable
    # to be unknown, and a typo that would silently match nothing is caught where it was
    # written instead. [LAW:parse-dont-validate]
    #
    # Features are absent from this check because they cannot fail it: `parse_features`
    # refuses a `${` in a feature name outright, so a feature carries no variable to be
    # unknown. That refusal is what keeps `flags.features` folding on the same text Chrome
    # is given, rather than on a pre-expansion spelling of it.
    _reject_unknown_variables(
        (
            *flags.render(scope.default_flags.sets),
            *flags.render(spec.flags.sets),
            *raw_env.values(),
        ),
        _variables(ref, profile_dir, scope.config_dir, None),
        where,
    )

    port = registry.port_for(ref, pinned=spec.port, source=scope.source)

    variables = _variables(ref, profile_dir, scope.config_dir, port)
    # Expansion lands in the report, and `argv` is then built from the report — one list,
    # not two that must stay parallel. That is what lets `crom config` match an annotation
    # to a command line by the text itself: the strings are the same strings.
    # [LAW:one-source-of-truth] The *answers* stay unexpanded on purpose — a user hunting
    # for the flag they wrote is hunting for the text they typed, not for what crom made
    # of it.
    provenance = Composed(
        tuple(
            Emitted(Flag.parse(_expand(str(item.flag), variables, where)), item.why)
            for item in emitted
        ),
        composed.dropped,
    )
    env = {k: _expand(v, variables, where) for k, v in raw_env.items()}

    seed: Seed = spec.seed if spec.seed is not None else scope.default_seed

    return ResolvedProfile(
        ref=ref,
        port=port,
        profile_dir=profile_dir,
        chrome_binary=scope.chrome_binary,
        argv=build_argv(scope.chrome_binary, profile_dir, port, flags.render(provenance.flags)),
        env=env,
        seed=seed,
        source=scope.source,
        provenance=provenance,
    )


def resolve_all(scope: Scope) -> list[ProfileEntry]:
    """Every profile a scope declares, resolved or explained — what `crom list` reports.

    Failures are isolated per profile and returned as `FailedProfile`. Letting one bad
    declaration propagate would abort the whole listing, and `crom list` is exactly what
    a user runs to find out which declaration is bad — the command would fail hardest in
    the situation it exists for.
    """
    entries: list[ProfileEntry] = []
    for _, spec in sorted(scope.profiles.items()):
        try:
            entries.append(resolve_spec(scope, spec))
        except CromError as error:
            entries.append(FailedProfile(ProfileRef(scope.namespace, spec.name), str(error)))
    return entries
