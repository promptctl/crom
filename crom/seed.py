"""Materializes a profile's user-data-dir from its declared seed, exactly once.

Seeding is a create-time act, not a launch-time one: once the directory exists it is
the profile's own state and crom never overwrites it. Which is why the seed is worth
choosing deliberately — copying a real Chrome profile duplicates hundreds of megabytes
and every cookie in it, so `fresh` is the default and `chrome` is opt-in.

A seed is copied only while nothing is writing it: crom reads the seed's ancestry before
and after, and refuses — saying what it saw — if a browser holds it at either read.
"""

import contextlib
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from . import chrome
from .locking import exclusive
from .model import CromError, ResolvedProfile, Seed, SeedChrome, SeedFresh, SeedPath

# Where the user's real Chrome keeps its user-data-dir, per platform. POSIX only, as
# crom is throughout: `chrome.scan` answers "is this profile running" by shelling out to
# `ps`, so a Windows entry here would describe a platform no other part of crom reaches.
#
# A list per platform, resolved first-hit-wins — the same shape and the same strategy as
# `browser._CANDIDATES`, because the two answer halves of one question and disagreeing
# about which browsers exist is what went wrong. `_CANDIDATES` treats Chromium as
# first-class on both platforms while this table named only Google Chrome, so on a
# Chromium-only machine `find_chrome()` succeeded and then the very first command failed:
# `_bootstrap_user_config` seeds `user/default` with `SeedChrome()` unconditionally, so a
# fresh install could not run once.
_CHROME_USER_DATA: dict[str, tuple[Path, ...]] = {
    "darwin": (
        Path.home() / "Library" / "Application Support" / "Google" / "Chrome",
        Path.home() / "Library" / "Application Support" / "Chromium",
    ),
    "linux": (
        Path.home() / ".config" / "google-chrome",
        Path.home() / ".config" / "chromium",
    ),
}


def chrome_user_data_dir() -> Path:
    """The real browser's user-data-dir, for a `chrome` seed.

    Falls back to the first candidate when none exists, so the caller's "seed 'chrome'
    does not exist: …" still names a real path rather than reporting nothing.
    """
    candidates = _CHROME_USER_DATA.get(sys.platform, _CHROME_USER_DATA["linux"])
    return next((path for path in candidates if path.is_dir()), candidates[0])


def _link_guard(source: Path, described: str):
    """Build the `copytree(ignore=...)` hook that refuses a symlink crom cannot copy safely.

    A seed's links must satisfy one rule: **relative, and resolving inside the seed.**
    Both halves are load-bearing, and each rules out a different way the copy goes wrong.

    *Escaping* is unsafe whichever way it is handled, which is why it is refused rather
    than resolved. Dereferencing copies the *content* of whatever the link names, so a
    seed could pull in `~/.ssh/id_rsa` and land the real key inside a profile whose CDP
    port is reachable by local tooling. Preserving the link is worse in the other
    direction: `profile_dir` becomes Chrome's live user-data-dir, and Chrome writes
    `Default/Preferences` and its siblings with ordinary `open()`, which follows
    symlinks — so a planted link is a write primitive aimed at any file the invoking
    user can modify.

    *Absolute* is unsafe even when the target is inside the seed, because `copytree`
    recreates a link as `os.symlink(os.readlink(src), dst)` — the raw target string,
    never rewritten for the new root. An absolute in-tree link therefore survives the
    copy still pointing at the *original* seed, so the finished profile stays live-linked
    back to the directory it was supposed to be an isolated copy of, and Chrome writes
    through it into the real thing. A relative link has no such problem: it is
    interpreted against wherever it now sits, which is exactly the corresponding place
    in the new profile.

    Empirically this costs nothing. A real Chrome user-data-dir carries four links —
    `RunningChromeVersion`, `SingletonCookie` and `SingletonLock` are relative, and
    `SingletonSocket` is absolute but points into `/var/folders`, so it is refused by
    the escape rule regardless.

    Running as `copytree`'s own `ignore` hook, rather than as a walk of its own, is what
    closes the gap between checking and copying. The hook is handed the very listing
    `copytree` is about to act on, so there is one traversal and one notion of what the
    tree contains — where two independent walks left a window for an entry to be swapped
    after passing validation (CWE-367). The residual window is now the sub-millisecond
    gap between this hook and the individual `os.symlink`, rather than a whole tree walk.
    """
    root = source.resolve()

    def guard(dirpath: str, names: list[str]) -> set[str]:
        for name in names:
            entry = Path(dirpath) / name
            if not entry.is_symlink():
                continue
            raw = entry.readlink()
            if raw.is_absolute():
                raise CromError(
                    f"seed {described} contains an absolute symlink:\n"
                    f"  {entry.relative_to(source)} -> {raw}\n"
                    f"crom copies links verbatim, so an absolute link would still point "
                    f"at the original seed from inside the finished profile — and Chrome "
                    f"would write through it into the seed. Make it relative."
                )
            target = (entry.parent / raw).resolve()
            if target == root or root in target.parents:
                continue
            raise CromError(
                f"seed {described} contains a symlink that points outside it:\n"
                f"  {entry.relative_to(source)} -> {target}\n"
                f"crom will not copy it: following the link would pull that file into "
                f"the profile, and keeping it would let Chrome write through it. Remove "
                f"the link, or point it inside the seed."
            )
        return set()

    return guard


