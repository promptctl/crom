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
from .locking import exclusive
from .model import CromError, ProfileRef, ProfileSpec, SeedChrome, validate_name
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
    # Migration runs at the top of every command, so two crom processes started moments
    # apart after an upgrade both see `needed()` here. The lock makes one of them the
    # migrator; the re-check inside it makes the other a no-op rather than a second
    # migration racing the first onto the same config file and the same final rename.
    # [LAW:no-ambient-temporal-coupling] "has this happened yet" becomes state read
    # under the lock, not a guess made before acquiring it.
    with exclusive(legacy_registry_file()):
        if not needed():
            return
        run(log)


def run(log) -> None:
    source = legacy_registry_file()
    legacy = json.loads(source.read_text()).get("profiles", {})
    old_dirs = {name: state_home() / name for name in legacy}

    _require_all_stopped(old_dirs)
    _require_legal_names(legacy)

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
        # The legacy registry created entries as `{}` and only added "port" the first
        # time a profile was actually launched, so a profile the user declared but never
        # brought up has no port to preserve. `adopt` exists to keep a number the world
        # already points at; with no such number there is nothing to keep, and the
        # profile simply gets one assigned now.
        legacy_port = entry.get("port")
        if legacy_port is None:
            port = registry.port_for(ref, pinned=None, source=user_config_file())
        else:
            port = legacy_port
            registry.adopt(ref, port, user_config_file())

        old_dir = old_dirs[name]
        if old_dir.is_dir():
            shutil.move(str(old_dir), str(destination_root / name))
        (state_home() / f"{name}.pid").unlink(missing_ok=True)
        log(f"crom:   {name} -> {ref} (port {port})")

    # Keep the old file rather than deleting it: it is the only record of the previous
    # assignment, and it costs nothing to leave behind.
    backup = source.with_suffix(".json.migrated")
    source.rename(backup)
    log(f"crom: done. Previous registry kept at {backup}")


def _require_legal_names(legacy: dict) -> None:
    """Refuse the whole migration if any legacy name is illegal under the new rules.

    The old registry never validated names, so `Default`, `Work`, or `QA env` were all
    possible; `config.parse` now rejects them. Writing one into the generated TOML would
    make every later command fail to load the file — and because a successful run
    retires the legacy registry, `needed()` would be false and there would be no way
    back. So this refuses *before* the first write, while the legacy file is still
    intact and the user can rename.

    [LAW:no-silent-failure] Deliberately not slugified: a profile directory the user's
    own tooling and `.mcp.json` files point at must not be renamed behind their back.
    """
    illegal = []
    for name in legacy:
        try:
            validate_name("profile name", name)
        except CromError as e:
            illegal.append(f"{name!r}: {e}")
    if not illegal:
        return
    listing = "\n  ".join(illegal)
    raise CromError(
        "crom cannot migrate these profiles — their names are not legal under the "
        f"namespaced layout:\n  {listing}\n"
        f"Rename them in {legacy_registry_file()} (and rename the matching directory "
        f"under {state_home()}), then run crom again."
    )


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
