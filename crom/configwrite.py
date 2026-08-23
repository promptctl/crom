"""Edits crom config files in place, preserving the comments and layout around the edit.

`crom add` and `crom rm` change files a human wrote and may keep in version control, so
they are edits, not rewrites: tomlkit round-trips the document and we touch only the one
table involved. [LAW:one-source-of-truth] the file on disk stays the authority on
everything crom did not explicitly change.
"""

import os
from pathlib import Path

import tomlkit

from .model import NotFound, ProfileSpec, Seed, SeedChrome, SeedFresh, SeedPath

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


def render_seed(seed: Seed) -> str:
    """The config spelling of a seed — the inverse of `config._parse_seed`."""
    match seed:
        case SeedFresh():
            return "fresh"
        case SeedChrome(profile="Default"):
            return "chrome"
        case SeedChrome(profile=which):
            return f"chrome:{which}"
        case SeedPath(path=path):
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
    """Write a new project config. Refuses to touch an existing file."""
    if path.exists():
        raise FileExistsError(f"{path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PROJECT_CONFIG_TEMPLATE.format(namespace=namespace))


def _declare(path: Path, spec: ProfileSpec, header: str) -> bool:
    """Append a `[profiles.<name>]` table for `spec`; report whether it was written.

    Returns False, having changed nothing, when the name is already declared — leaving
    each caller to say what that means for it. `header` is written only when the file is
    being created, so an existing file's own preamble is never duplicated or displaced.
    """
    if not path.exists() and header:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(header)

    doc = _load(path)
    profiles = doc.setdefault("profiles", tomlkit.table(is_super_table=True))
    if spec.name in profiles:
        return False

    table = tomlkit.table()
    table["seed"] = render_seed(spec.seed or SeedFresh())
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
    doc = _load(path)
    profiles = doc.get("profiles")
    if not profiles or name not in profiles:
        raise NotFound(f"{path}: profile '{name}' is not declared here")
    del profiles[name]
    _save(path, doc)
