"""The types crom's pipeline speaks, from a user-typed reference to a launchable spec.

The pipeline is a straight line with one shape per stage, and each stage's output type
is the proof that the stage ran:

    "myapp/dev"  --parse-->  ProfileRef
    ProfileRef + Scope + port  --resolve-->  ResolvedProfile  --launch-->  a process

[LAW:parse-dont-validate] Nothing downstream re-checks a name, re-reads a config file,
or re-derives a port: a `ResolvedProfile` could not have been constructed without all
of that already being true, so `chrome.launch` has nothing left to look up.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path


# Namespaces and profile names become both directory components and CLI tokens, so the
# legal set is the intersection of what is safe in each. [LAW:parse-dont-validate] this
# is checked once, where names enter, and never again.
#
# `\Z`, not `$`: Python's `$` also matches immediately before a trailing newline, so `$`
# would accept "dev\n" — a directory component carrying a newline, which then splits the
# process's line in two when `chrome.scan` reads `ps` output. `\Z` is the true end.
#
# The namespace every machine has without declaring one. A domain fact rather than a
# path fact: `paths.py` composes directories from it, and `model` needs it to answer
# whether a ref belongs to the implicit scope. Owning it here makes the dependency
# one-way — `paths` imports `model` for `CromError`, and nothing imports back.
# [LAW:one-way-deps]
USER_NAMESPACE = "user"

# The length cap is named rather than spelled into the pattern, because `cli._slug`
# truncates to it — two hand-matched numbers would drift the first time either moved.
NAME_LIMIT = 64
_NAME_RE = re.compile(rf"^[a-z0-9][a-z0-9._-]{{0,{NAME_LIMIT - 1}}}\Z")


class CromError(Exception):
    """A failure with a message meant for the user, raised anywhere below the CLI."""


class NotFound(CromError):
    """A referenced profile, namespace, or config file does not exist."""


class Conflict(CromError):
    """Two declarations claim the same resource — usually a port."""


def validate_name(kind: str, value: str) -> str:
    if not _NAME_RE.match(value):
        raise CromError(
            f"invalid {kind} {value!r}: must match {_NAME_RE.pattern} "
            f"(lowercase letters, digits, and . _ - ; starting alphanumeric)"
        )
    return value


# The legal range for a TCP port, stated once. [LAW:one-source-of-truth] `config.parse_port`
# rejects a configured port outside it, `registry._read` rejects a stored one, and
# `registry._allocate` stops searching at the top — three enforcers of one rule, which
# only stay in agreement if they read the same bound rather than each spelling it out.
MIN_PORT = 1
MAX_PORT = 65535


# --- seeds -------------------------------------------------------------------------
# Where a profile's user-data-dir comes from the first time it is created. Three
# variants, because there are exactly three sources: nothing, your real Chrome, or a
# directory on disk. [LAW:types-are-the-program] a `seed: str` would admit "chorme"
# and defer the failure to copy time.


@dataclass(frozen=True)
class SeedFresh:
    """An empty directory; Chrome initializes it on first launch."""


@dataclass(frozen=True)
class SeedChrome:
    """A copy of one of the user's real Chrome profile directories."""

    profile: str = "Default"


@dataclass(frozen=True)
class SeedPath:
    """A copy of a directory on disk — a checked-in fixture, or another profile."""

    path: Path


Seed = SeedFresh | SeedChrome | SeedPath


# Where a profile's data comes from when nothing says otherwise.
#
# [LAW:one-source-of-truth] This question used to be answered in five places, and two of
# the answers disagreed: `cli._bootstrap_user_config` seeded `user/default` from the real
# browser while `configwrite.PROJECT_CONFIG_TEMPLATE` wrote `fresh`. So the word `default`
# meant "your Chrome, with your logins" outside a project and "an empty browser" inside
# one, with nothing in any output marking the difference — someone who ran `crom init` and
# then `crom up` got a browser they could not use and no way to see why it differed from
# the one they had yesterday. Both templates now render *this*, so they cannot drift again.
#
# `chrome` is the answer because a profile with no cookies and no extensions cannot do the
# job crom exists for: driving a real session. It costs one copy of one Chrome profile
# directory at create time — `crom init --seed fresh` and `crom add --seed fresh` decline
# it, and `crom up` names the seed on stderr before it starts copying.
DEFAULT_SEED: Seed = SeedChrome()


# --- declarations ------------------------------------------------------------------


@dataclass(frozen=True)
class ProfileRef:
    """A profile's global identity: which namespace, and which profile within it.

    Both fields are validated here, so the module docstring's "checked once, where they
    enter, and never again" is a property of the type rather than of caller discipline.
    [LAW:parse-dont-validate] the constructor is the checkpoint: a `ProfileRef` that
    exists names two legal components, and `resolve_spec` can compose
    `profiles_root / ref.namespace / ref.name` without wondering whether either could
    carry a `..` or a separator.
    """

    namespace: str
    name: str

    def __post_init__(self) -> None:
        validate_name("namespace", self.namespace)
        validate_name("profile name", self.name)

    def __str__(self) -> str:
        return f"{self.namespace}/{self.name}"


