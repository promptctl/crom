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
import os
import shutil
import sys
from pathlib import Path

from . import chrome, configwrite, registry
from .config import parse_port
from .locking import exclusive
from .model import CromError, ProfileRef, ProfileSpec, SeedChrome, validate_name
from .paths import (
    USER_NAMESPACE,
    default_profiles_root,
    user_config_file,
)

LEGACY_REGISTRY = "profiles.json"


# Where the pre-namespace crom actually put its data.
#
# [LAW:no-silent-failure] These are deliberately *not* `config_home()`/`state_home()`,
# and must not be "tidied up" into them. Those helpers are new in the namespaced layout
# and honor `XDG_CONFIG_HOME`/`XDG_STATE_HOME`; the removed `crom/profiles.py` that
# wrote the data being migrated never consulted either variable and hardcoded these
# paths unconditionally. Looking through the XDG-aware helpers would therefore search
# where the legacy data was never written: for a user who has those variables set,
# `needed()` returns False, crom bootstraps as if fresh, and their profiles are
# abandoned with new ports assigned and no warning. Migration reads a historical
# artifact, so it has to look where that artifact was actually put.
#
# Functions rather than constants so the lookup happens at call time and honors `HOME`,
# which is what `Path.home()` reads and what the legacy module resolved against too.
def _legacy_config_dir() -> Path:
    return Path.home() / ".config" / "crom"


def _legacy_state_dir() -> Path:
    return Path.home() / ".local" / "state" / "crom"


def legacy_registry_file() -> Path:
    return _legacy_config_dir() / LEGACY_REGISTRY


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


def _read_legacy(source: Path) -> dict[str, dict]:
    """Parse the legacy registry into a shape the rest of this module can trust.

    Everything downstream indexes this document — `entry.get("port")`, iteration over
    names — so its shape is checked once here rather than at each use.
    [LAW:parse-dont-validate] the return type is the guarantee: a mapping of name to
    entry-dict, or no return at all.

    Any failure to hold that shape is a `CromError`, never a raw exception. A
    `JSONDecodeError` or an `AttributeError` from a hand-edited file is not a
    `CromError`, so it escapes the CLI's exit-code contract — and because migration runs
    before every command until it succeeds, that traceback would be the only thing crom
    could do, with no way out. The same guard `registry._read` applies to the current
    ledger.
    """
    def refuse(problem: str) -> CromError:
        return CromError(
            f"{source}: the previous crom registry {problem}.\n"
            f"Repair it, or move it aside — crom will then start fresh, and your existing "
            f"profiles will be re-created with new ports."
        )

    try:
        document = json.loads(source.read_text())
    except json.JSONDecodeError as e:
        raise refuse(f"is not valid JSON ({e})") from e

    if not isinstance(document, dict):
        raise refuse(f"is a JSON {type(document).__name__}, not an object")
    profiles = document.get("profiles", {})
    if not isinstance(profiles, dict):
        raise refuse(f"has a 'profiles' key that is a {type(profiles).__name__}, not an object")
    for name, entry in profiles.items():
        if not isinstance(entry, dict):
            raise refuse(f"entry for '{name}' is a {type(entry).__name__}, not an object")
        # The port is indexed straight into `registry.adopt`, which checks who may hold a
        # number but not whether it is one. An unvalidated value is persisted into the new
        # ledger and surfaces later as a TypeError from `socket.bind` or inside Chrome's
        # argv — far from the file that caused it. [LAW:single-enforcer] the 1..65535 rule
        # has one home in `parse_port`; this reuses it rather than restating it.
        if "port" in entry:
            try:
                parse_port(entry["port"], f"profile '{name}'", source)
            except CromError as e:
                raise refuse(f"has a bad port for '{name}' ({e})") from e
    return profiles


def run(log) -> None:
    source = legacy_registry_file()
    legacy = _read_legacy(source)
    old_dirs = {name: _legacy_state_dir() / name for name in legacy}

    destination_root = default_profiles_root() / USER_NAMESPACE

    # Everything that can refuse this migration runs before the first write. A refusal
    # partway through is far worse here than elsewhere: the legacy registry is only
    # retired after the whole loop, so a retry re-enters and fails at the identical
    # point, leaving the user with a half-migrated installation and no way forward.
    _require_all_stopped(old_dirs)
    _require_legal_names(legacy)
    _require_distinct_ports(legacy, source)
    _require_no_destination_collision(old_dirs, destination_root)

    log(f"crom: migrating {len(legacy)} profile(s) into the '{USER_NAMESPACE}' namespace")
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

        # Unconditionally: `_move_staged` decides what is left to do by reading the
        # destination and the staging directory, and an interrupted commit is a state
        # only it can see — a caller-side `old_dir.is_dir()` guard would skip exactly
        # the case that needs finishing.
        _move_staged(old_dirs[name], destination_root / name)
        (_legacy_state_dir() / f"{name}.pid").unlink(missing_ok=True)
        log(f"crom:   {name} -> {ref} (port {port})")

    # Keep the old file rather than deleting it: it is the only record of the previous
    # assignment, and it costs nothing to leave behind.
    backup = source.with_suffix(".json.migrated")
    source.rename(backup)
    log(f"crom: done. Previous registry kept at {backup}")


