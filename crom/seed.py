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


def _reject_escaping_symlinks(source: Path, described: str) -> None:
    """Refuse a seed containing a link that points outside itself.

    Neither way of handling such a link is safe, which is why this rejects rather than
    choosing one. Dereferencing copies the *content* of whatever the link names, so a
    seed could pull in `~/.ssh/id_rsa` and land the real key inside a profile whose CDP
    port is reachable by local tooling. Preserving the link is worse in the other
    direction: `profile_dir` becomes Chrome's live user-data-dir, and Chrome writes
    `Default/Preferences` and its siblings with ordinary `open()`, which follows
    symlinks — so a planted link is a write primitive aimed at any file the invoking
    user can modify.

    Links that stay inside the tree are kept as links: they resolve within the finished
    profile, and they are how a real Chrome user-data-dir's own internal links survive
    the copy.

    `os.walk(followlinks=False)` rather than `rglob`: a symlinked directory still needs
    checking, but must not be recursed into — a link cycle would otherwise hang here.
    """
    root = source.resolve()
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        for name in (*dirnames, *filenames):
            entry = Path(dirpath) / name
            if not entry.is_symlink():
                continue
            # An absolute target discards the left side of the join, so this handles
            # relative and absolute links alike.
            target = (entry.parent / entry.readlink()).resolve()
            if target == root or root in target.parents:
                continue
            raise CromError(
                f"seed {described} contains a symlink that points outside it:\n"
                f"  {entry.relative_to(source)} -> {target}\n"
                f"crom will not copy it: following the link would pull that file into "
                f"the profile, and keeping it would let Chrome write through it. Remove "
                f"the link, or point it inside the seed."
            )


def _copy(source: Path, dest: Path, described: str) -> None:
    if not source.is_dir():
        raise CromError(f"seed {described} does not exist: {source}")
    _reject_escaping_symlinks(source, described)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # `dest` is either absent or the freshly-made empty staging directory, never a
    # profile with contents of its own.
    #
    # `symlinks=True` copies a link as a link rather than dereferencing it. Every link
    # that survives the check above resolves inside the tree, so the copy reproduces the
    # seed's internal structure without reaching outside it in either direction.
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
    with profile_lock(profile):
        return materialize_under_lock(profile)


def profile_lock(profile: ResolvedProfile):
    """The exclusive lock guarding one profile's directory.

    Public because bringing a profile up is a longer critical section than seeding: the
    liveness check and the launch have to sit under the same lock, or two `crom up`
    calls both see no running Chrome and both start one. `flock` on a second descriptor
    blocks even within one process, so the caller takes this once and calls
    `materialize_under_lock` rather than nesting `materialize`.
    """
    return exclusive(profile.profile_dir)


def materialize_under_lock(profile: ResolvedProfile) -> bool:
    """`materialize`'s body, for a caller already holding `profile_lock`."""
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
