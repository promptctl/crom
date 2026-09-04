"""Resolves crom's XDG base directories.

[LAW:one-source-of-truth] Config and state are different kinds of fact and live in
different trees: config is what a human writes, state is what crom writes. Every
other module asks here rather than composing `~/.config` itself.
"""

import os
from pathlib import Path

from .model import Reason

CONFIG_FILENAME = "config.toml"
REGISTRY_FILENAME = "registry.json"

# Discovery candidates, in precedence order, relative to a directory being probed.
# The bare file covers the common case; the directory form is for projects that keep
# adjacent assets (seed profiles, extensions) next to their config.
PROJECT_CONFIG_CANDIDATES = (
    Path(".crom.toml"),
    Path(".crom") / "config.toml",
)


def _home(env: str) -> Path:
    """The home directory, as a `CromError` rather than a `RuntimeError` when there
    is none. `env` names the variable that would have made this lookup unnecessary."""
    try:
        return Path.home()
    except RuntimeError as e:
        raise Reason.HOME_UNKNOWN.error(
            f"cannot determine your home directory ({e}), and {env} is not set to an "
            f"absolute path. Set {env} (or HOME) and run crom again."
        ) from e


def _expanduser(raw: str, env: str) -> Path:
    """`Path.expanduser`, with the same missing-home translation as `_home`.

    A leading `~` is the one override form that still consults the home directory, so
    it is the one place laziness alone does not make the XDG path home-free.
    """
    try:
        return Path(raw).expanduser()
    except RuntimeError as e:
        raise Reason.HOME_UNKNOWN.error(
            f"{env} is set to {raw!r}, but your home directory cannot be determined "
            f"({e}). Set {env} to an absolute path, or set HOME."
        ) from e


def _xdg(env: str, *fallback: str) -> Path:
    """The XDG directory named by `env`, or the `$HOME`-relative `fallback` when it is
    unset or relative.

    The spec says a relative value in these variables must be ignored, and here that is
    load-bearing rather than pedantry: crom is run from many different directories by
    design, so a relative `XDG_STATE_HOME` would put the ledger, the user config, and
    every profile directory somewhere different depending on the cwd at invocation —
    quietly dissolving the "same profile, same port, same directory every time"
    guarantee, and scattering cookies and logins across the filesystem while doing it.

    The fallback arrives as path *segments*, not as a built `Path`, so `Path.home()` is
    called only on the branch that needs it. Passing `Path.home() / ".config"` as an
    argument looked equivalent and was not: Python evaluates arguments eagerly, so the
    home lookup happened on every call — including the calls where `XDG_CONFIG_HOME` was
    set precisely so that crom would not need `$HOME`. The override did not deliver the
    independence it advertised, and still raised in the environments it was for: minimal
    containers and sandboxes with no passwd entry. [FRAMING:representation]
    """
    raw = os.environ.get(env)
    if raw:
        expanded = _expanduser(raw, env)
        if expanded.is_absolute():
            return expanded
    return _home(env).joinpath(*fallback)


def config_home() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "crom"


def state_home() -> Path:
    return _xdg("XDG_STATE_HOME", ".local", "state") / "crom"


def user_config_file() -> Path:
    """The `user` namespace's config file — a fixed location, never discovered."""
    return config_home() / CONFIG_FILENAME


def registry_file() -> Path:
    return state_home() / REGISTRY_FILENAME


def default_profiles_root() -> Path:
    """Where profile user-data-dirs live unless a scope overrides `state_dir`."""
    return state_home() / "profiles"