@dataclass(frozen=True)
class ProfileSpec:
    """One `[profiles.<name>]` stanza, parsed but not yet resolved against a port.

    `name` is validated here for the same reason `ProfileRef` validates both of its
    fields: it is the profile's identity, and it is written to places that cannot
    re-check it. [LAW:parse-dont-validate] `configwrite._declare` indexes
    `profiles[spec.name]` straight into a TOML document, and `resolve_spec` composes
    `ProfileRef(scope.namespace, spec.name)` — so a name carrying a `/` or a `..` would
    become an illegal TOML key and a profile directory outside the profiles root.

    Every existing caller happens to validate first, which is exactly the arrangement
    this constructor replaces: a convention every future call site must rediscover
    becomes a property of the type. [LAW:types-are-the-program]
    """

    name: str
    flags: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    seed: Seed | None = None
    port: int | None = None

    def __post_init__(self) -> None:
        validate_name("profile name", self.name)


def _config_dir(source: Path | None) -> Path:
    """The directory a config's relative paths resolve against.

    [LAW:one-source-of-truth] `Scope` and `ResolvedProfile` both expose this and must
    agree — a profile's seed is parsed against the scope's answer and rendered back
    against the profile's, so a divergence would write a path that reads back as a
    different one. Stating the rule once is what makes them agree; two copies with a
    comment claiming they match is the arrangement that silently stops being true.

    The fileless `user` scope has no directory of its own, so it anchors on the working
    directory — which is why this is a function of `source` alone and nothing else.
    """
    return source.parent if source else Path.cwd()


@dataclass(frozen=True)
class Scope:
    """One config file's contents: a namespace, its defaults, and its profiles.

    `source` is None only for the implicit `user` scope on a machine with no user
    config file yet — the one case where a scope exists without a file behind it.
    """

    namespace: str
    source: Path | None
    profiles_root: Path
    chrome_binary: Path
    default_flags: tuple[str, ...] = ()
    default_env: dict[str, str] = field(default_factory=dict)
    # `DEFAULT_SEED`, not a literal — this dataclass default is the sixth answer to the
    # question that constant exists to answer, and it sat 80 lines below the comment
    # claiming the drift was gone. It is reachable, not decorative: `load_user_scope`
    # builds a fileless `Scope` without this field on a machine with no user config, so
    # that scope reported `fresh` while every file-backed scope reported `chrome`.
    default_seed: Seed = DEFAULT_SEED
    profiles: dict[str, ProfileSpec] = field(default_factory=dict)

    @property
    def config_dir(self) -> Path:
        """The directory a project's relative paths and ${CROM_CONFIG_DIR} resolve against."""
        return _config_dir(self.source)

    @property
    def is_user(self) -> bool:
        return self.namespace == USER_NAMESPACE


# --- the stamped type --------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedProfile:
    """Everything needed to launch, seed, find, or describe one profile.

    `argv` is complete: `subprocess.Popen(resolved.argv)` is the entire launch. This is
    the seam the rest of crom is built on — [LAW:composability] every consumer takes
    this one type, so none of them needs to know that config files or registries exist.
    """

    ref: ProfileRef
    port: int
    profile_dir: Path
    chrome_binary: Path
    argv: tuple[str, ...]
    env: dict[str, str]
    seed: Seed
    source: Path | None

    @property
    def cdp_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def config_dir(self) -> Path:
        """The directory this profile's relative paths are written and read against.

        The same notion `Scope.config_dir` carries, available downstream of resolution
        so a seed can be rendered back in the spelling its config file would use.
        """
        return _config_dir(self.source)

    def describe(self, *, running: bool, pids: tuple[int, ...]) -> dict:
        """The machine-readable view — the contract `--json` output promises.

        [LAW:one-source-of-truth] every command that emits JSON emits *this*, so the
        shape a consuming app parses is defined in exactly one place.
        """
        return {
            "namespace": self.ref.namespace,
            "profile": self.ref.name,
            "ref": str(self.ref),
            "port": self.port,
            "cdp_url": self.cdp_url,
            "profile_dir": str(self.profile_dir),
            "chrome_binary": str(self.chrome_binary),
            "source": str(self.source) if self.source else None,
            "running": running,
            "pids": list(pids),
        }


@dataclass(frozen=True)
class FailedProfile:
    """A declared profile that could not be resolved, carried as a value not a raise.

    `crom list` is the command a user reaches for *because* something is broken, so one
    unresolvable declaration must not hide every working one. [LAW:no-silent-failure]
    this is not a swallow: the failure is rendered inline in the listing and present in
    `--json`, so it is strictly more visible than the traceback it replaces — what
    changes is that it no longer takes the other profiles down with it.

    [LAW:dataflow-not-control-flow] `resolve_all` returns this alongside
    `ResolvedProfile`, so the variability is in the values the caller matches on rather
    than in whether the listing runs.
    """

    ref: ProfileRef
    error: str

    def describe(self) -> dict:
        return {
            "namespace": self.ref.namespace,
            "profile": self.ref.name,
            "ref": str(self.ref),
            "error": self.error,
        }


# What one entry in a listing is: resolved, or explaining why it is not.
ProfileEntry = ResolvedProfile | FailedProfile


def parse_ref(text: str, ambient: str) -> ProfileRef:
    """Parse a user-typed reference; a bare name resolves in the ambient namespace.

    `dev` -> ambient/dev, `myapp/dev` -> myapp/dev. Anything else is an error rather
    than a guess — [LAW:no-silent-failure] a mistyped reference must not quietly
    become a different profile.
    """
    parts = text.split("/")
    if len(parts) == 1:
        namespace, name = ambient, parts[0]
    elif len(parts) == 2:
        namespace, name = parts
    else:
        raise CromError(f"invalid profile reference {text!r}: expected 'name' or 'namespace/name'")
    # No validation here: splitting is this function's job, and `ProfileRef` validates
    # its own fields. [LAW:single-enforcer] one place decides what a legal name is.
    return ProfileRef(namespace, name)
