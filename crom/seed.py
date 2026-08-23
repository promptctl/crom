"""Materializes a profile's user-data-dir from its declared seed, exactly once.

Seeding is a create-time act, not a launch-time one: once the directory exists it is
the profile's own state and crom never overwrites it. Which is why the seed is worth
choosing deliberately — copying a real Chrome profile duplicates hundreds of megabytes
and every cookie in it, so `fresh` is the default and `chrome` is opt-in.
"""

import contextlib
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

from .locking import exclusive
from .model import CromError, ResolvedProfile, SeedChrome, SeedFresh, SeedPath

# Where the user's real Chrome keeps its user-data-dir, per platform. POSIX only, as
# crom is throughout: `chrome.scan` answers "is this profile running" by shelling out to
# `ps`, so a Windows entry here would describe a platform no other part of crom reaches.
_CHROME_USER_DATA: dict[str, Path] = {
    "darwin": Path.home() / "Library" / "Application Support" / "Google" / "Chrome",
    "linux": Path.home() / ".config" / "google-chrome",
}


def chrome_user_data_dir() -> Path:
    return _CHROME_USER_DATA.get(sys.platform, _CHROME_USER_DATA["linux"])


def _copy(source: Path, dest: Path, described: str) -> None:
    if not source.is_dir():
        raise CromError(f"seed {described} does not exist: {source}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # `dest` is either absent or the freshly-made empty staging directory, never a
    # profile with contents of its own.
    #
    # `symlinks=True` copies a link as a link. The default dereferences it and copies
    # the *content* it points at, which turns a symlink inside a seed directory into a
    # way to pull a file the seed never contained — an `~/.ssh/id_rsa` link in a seed
    # checked into a repo would land as the real key inside a profile whose CDP port is
    # then open to local tooling. Preserving the link copies the pointer and nothing else.
    shutil.copytree(source, dest, dirs_exist_ok=True, symlinks=True)


@contextlib.contextmanager
def _staged(destination: Path) -> Iterator[Path]:
    """Build the profile beside its final path and move it in only once it is whole.

    [LAW:no-silent-failure] The directory's *existence* is what `materialize` reads as
    "already seeded", so a copy that dies halfway — disk full, unreadable file, a
    dangling `SingletonSocket` symlink in a user-data-dir — must leave nothing behind.
    Otherwise the next `crom up` finds the stump, concludes the profile is ready, and
    silently launches Chrome on a half-copied profile: the original failure is loud
    exactly once and every run after it is quietly wrong.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        yield staging
        # Rename is the commit: the profile appears at its real path complete or not at
        # all. It sits *inside* the guarded block because it can fail too — `os.replace`
        # onto a non-empty directory raises `ENOTEMPTY` — and a commit that failed
        # outside the guard would leave the staging directory behind forever, which is
        # precisely the "leave nothing behind" invariant this function exists to keep.
        os.replace(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def materialize(profile: ResolvedProfile) -> bool:
    """Create the profile directory if it is not there yet; report whether we did.

    Returns False when the directory already existed, which is the steady state — this
    makes `crom up` safe to call on every invocation without re-copying anything.

    The check and the copy are one critical section. `crom up` advertises itself as
    idempotent and safe to call concurrently, but unlocked both callers would see no
    directory, both build a full staging copy, and the loser's `os.replace` would fail
    on the winner's finished profile. Under the lock the second caller observes the
    directory and reports False, which is what idempotent was supposed to mean.
    """
    with exclusive(profile.profile_dir):
        return _materialize_locked(profile)


def _materialize_locked(profile: ResolvedProfile) -> bool:
    if profile.profile_dir.exists():
        return False

    with _staged(profile.profile_dir) as staging:
        match profile.seed:
            case SeedFresh():
                # Nothing to fill: Chrome builds a first-run profile in the empty
                # directory itself.
                pass
            case SeedChrome(profile=which):
                # A Chrome user-data-dir holds one directory per profile; we copy the
                # named one into the canonical slot so the browser opens straight into it.
                _copy(
                    chrome_user_data_dir() / which,
                    staging / "Default",
                    f"'chrome:{which}'",
                )
            case SeedPath(path=path):
                # A path is expected to be a whole user-data-dir, copied verbatim.
                _copy(path, staging, f"path '{path}'")
    return True
