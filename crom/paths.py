"""Resolves crom's XDG base directories.

[LAW:one-source-of-truth] Config and state are different kinds of fact and live in
different trees: config is what a human writes, state is what crom writes. Every
other module asks here rather than composing `~/.config` itself.
"""

import os
from pathlib import Path

USER_NAMESPACE = "user"

CONFIG_FILENAME = "config.toml"
REGISTRY_FILENAME = "registry.json"

# Discovery candidates, in precedence order, relative to a directory being probed.
# The bare file covers the common case; the directory form is for projects that keep
# adjacent assets (seed profiles, extensions) next to their config.
PROJECT_CONFIG_CANDIDATES = (
    Path(".crom.toml"),
    Path(".crom") / "config.toml",
)


def _xdg(env: str, fallback: Path) -> Path:
    """The XDG directory named by `env`, or `fallback` when it is unset or relative.

    The spec says a relative value in these variables must be ignored, and here that is
    load-bearing rather than pedantry: crom is run from many different directories by
    design, so a relative `XDG_STATE_HOME` would put the ledger, the user config, and
    every profile directory somewhere different depending on the cwd at invocation —
    quietly dissolving the "same profile, same port, same directory every time"
    guarantee, and scattering cookies and logins across the filesystem while doing it.
    """
    raw = os.environ.get(env)
    if not raw:
        return fallback
    expanded = Path(raw).expanduser()
    return expanded if expanded.is_absolute() else fallback


def config_home() -> Path:
    return _xdg("XDG_CONFIG_HOME", Path.home() / ".config") / "crom"


def state_home() -> Path:
    return _xdg("XDG_STATE_HOME", Path.home() / ".local" / "state") / "crom"


def user_config_file() -> Path:
    """The `user` namespace's config file — a fixed location, never discovered."""
    return config_home() / CONFIG_FILENAME


def registry_file() -> Path:
    return state_home() / REGISTRY_FILENAME


def default_profiles_root() -> Path:
    """Where profile user-data-dirs live unless a scope overrides `state_dir`."""
    return state_home() / "profiles"
