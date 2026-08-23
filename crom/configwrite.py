"""Edits crom config files in place, preserving the comments and layout around the edit.

`crom add` and `crom rm` change files a human wrote and may keep in version control, so
they are edits, not rewrites: tomlkit round-trips the document and we touch only the one
table involved. [LAW:one-source-of-truth] the file on disk stays the authority on
everything crom did not explicitly change.
"""

import os
from pathlib import Path

import tomlkit

from .locking import exclusive
from .model import Conflict, NotFound, ProfileSpec, Seed, SeedChrome, SeedFresh, SeedPath

USER_CONFIG_HEADER = """\
# crom — your personal Chrome profiles (the `user` namespace).
#
# Profiles declared here are addressable from anywhere as `user/<name>`, or by bare
# name when you are not inside a project that has its own .crom.toml.
"""

PROJECT_CONFIG_TEMPLATE = """\
# crom — this project's Chrome profiles.
#
# The namespace keeps this project's profile directories and CDP ports clear of every
# other project's on this machine. Ports are assigned once, remembered, and reported by
# `crom port`; pin one with `port = 9401` under a profile if you need it fixed.

namespace = "{namespace}"

# Flags every profile in this namespace gets, appended after crom's launch policy.
# Available in flags and env values: ${{CROM_PROFILE_DIR}}, ${{CROM_CONFIG_DIR}},
# ${{CROM_PORT}}, ${{CROM_NAMESPACE}}, ${{CROM_PROFILE}}.
[defaults]
flags = []

[profiles.default]
# fresh | chrome | chrome:<Profile Name> | ./path/to/a/user-data-dir
seed = "fresh"
flags = []
"""


def render_seed(seed: Seed, base: Path) -> str:
    """The config spelling of a seed — the inverse of `config.parse_seed`.

    `base` is the directory the config file lives in, and a path under it is written
    back relative to it. `config.parse_seed` absolutizes every path seed against that
    same directory, so rendering the resolved path verbatim would bake this machine's
    layout into a file the README expects to be committed and shared: `--seed
    ./local-seed` would land in the file as `/home/you/project/local-seed`, which is
    correct on exactly one machine.

    A path outside `base` has no portable spelling and is written absolute — `..`
    chains out of the project would be portable in form and wrong in meaning.
    """
    match seed:
        case SeedFresh():
            return "fresh"
        case SeedChrome(profile="Default"):
            return "chrome"
        case SeedChrome(profile=which):
            return f"chrome:{which}"
        case SeedPath(path=path):
            return _relative_to(path, base)


def _relative_to(path: Path, base: Path) -> str:
    """`./x` when `path` sits under `base`, else the absolute path.

    The `./` prefix is load-bearing rather than decoration: `config.parse_seed` only
    treats a value as a path when it starts with `.`, `/`, or `~`, so a bare `local-seed`
    would come back as an unrecognised keyword.
    """
    try:
        return f"./{path.relative_to(base)}"
    except ValueError:
        return str(path)


def _load(path: Path) -> tomlkit.TOMLDocument:
    return tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()


def _save(path: Path, doc: tomlkit.TOMLDocument) -> None:
    """Replace the file atomically — this is a document a human wrote and may have
    committed, and `write_text` truncates before it writes. A failure partway through
    that would leave a half-written config, and crom's own parser is strict enough that
    the user would be locked out of every command until they repaired it by hand."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(tomlkit.dumps(doc))
    os.replace(tmp, path)


def init_project(path: Path, namespace: str) -> None:
    """Write a new project config, or refuse if one is already there.

    The refusal is the kernel's, not a check of ours: `O_CREAT | O_EXCL` creates the
    file only if it does not exist, atomically. An `exists()` test followed by a write
    is check-then-act, and two `crom init` calls in one directory could both pass it and
    the second would clobber the first's config — possibly with a different namespace —
    while both reported success.

    `Conflict` rather than the bare `FileExistsError` the kernel hands back: this is the
    exit-4 case the CLI contract promises. Raised here rather than translated at the call
    site so no future caller can reintroduce the gap by forgetting a wrapper.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError as e:
        raise Conflict(f"{path} already exists") from e
    with os.fdopen(fd, "w") as handle:
        handle.write(PROJECT_CONFIG_TEMPLATE.format(namespace=namespace))


def declares(path: Path, name: str) -> bool:
    """Whether `path` already declares a profile called `name`.

    Lets a caller find out that a name is taken *before* doing work that persists
    something — `crom add` reserves a port while resolving, and must not do that for a
    profile it is about to refuse.
    """
    return name in _load(path).get("profiles", {})


def _declare(path: Path, spec: ProfileSpec, header: str) -> bool:
    """Append a `[profiles.<name>]` table for `spec`; report whether it was written.

    Returns False, having changed nothing, when the name is already declared — leaving
    each caller to say what that means for it. `header` is written only when the file is
    being created, so an existing file's own preamble is never duplicated or displaced.

    The whole load-mutate-save runs under an exclusive lock: two `crom add` calls
    against one config would otherwise both read the document before either wrote, and
    the later `_save` would drop the earlier profile while its process still reported
    success. Same hazard the port ledger takes a lock for, same lock.
    """
    with exclusive(path):
        if not path.exists() and header:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(header)

        doc = _load(path)
        profiles = doc.setdefault("profiles", tomlkit.table(is_super_table=True))
        if spec.name in profiles:
            return False

        table = tomlkit.table()
        table["seed"] = render_seed(spec.seed or SeedFresh(), path.parent)
        table["flags"] = list(spec.flags)
        if spec.port is not None:
            table["port"] = spec.port
        if spec.env:
            table["env"] = dict(spec.env)
        profiles[spec.name] = table
        _save(path, doc)
        return True


def add_profile(path: Path, spec: ProfileSpec, *, header: str = "") -> None:
    """Declare a profile the user asked to create. A name already taken is a conflict."""
    if not _declare(path, spec, header):
        raise FileExistsError(f"{path}: profile '{spec.name}' is already declared")


def ensure_profile(path: Path, spec: ProfileSpec, *, header: str = "") -> bool:
    """Declare a profile unless it is already declared; report whether we wrote it.

    The converging twin of `add_profile`, for callers whose goal is that the declaration
    *exist* rather than that they be the one to create it. Migration is the caller that
    needs this: it re-runs from the top after a failed attempt, so a name it already
    wrote must be a no-op rather than a collision.
    """
    return _declare(path, spec, header)


def remove_profile(path: Path, name: str) -> None:
    """Delete a profile's declaration, under the same lock `_declare` writes beneath."""
    with exclusive(path):
        doc = _load(path)
        profiles = doc.get("profiles")
        if not profiles or name not in profiles:
            raise NotFound(f"{path}: profile '{name}' is not declared here")
        del profiles[name]
        _save(path, doc)
