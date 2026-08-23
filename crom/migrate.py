"""Moves a pre-namespace crom installation into the namespaced layout, once.

The old shape was flat: profile ports in `~/.config/crom/profiles.json`, user-data-dirs
at `<state>/<name>`, and a stray `<name>.pid` file left over from an even earlier design.
Everything it held becomes part of the `user` namespace, keeping each profile's existing
port — apps and `.mcp.json` files out in the world already point at those numbers.

[LAW:no-silent-failure] If any of those profiles has a live Chrome, we refuse and say
which: renaming a user-data-dir out from under a running browser would leave a process
crom can no longer find, stop, or account for.
"""

import json
import shutil
import sys
from pathlib import Path

from . import chrome, configwrite, registry
from .model import CromError, ProfileRef, ProfileSpec, SeedChrome
from .paths import (
    USER_NAMESPACE,
    config_home,
    default_profiles_root,
    state_home,
    user_config_file,
)

LEGACY_REGISTRY = "profiles.json"


def legacy_registry_file() -> Path:
    return config_home() / LEGACY_REGISTRY


def needed() -> bool:
    return legacy_registry_file().is_file()


def run_if_needed(log=lambda message: print(message, file=sys.stderr)) -> None:
    if not needed():
        return
    run(log)


def run(log) -> None:
    source = legacy_registry_file()
    legacy = json.loads(source.read_text()).get("profiles", {})
    old_dirs = {name: state_home() / name for name in legacy}

    _require_all_stopped(old_dirs)

    log(f"crom: migrating {len(legacy)} profile(s) into the '{USER_NAMESPACE}' namespace")
    destination_root = default_profiles_root() / USER_NAMESPACE
    destination_root.mkdir(parents=True, exist_ok=True)

    for name, entry in legacy.items():
        ref = ProfileRef(USER_NAMESPACE, name)
        # These profiles were all clones of the real Chrome profile; record that as their
        # seed so the config tells the truth about where their data came from.
        #
        # `ensure_profile`, not `add_profile`: the legacy registry is only retired after
        # the whole loop, so an attempt that dies partway is retried on the user's next
        # command — and every step here has to survive being run twice. It is the one
        # step that would not: re-declaring a name it already wrote would raise, and
        # because migration runs before anything else in `main`, that exception would
        # come back on *every* command and leave crom unusable with no way out.
        configwrite.ensure_profile(
            user_config_file(),
            ProfileSpec(name=name, seed=SeedChrome()),
            header=configwrite.USER_CONFIG_HEADER,
        )
        registry.adopt(ref, entry["port"], user_config_file())

        old_dir = old_dirs[name]
        if old_dir.is_dir():
            shutil.move(str(old_dir), str(destination_root / name))
        (state_home() / f"{name}.pid").unlink(missing_ok=True)
        log(f"crom:   {name} -> {ref} (port {entry['port']})")

    # Keep the old file rather than deleting it: it is the only record of the previous
    # assignment, and it costs nothing to leave behind.
    backup = source.with_suffix(".json.migrated")
    source.rename(backup)
    log(f"crom: done. Previous registry kept at {backup}")


def _require_all_stopped(old_dirs: dict[str, Path]) -> None:
    running = {
        name: pids
        for name, directory in old_dirs.items()
        if (pids := chrome.find_pids_for_dir(directory))
    }
    if not running:
        return
    listing = "\n  ".join(f"{name} (pid {', '.join(map(str, pids))})" for name, pids in running.items())
    raise CromError(
        "crom needs to move its profile directories into the new namespaced layout, "
        "but these profiles are still running:\n  "
        f"{listing}\n"
        "Quit those Chrome windows (or kill those pids) and run crom again."
    )
