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

from .model import CromError, ResolvedProfile, SeedChrome, SeedFresh, SeedPath

# Where the user's real Chrome keeps its user-data-dir, per platform.
_CHROME_USER_DATA: dict[str, Path] = {
    "darwin": Path.home() / "Library" / "Application Support" / "Google" / "Chrome",
    "linux": Path.home() / ".config" / "google-chrome",
    "win32": Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data",
}


def chrome_user_data_dir() -> Path:
    return _CHROME_USER_DATA.get(sys.platform, _CHROME_USER_DATA["linux"])


def _copy(source: Path, dest: Path, described: str) -> None:
    if not source.is_dir():
        raise CromError(f"seed {described} does not exist: {source}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # `dest` is either absent or the freshly-made empty staging directory, never a
    # profile with contents of its own.
    shutil.copytree(source, dest, dirs_exist_ok=True)


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
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    # Rename is the commit: the profile appears at its real path complete or not at all.
    os.replace(staging, destination)


def materialize(profile: ResolvedProfile) -> bool:
    """Create the profile directory if it is not there yet; report whether we did.

    Returns False when the directory already existed, which is the steady state — this
    makes `crom up` safe to call on every invocation without re-copying anything.
    """
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
