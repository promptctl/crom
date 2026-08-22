"""Edits crom config files in place, preserving the comments and layout around the edit.

`crom add` and `crom rm` change files a human wrote and may keep in version control, so
they are edits, not rewrites: tomlkit round-trips the document and we touch only the one
table involved. [LAW:one-source-of-truth] the file on disk stays the authority on
everything crom did not explicitly change.
"""

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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc))


def init_project(path: Path, namespace: str) -> None:
    """Write a new project config. Refuses to touch an existing file."""
    if path.exists():
        raise FileExistsError(f"{path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PROJECT_CONFIG_TEMPLATE.format(namespace=namespace))


def add_profile(path: Path, spec: ProfileSpec, *, header: str = "") -> None:
    """Append a `[profiles.<name>]` table declaring `spec`.

    `header` is written only when the file is being created, so an existing file's own
    preamble is never duplicated or displaced.
    """
    if not path.exists() and header:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(header)

    doc = _load(path)
    profiles = doc.setdefault("profiles", tomlkit.table(is_super_table=True))
    if spec.name in profiles:
        raise FileExistsError(f"{path}: profile '{spec.name}' is already declared")

    table = tomlkit.table()
    table["seed"] = render_seed(spec.seed or SeedFresh())
    table["flags"] = list(spec.flags)
    if spec.port is not None:
        table["port"] = spec.port
    if spec.env:
        table["env"] = dict(spec.env)
    profiles[spec.name] = table
    _save(path, doc)


def remove_profile(path: Path, name: str) -> None:
    doc = _load(path)
    profiles = doc.get("profiles")
    if not profiles or name not in profiles:
        raise NotFound(f"{path}: profile '{name}' is not declared here")
    del profiles[name]
    _save(path, doc)
