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

from .paths import USER_NAMESPACE

# Namespaces and profile names become both directory components and CLI tokens, so the
# legal set is the intersection of what is safe in each. [LAW:parse-dont-validate] this
# is checked once, where names enter, and never again.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


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


# --- declarations ------------------------------------------------------------------


@dataclass(frozen=True)
class ProfileRef:
    """A profile's global identity: which namespace, and which profile within it."""

    namespace: str
    name: str

    def __str__(self) -> str:
        return f"{self.namespace}/{self.name}"


@dataclass(frozen=True)
class ProfileSpec:
    """One `[profiles.<name>]` stanza, parsed but not yet resolved against a port."""

    name: str
    flags: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    seed: Seed | None = None
    port: int | None = None


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
    default_seed: Seed = SeedFresh()
    profiles: dict[str, ProfileSpec] = field(default_factory=dict)

    @property
    def config_dir(self) -> Path:
        """The directory a project's relative paths and ${CROM_CONFIG_DIR} resolve against."""
        return self.source.parent if self.source else Path.cwd()

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
    return ProfileRef(
        validate_name("namespace", namespace),
        validate_name("profile name", name),
    )
