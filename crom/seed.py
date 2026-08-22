"""Materializes a profile's user-data-dir from its declared seed, exactly once.

Seeding is a create-time act, not a launch-time one: once the directory exists it is
the profile's own state and crom never overwrites it. Which is why the seed is worth
choosing deliberately — copying a real Chrome profile duplicates hundreds of megabytes
and every cookie in it, so `fresh` is the default and `chrome` is opt-in.
"""

import os
import shutil
import sys
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
    shutil.copytree(source, dest)


def materialize(profile: ResolvedProfile) -> bool:
    """Create the profile directory if it is not there yet; report whether we did.

    Returns False when the directory already existed, which is the steady state — this
    makes `crom up` safe to call on every invocation without re-copying anything.
    """
    if profile.profile_dir.exists():
        return False

    match profile.seed:
        case SeedFresh():
            # Chrome builds a first-run profile here itself.
            profile.profile_dir.mkdir(parents=True)
        case SeedChrome(profile=which):
            # A Chrome user-data-dir holds one directory per profile; we copy the named
            # one into the canonical slot so the new browser opens straight into it.
            _copy(
                chrome_user_data_dir() / which,
                profile.profile_dir / "Default",
                f"'chrome:{which}'",
            )
        case SeedPath(path=path):
            # A path is expected to be a whole user-data-dir, copied verbatim.
            _copy(path, profile.profile_dir, f"path '{path}'")
    return True