@dataclass(frozen=True)
class _Copy:
    """One directory to duplicate."""

    source: Path
    dest: Path
    described: str


def _refuse(copy: _Copy, held: Path, holder: str, lead: str) -> CromError:
    """The one thing crom says when it will not read a seed: what it saw, and the way out."""
    return CromError(
        chrome.printable(
            f"seed {copy.described} {lead}:\n"
            f"  {held}\n"
            f"  {holder}\n"
            f"crom will not copy a user-data-dir a browser is writing. Chrome keeps Cookies, "
            f"History, Login Data and Web Data in SQLite databases it writes continuously, "
            f"so a copy taken now can catch one mid-transaction — and the damage surfaces "
            f"much later as missing history or a profile-error dialog, with nothing pointing "
            f"back here.\n"
            f'Quit that browser and run this again, or set `seed = "fresh"` for a '
            f"profile that starts empty."
        )
    )


def _held(source: Path) -> tuple[Path, str] | None:
    """The nearest directory over `source` a browser is writing, and its evidence.

    Chrome takes *one* singleton, at the user-data-dir root, and it governs everything
    beneath — so the question a copy has to ask is not "is this directory a user-data-dir",
    which is unanswerable for the arbitrary trees a `path` seed may name, but "is anything
    above this one held". Walking ancestors answers that without ever classifying a
    directory, which is what makes it total over both seed kinds: a `chrome` seed's root
    is an ancestor of the profile it copies, and a `path` seed naming `Chrome/Default` —
    or `Chrome/Default/Extensions` — finds the lock that a parent-only rule would miss.
    """
    for directory in (source, *source.parents):
        holder = chrome.singleton_holder(directory)
        if holder is not None:
            return directory, holder
    return None


@contextlib.contextmanager
def _undisturbed(copy: _Copy) -> Iterator[None]:
    """Read the seed's ancestry before and after, and refuse unless both say idle.

    One check would only prove the browser was closed at the instant crom looked. Chrome
    takes its singleton at startup and holds it for the session, so a browser opened
    while `copytree` was walking leaves the lock behind for the second read to find — and
    `_staged` then discards the partial copy rather than commit a torn one.

    What remains uncovered is a browser that both starts *and* exits cleanly inside the
    copy, which erases its own evidence. That is the residue of this approach, not an
    oversight; closing it would need a generation counter Chrome does not keep.
    """
    before = _held(copy.source)
    if before is not None:
        raise _refuse(copy, *before, "is in use")
    yield
    after = _held(copy.source)
    if after is not None:
        raise _refuse(copy, *after, "was opened by a browser while crom was copying it")


def _copy(copy: _Copy) -> None:
    # `os.stat` rather than `Path.is_dir`, because what `is_dir` does with an unreadable
    # path is not settled across the Pythons crom supports: 3.12 raises, 3.14 answers
    # False. Either would report a seed we were merely refused as one that is not there.
    try:
        present = stat.S_ISDIR(os.stat(copy.source).st_mode)
    except FileNotFoundError:
        present = False
    except OSError as e:
        raise CromError(
            f"seed {copy.described} cannot be read: {copy.source}: {e.strerror}"
        ) from e
    if not present:
        raise CromError(f"seed {copy.described} does not exist: {copy.source}")
    copy.dest.parent.mkdir(parents=True, exist_ok=True)
    # `dest` is either absent or the freshly-made empty staging directory, never a
    # profile with contents of its own.
    #
    # `symlinks=True` copies a link as a link rather than dereferencing it; `_link_guard`
    # vets each directory's entries as `copytree` reaches them, so every link that gets
    # recreated is relative and resolves inside the tree.
    shutil.copytree(
        copy.source, copy.dest, dirs_exist_ok=True, symlinks=True,
        ignore=_link_guard(copy.source, copy.described),
    )


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
        for copy in _plan(profile.seed, staging):
            with _undisturbed(copy):
                _copy(copy)
    return True


def _plan(seed: Seed, staging: Path) -> tuple[_Copy, ...]:
    """What a seed means as directories to duplicate — nought, or one.

    A plan rather than three arms that each copy, so that everything true of *a* copy —
    the stillness check today, whatever comes next — is written once at the one place the
    plan is walked, and cannot be added to `chrome` while being forgotten for `path`.
    [LAW:single-enforcer]

    `fresh` is the empty plan rather than a case that skips the copy: the loop below runs
    the same way for every seed, and the seed decides only what flows through it.
    [LAW:dataflow-not-control-flow]
    """
    match seed:
        case SeedFresh():
            # Chrome builds a first-run profile in the empty directory itself.
            return ()
        case SeedChrome(profile=which):
            # A Chrome user-data-dir holds one directory per profile; we copy the named
            # one into the canonical slot so the browser opens straight into it.
            root = chrome_user_data_dir()
            return (_Copy(root / which, staging / "Default", f"'chrome:{which}'"),)
        case SeedPath(path=path):
            return (_Copy(path, staging, f"path '{path}'"),)