def _move_staged(old_dir: Path, destination: Path) -> None:
    """Move a profile directory so an interrupted attempt is finished by the next one.

    On one filesystem `shutil.move` is a rename and is already atomic. Across
    filesystems it degrades to copy-then-delete, and a failure mid-copy leaves the
    destination partly populated *and* the source still in place. This module is built
    so a failed attempt is resumed by the next command, and that retry would re-enter
    with a non-empty destination — turning a recoverable interruption into a permanent
    one. Staging beside the destination and committing with a rename keeps the retry's
    precondition true, the same shape `seed._staged` uses.

    How far the previous attempt got is **three** states, not two, and reading only
    `old_dir` conflated the last two. A same-filesystem `shutil.move` is a rename, so an
    interruption between it and the commit leaves the data whole in staging with
    `old_dir` already gone. The retry then saw no `old_dir`, concluded there was nothing
    to do, and left a complete profile stranded in a hidden directory that no code path
    ever looked at again — and because migration declares these profiles `seed = "chrome"`,
    the next `crom up` silently rebuilt the profile from the real Chrome profile instead
    of the user's actual data. Worse, had this function been re-entered, its first act
    was to delete that staging directory as debris.

    [LAW:no-ambient-temporal-coupling] the progress of the last attempt is state on
    disk, read here, rather than inferred from which of two paths happens to exist.
    """
    staging = destination.parent / f".{destination.name}.partial"

    if destination.exists():
        # Committed on an earlier attempt. `os.replace` is the only thing that creates
        # `destination` and it is atomic, so its presence means done — and any staging
        # beside it really is debris.
        shutil.rmtree(staging, ignore_errors=True)
        return
    if staging.exists():
        # Moved but not committed. Finish the commit rather than starting over; the data
        # is already here and `old_dir` is already gone.
        os.replace(staging, destination)
        return
    if not old_dir.is_dir():
        return

    try:
        shutil.move(str(old_dir), str(staging))
        os.replace(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _require_distinct_ports(legacy: dict, source: Path) -> None:
    """Refuse before the first write if two legacy profiles claim one port.

    `registry.adopt` already refuses the second claimant — but it does so from inside the
    loop, after earlier profiles have been declared and adopted. The legacy registry is
    retired only when the whole loop succeeds, so the retry re-enters and raises at the
    identical point, forever: a half-migrated installation with no way forward and no way
    back. The same reasoning `_require_legal_names` is built on, applied to the other
    thing a hand-edited registry can get wrong.
    """
    seen: dict[int, str] = {}
    for name, entry in legacy.items():
        port = entry.get("port")
        if port is None:
            continue
        if port in seen:
            raise CromError(
                f"{source}: '{name}' and '{seen[port]}' both claim port {port}.\n"
                f"crom cannot give one port to two profiles. Edit that file to give them "
                f"different ports (or remove one `port`, and crom will assign a fresh "
                f"one), then run crom again."
            )
        seen[port] = name


def _require_no_destination_collision(old_dirs: dict[str, Path], destination_root: Path) -> None:
    """Refuse if a legacy profile directory contains the place its data must move into.

    With `XDG_STATE_HOME` unset — the ordinary case — `state_home()` and
    `_legacy_state_dir()` are the same directory, which is deliberate: the two layouts do
    not collide because new profiles live under `<state>/profiles/<ns>/<name>` while
    legacy ones sat directly at `<state>/<name>`. That holds for every legacy name except
    one. A profile named `profiles` sat at `<state>/profiles`, which is exactly the root
    every migrated profile is moving into — so the move becomes "put this directory
    inside itself", which `shutil` refuses with a raw `shutil.Error`, escaping the
    exit-code contract partway through the loop.

    `_NAME_RE` accepts `profiles`, and the old scheme validated nothing, so this is
    reachable by a user who once ran `crom add profiles`.

    Refusing beats renaming the profiles root: that path is documented, and it is where
    every already-migrated installation's data now lives — changing it to dodge one name
    would strand data for every existing user. Stated as a relationship between paths
    rather than as the literal name `profiles`, so it still holds if that layout changes.
    """
    for name, old_dir in old_dirs.items():
        if old_dir == destination_root or old_dir in destination_root.parents:
            raise CromError(
                f"crom cannot migrate the profile '{name}': its directory ({old_dir}) is "
                f"where the namespaced layout keeps every profile ({destination_root}), "
                f"so moving it would put it inside itself.\n"
                f"Rename it in {legacy_registry_file()} (and rename the directory to "
                f"match), then run crom again."
            )


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
        # The *legacy* state directory, which is where the directories being renamed
        # actually are — naming the new XDG-aware one would send a user with
        # XDG_STATE_HOME set to a directory their profiles are not in.
        f"Rename them in {legacy_registry_file()} (and rename the matching directory "
        f"under {_legacy_state_dir()}), then run crom again."
    )


def _require_all_stopped(old_dirs: dict[str, Path]) -> None:
    # One `ps` for the whole set, not one per profile. `find_pids_for_dir` re-runs
    # `scan()` on every call and `scan()` spawns a subprocess, so asking it per profile
    # would cost N processes to answer a question `scan` is built to answer for all of
    # them at once. [LAW:dataflow-not-control-flow] read the table once, then look each
    # directory up in the value — the same shape `cli.list_cmd` uses.
    live = chrome.scan()
    running = {
        name: pids
        for name, directory in old_dirs.items()
        if (pids := live.get(str(directory)))
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
