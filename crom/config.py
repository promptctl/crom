"""Discovers a project's crom config and turns any config file into a `Scope`.

This module is the border checkpoint for everything a human writes on disk
([LAW:parse-dont-validate]): a config file goes in as untyped TOML and comes out as a
`Scope` whose every field is already legal — names validated, seeds recognised, paths
made absolute, reserved Chrome switches rejected. Nothing downstream re-checks a key,
because downstream code only ever sees a `Scope`.

`repair_unreadable` is the one thing here that writes, and it writes in exactly one
situation: the file will not tokenize as TOML, so it holds nothing crom can act on and
no command — `crom init` and `crom rm` included — can run to repair it. Then, and only
then, it is reset to the default and the original is kept beside it. Every other way a
config can be wrong keeps its precise diagnostic, because crom can still read those
files and a reset would destroy the good declarations along with the bad line.
"""

import os
import tomllib
from pathlib import Path

from . import configwrite, flags, registry, report
from .browser import find_chrome
from .locking import exclusive
from .model import (
    DEFAULT_SEED,
    FALLBACK_NAMESPACE,
    MAX_PORT,
    MIN_PORT,
    USER_NAMESPACE,
    Conflict,
    CromError,
    Flag,
    NotFound,
    ProfileSpec,
    Scope,
    Seed,
    SeedChrome,
    SeedFresh,
    SeedPath,
    slug_for,
    validate_name,
)
from .paths import (
    PROJECT_CONFIG_CANDIDATES,
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


def parse_flags(raw, where: str, source: Path) -> tuple[Flag, ...]:
    """The border for everything a config — or `crom add --flag` — says about flags.

    Returns parsed flags rather than the strings that came in, so `flags.compose` and
    `configwrite` both work from the switch/value split instead of each re-deriving it.
    [LAW:one-source-of-truth] `Flag.parse` is the only thing here that knows a switch is
    the text before the first `=`.

    The reserved check runs *before* `flags.layer`'s duplicate rule, and the order is
    load-bearing: `flags = ["--user-data-dir=/a", "--user-data-dir=/b"]` is both a
    duplicate and a reserved switch, and the duplicate message would tell the user to
    write `--user-data-dir=/a,/b` — advice that fails again on the next load, because no
    occurrence of a reserved switch is allowed however it is spelled. A diagnostic whose
    remedy does not work is worse than none. [LAW:no-silent-failure]
    """
    if not isinstance(raw, list) or not all(isinstance(f, str) for f in raw):
        raise CromError(f"{source}: {where}.flags must be a list of strings")
    for flag in (Flag.parse(text) for text in raw):
        if flag.switch in RESERVED_SWITCHES:
            raise CromError(
                f"{source}: {where}.flags may not set {flag.switch} — crom owns it "
                f"(it defines the profile's data directory and CDP port)"
            )
    return flags.layer(raw, f"{source}: {where}.flags")


def parse_env(raw, where: str, source: Path) -> dict[str, str]:
    if not isinstance(raw, dict) or not all(isinstance(v, str) for v in raw.values()):
        raise CromError(f"{source}: {where}.env must be a table of string values")
    return dict(raw)


def parse_seed(raw, where: str, source: Path, config_dir: Path) -> Seed:
    """Turn a seed string into one of the three real sources.

    The vocabulary is closed: `fresh`, `default`, `chrome:<Profile Name>`, or a path
    (which must start with `.`, `/`, or `~` so a typo'd keyword can never be mistaken
    for a relative path that happens not to exist yet).

    `default` names the Chrome profile it copies. The keyword used to be `chrome`, which
    named the browser instead — and a browser has many profiles, so the value said
    nothing about which one it meant.
    """
    if not isinstance(raw, str):
        raise CromError(f"{source}: {where}.seed must be a string")
    if raw == "fresh":
        return SeedFresh()
    if raw == "default":
        return SeedChrome()
    if raw == "chrome":
        # A tombstone, not a spelling. Configs written before the rename say `chrome`,
        # and leaving it to fall through to "not recognised" would make the fix a guess.
        raise CromError(
            f"{source}: {where}.seed = 'chrome' — the seed names the Chrome profile now, "
            f'not the browser. Write seed = "default".'
        )
    if raw.startswith("chrome:"):
        return SeedChrome(profile=_parse_chrome_profile(raw.split(":", 1)[1], where, source))
    if raw[:1] in (".", "/", "~"):
        return SeedPath(_parse_seed_path(raw, where, source, config_dir))
    raise CromError(
        f"{source}: {where}.seed = {raw!r} is not recognised. Use 'fresh', 'default', "
        f"'chrome:<Profile Name>', or a path beginning with './', '/', or '~'."
    )


def _parse_seed_path(raw: str, where: str, source: Path, config_dir: Path) -> Path:
    """A directory to copy a profile from, checked before it becomes a `SeedPath`.

    `seed` is read from a `.crom.toml` that may have arrived with a cloned repository —
    the same untrusted-config threat model `_parse_chrome_profile` and `seed._link_guard`
    are built around — and `materialize` will `copytree` whatever this names. So
    `seed = "~"` or `seed = "/"` meant copying the user's entire home directory, or the
    filesystem, into a profile directory served by a locally reachable CDP port. The
    sibling vocabulary was guarded and this one, beside it, accepted anything.

    Refusing the home directory and the filesystem root refuses the whole class, because
    an ancestor of the profile's own home is what makes the copy unbounded. A legitimate
    seed is some specific profile directory, never the tree containing all of them.

    [LAW:parse-dont-validate] checked here, so `seed.materialize` holds no guard: a
    `SeedPath` that exists names a directory crom is willing to copy.
    """
    resolved = (config_dir / Path(raw).expanduser()).resolve()
    home = Path.home().resolve()
    forbidden = {resolved == home, resolved in home.parents, resolved == Path(resolved.anchor)}
    if any(forbidden):
        raise CromError(
            f"{source}: {where}.seed = {raw!r} resolves to {resolved}, which contains "
            f"your whole home directory or filesystem.\n"
            f"crom copies a seed directory in full, so this would duplicate everything "
            f"under it into a profile reachable over CDP. Name the specific profile "
            f"directory to copy instead."
        )
    return resolved


def _parse_chrome_profile(which: str, where: str, source: Path) -> str:
    """The name of one profile inside the user's Chrome directory, and nothing else.

    `seed.materialize` builds the copy source as `chrome_user_data_dir() / which`, and
    `Path.__truediv__` discards its left side when the right is absolute — so an
    unchecked `chrome:/etc` reads as `Path("/etc")`, and `chrome:../../..` walks out by
    ordinary resolution. Either way `shutil.copytree` would pull an arbitrary readable
    directory into a profile whose CDP port is reachable by local tooling, which is a
    file-exfiltration primitive handed to any `.crom.toml` — including one that arrived
    with a cloned repo.

    An empty name is refused for the same reason at a different scale: `Path('/a') / ''`
    is `Path('/a')`, so `chrome:` would silently copy the user's *entire* Chrome
    directory — every profile and every cookie — when they asked for one profile.

    [LAW:parse-dont-validate] The checkpoint is here, so `seed.py` holds no guard: a
    `SeedChrome` that exists names a single directory that cannot escape.
    """
    if not which:
        raise CromError(
            f"{source}: {where}.seed = 'chrome:' names no profile. Use 'chrome' for the "
            f"default profile, or 'chrome:<Profile Name>' for a specific one."
        )
    # Each clause tests the representation it is actually about. Mixing them — a
    # normalized component count against a raw-string comparison — leaves a gap between
    # the two: pathlib drops `.` and empty components, so `../` and `./..` both normalize
    # to `('..',)` while the raw value equals neither `.` nor `..`; and `Path('/').parts`
    # is `('/',)`, one component, so a bare `/` passed a length check and then collapsed
    # the join to the filesystem root.
    parts = Path(which).parts
    if "/" in which or which.startswith("~") or len(parts) != 1 or parts[0] in (".", ".."):
        raise CromError(
            f"{source}: {where}.seed = 'chrome:{which}' is not a profile name. It must "
            f"name one directory inside your Chrome user-data-dir (e.g. 'Default', "
            f"'Profile 1') — not a path."
        )
    return which


def parse_port(raw, where: str, source: Path) -> int | None:
    if raw is None:
        return None
    if not isinstance(raw, int) or isinstance(raw, bool) or not (MIN_PORT <= raw <= MAX_PORT):
        raise CromError(
            f"{source}: {where}.port must be an integer in {MIN_PORT}..{MAX_PORT}"
        )
    return raw


def _parse_chrome_binary(raw, source: Path, config_dir: Path) -> Path:
    """The Chrome to launch: the configured one, checked, or the one we can find.

    Resolved against the config file's directory exactly as `state_dir` and seed paths
    are. A merely `expanduser()`'d relative path would be read against whatever the
    working directory happened to be at launch, which breaks the one thing a namespace
    promises — that `crom up myapp/dev` means the same thing from anywhere on the machine.

    Checked for existence and executability here rather than discovered at `Popen`:
    `browser.py` promises crom names the paths it tried "rather than failing later inside
    Popen with a bare ENOENT", and that promise held only for the auto-detected path. A
    typo'd `chrome_binary` used to surface as a raw `FileNotFoundError` traceback,
    outside the CLI's exit-code contract entirely.
    """
    if raw is None:
        return find_chrome()
    if not isinstance(raw, str):
        raise CromError(f"{source}: chrome_binary must be a string path")
    if raw == "":
        # `Path("")` is `Path(".")`, so the empty string resolves to the config's own
        # directory — which exists. The `is_file()` check below still refuses it, but the
        # message it produces says a directory that is plainly there "does not exist". A
        # diagnostic that is false about the thing it names sends the reader to check the
        # wrong fact; `state_dir` refuses the empty string by name for the same reason.
        raise CromError(
            f"{source}: chrome_binary is empty. Give a path to the Chrome executable, "
            f"or remove the key to let crom find one."
        )

    binary = (config_dir / Path(raw).expanduser()).resolve()
    if not binary.is_file():
        raise CromError(
            f"{source}: chrome_binary {raw!r} does not exist (resolved to {binary})."
        )
    if not os.access(binary, os.X_OK):
        raise CromError(
            f"{source}: chrome_binary {raw!r} is not executable (resolved to {binary})."
        )
    return binary


def parse(text: str, source: Path, *, namespace: str | None = None) -> Scope:
    """Parse config text into a Scope. `namespace` forces one (the `user` scope)."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise CromError(f"{source}: invalid TOML: {e}") from e

    _reject_unknown(data, _SCOPE_KEYS, "the top level", source)

    if namespace is None:
        declared = data.get("namespace")
        # Two different problems, so two different messages. Telling someone who wrote
        # `namespace = 123` that their key is missing is the least useful thing crom
        # could say to the person actually looking at the line.
        if "namespace" not in data:
            raise CromError(
                f'{source}: missing required key `namespace`. Add e.g. namespace = "myapp" — '
                f"it is how this project's profiles and ports stay clear of every other project's."
            )
        if not isinstance(declared, str):
            raise CromError(
                f"{source}: `namespace` must be a string, not "
                f"{type(declared).__name__} ({declared!r})."
            )
        namespace = validate_name("namespace", declared)
        if namespace == USER_NAMESPACE:
            # `Conflict` (exit 4), matching `crom init` and `registry.forget_namespace`,
            # which refuse the same reserved name. A script branching on exit 4 to detect
            # "that name is taken" must see it from every path that decides it.
            raise Conflict(
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
    if state_dir == "":
        # Refused rather than interpreted. A truthy test silently treated this as absent
        # and fell back to the default, so someone debugging why their explicit
        # `state_dir` "was ignored" had nothing to go on. `is not None` would be worse:
        # it resolves to the config's own directory, quietly scattering profile
        # directories next to the config file. An empty path is not a location under any
        # reading, so the parser should not admit one — the same stance the seed
        # vocabulary already takes toward "".
        raise CromError(
            f"{source}: state_dir is empty. Give a path, or remove the key to use "
            f"{default_profiles_root()}."
        )
    profiles_root = (
        (config_dir / Path(state_dir).expanduser()).resolve()
        if state_dir is not None
        else default_profiles_root()
    )

    binary = _parse_chrome_binary(data.get("chrome_binary"), source, config_dir)

    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        raise CromError(f"{source}: [defaults] must be a table")
    _reject_unknown(defaults, _DEFAULTS_KEYS, "[defaults]", source)

    default_seed = (
        parse_seed(defaults["seed"], "[defaults]", source, config_dir)
        if "seed" in defaults
        else DEFAULT_SEED
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


# --- loading, and the one repair that writes -----------------------------------------


def load_file(source: Path, *, namespace: str | None = None) -> Scope:
    if not source.is_file():
        raise NotFound(f"config file not found: {source}")
    return parse(source.read_text(), source, namespace=namespace)


def repair_unreadable(source: Path, *, namespace: str | None = None, log=report.to_stderr) -> None:
    """Reset `source` to crom's default if it cannot be read as TOML at all.

    The trigger is deliberately narrow, and the narrowness is the whole design. A file
    that will not tokenize holds nothing crom can act on and locks out every command in
    the project — `crom init` and `crom rm` included — so there is no command crom could
    name as the repair, which is what makes resetting it the only useful answer.

    Every *other* way a config can be wrong is excluded, because in all of them crom can
    still read the file: two profiles pinning one port, an unknown key, a typo'd
    `chrome_binary`, an unrecognised seed. Those already produce a message naming the
    file and the exact key, and resetting over one of them would destroy four good
    declarations to punish one bad line. [LAW:no-silent-failure] a precise diagnostic is
    information; replacing it with a reset throws that information away and takes the
    user's other work with it.

    Reading the file is also all this asks of the machine — no Chrome lookup, no seed
    vocabulary, no port ledger — so it can run before every command without making
    `find_chrome()` a precondition of `crom init`.

    Under the lock `configwrite` writes beneath, re-reading inside it, so two crom
    processes meeting one broken file produce one reset and one no-op rather than two —
    the second of which would set aside the first's freshly written default and file the
    user's real config one name further along.
    """
    # Read once outside the lock, and again inside it. Not a redundant check: this runs
    # before every command, and `exclusive` creates a `.crom.toml.lock` beside the config
    # — in the user's repository — the moment it is called. Taking the lock only when
    # there is something to repair keeps crom from dropping a lock file into every
    # project on every invocation. The in-lock read is the authoritative one.
    if not source.is_file() or _reads_as_toml(source) is None:
        return

    with exclusive(source):
        unreadable = _reads_as_toml(source)
        if unreadable is None:
            return

        chosen = None if namespace == USER_NAMESPACE else _namespace_for(source)
        kept = configwrite.reset(
            source,
            configwrite.default_text(namespace=chosen, seed=DEFAULT_SEED, base=source.parent),
        )
        # The namespace and the seed are named, not just the reset. Both are guesses this
        # function had to make because the file that would have answered them is the file
        # that could not be read: the namespace decides which ports and profile
        # directories the project keeps, and the seed decides whether the next `crom up`
        # copies the user's real Chrome profile or starts empty. A project that had
        # chosen `seed = "fresh"` gets `default` back, and saying so is the difference
        # between a repair and a surprise. [LAW:no-silent-failure]
        wrote = f"namespace '{chosen}', " if chosen else ""
        seed_name = configwrite.render_seed(DEFAULT_SEED, source.parent)
        log(
            f"{source} could not be read as TOML: {unreadable}\n"
            f"crom reset it to the default ({wrote}seed '{seed_name}'); your original is "
            f"kept at {kept}. Copy anything you need back out of it."
        )


def _reads_as_toml(source: Path) -> str | None:
    """Why `source` cannot be tokenized as TOML, or None when it can.

    `UnicodeDecodeError` sits beside the decode error rather than escaping: it is a
    `ValueError`, so a config saved as UTF-16 used to leave `CromGroup.invoke` — which
    catches only `CromError` — as a traceback. Both mean the same thing to every caller,
    which is that there are no bytes here crom can read. [LAW:parse-dont-validate]
    """
    try:
        tomllib.loads(source.read_text())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        return str(e)
    return None


def _namespace_for(source: Path) -> str:
    """The namespace a reset project config should declare, when the file cannot say.

    The registry is asked first because it is crom's own record of which file owns which
    namespace, and it survives the file becoming unreadable. Keeping the old name keeps
    the project's port reservations and profile directories, which are keyed on it — a
    reset that renamed the namespace would hand the project a fresh set of both and
    orphan everything already pointing at the old ports.

    The registry can only answer for a project crom has loaded successfully at least
    once, because `_Session.scope` records the name *after* the load. A `.crom.toml` that
    arrived broken — a fresh clone, a hand-written file — therefore falls back to the
    directory name, which is the same rule `crom init` applies. That is a real rename, and
    it is why `repair_unreadable` reports the name it chose rather than assuming the
    caller can predict it. [LAW:one-source-of-truth]
    """
    remembered = next((name for name, path in registry.namespaces().items() if path == source), None)
    candidate = remembered or slug_for(source.parent.name)
    # `parse` refuses `user` in a project file, so a directory literally named `user`
    # would otherwise be reset to a config crom rejects on the very next command.
    return FALLBACK_NAMESPACE if candidate == USER_NAMESPACE else candidate


def load_user_scope() -> Scope:
    """The `user` namespace — your personal profiles, from a fixed path.

    A machine with no user config still has a `user` scope; it simply declares no
    profiles yet. [LAW:no-defensive-null-guards] callers never ask whether the file
    exists, because a Scope is always what they get.
    """
    source = user_config_file()
    repair_unreadable(source, namespace=USER_NAMESPACE)
    if source.is_file():
        return load_file(source, namespace=USER_NAMESPACE)
    return Scope(
        namespace=USER_NAMESPACE,
        source=None,
        profiles_root=default_profiles_root(),
        chrome_binary=find_chrome(),
    )


def load_ambient(start: Path | None = None) -> Scope:
    """The scope governing the current directory: the discovered project, else `user`.

    The repair is attached to the two loads that answer "which config governs *me*" —
    here and in `load_user_scope` — rather than to `load_file`, which also serves
    `resolve.scope_for` reaching a *foreign* project's config through the registry.
    Resetting from there meant one `crom list --all` could rewrite every registered
    project's `.crom.toml` on the machine, dropping declarations belonging to work the
    user was not even doing. A config is repaired by the project standing in it.
    [LAW:decomposition] the joint is ownership, not the act of reading a file.
    """
    found = discover(start)
    if found is None:
        return load_user_scope()
    repair_unreadable(found)
    return load_file(found)
