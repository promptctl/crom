"""Tests for profile materialization — that a profile directory appears whole or not at all.

`materialize` reads the directory's existence as "already seeded", so these tests are
mostly about the failure path: a copy that dies partway must not leave a stump behind
for the next run to mistake for a finished profile.
"""

import os
import shutil
import subprocess
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from crom import chrome, seed
from crom.model import (
    CromError,
    ProfileRef,
    Reason,
    ResolvedProfile,
    SeedChrome,
    SeedFresh,
    SeedPath,
)


def profile(profile_dir: Path, spec_seed) -> ResolvedProfile:
    return ResolvedProfile(
        ref=ProfileRef("myapp", "dev"),
        port=9300,
        profile_dir=profile_dir,
        chrome_binary=Path("/chrome"),
        argv=(),
        env={},
        seed=spec_seed,
        source=None,
    )


def _dead_pid() -> int:
    """A pid that has certainly exited — spawned, waited on, and reaped."""
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


class MaterializeTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.dest = self.root / "profiles" / "myapp" / "dev"

    def test_fresh_seed_creates_an_empty_directory(self):
        self.assertTrue(seed.materialize(profile(self.dest, SeedFresh())))
        self.assertTrue(self.dest.is_dir())
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_existing_profile_is_never_reseeded(self):
        self.dest.mkdir(parents=True)
        (self.dest / "Cookies").write_text("mine")
        self.assertFalse(seed.materialize(profile(self.dest, SeedFresh())))
        self.assertEqual((self.dest / "Cookies").read_text(), "mine")

    def test_path_seed_is_copied_verbatim(self):
        source = self.root / "template"
        (source / "Default").mkdir(parents=True)
        (source / "Default" / "Preferences").write_text("{}")

        self.assertTrue(seed.materialize(profile(self.dest, SeedPath(source))))
        self.assertEqual((self.dest / "Default" / "Preferences").read_text(), "{}")

    def test_chrome_seed_lands_in_the_default_slot(self):
        chrome_root = self.root / "chrome"
        (chrome_root / "Profile 1").mkdir(parents=True)
        (chrome_root / "Profile 1" / "Preferences").write_text("{}")

        with mock.patch.object(seed, "chrome_user_data_dir", return_value=chrome_root):
            self.assertTrue(seed.materialize(profile(self.dest, SeedChrome(profile="Profile 1"))))
        self.assertEqual((self.dest / "Default" / "Preferences").read_text(), "{}")

    def test_missing_seed_source_reports_and_leaves_nothing_behind(self):
        with self.assertRaisesRegex(CromError, "does not exist"):
            seed.materialize(profile(self.dest, SeedPath(self.root / "absent")))
        self.assertFalse(self.dest.exists())

    def _source(self) -> Path:
        source = self.root / "template"
        (source / "sub").mkdir(parents=True)
        (source / "sub" / "a.txt").write_text("a")
        return source

    @staticmethod
    def _dies_partway():
        """Stub a copy that writes some of the tree and then fails.

        Induced by stubbing the copy rather than by planting a dangling symlink: crom
        now copies links as links (so a dangling one is reproduced, not chased), and a
        test that leaned on dereferencing would be asserting the mechanism instead of
        the contract. What matters here is only that the copy failed after writing
        something — every arm below is about what is left behind when it does.
        """
        def explode(source, dest, **kwargs):
            Path(dest).mkdir(parents=True, exist_ok=True)
            (Path(dest) / "half-written").write_text("partial")
            raise OSError("disk full")

        return mock.patch.object(seed.shutil, "copytree", side_effect=explode)

    def test_a_copy_that_fails_partway_leaves_no_profile_to_mistake_for_a_finished_one(self):
        source = self._source()
        with self._dies_partway(), self.assertRaises(OSError):
            seed.materialize(profile(self.dest, SeedPath(source)))

        # The whole point: without an atomic commit this directory survives half-built,
        # and the *next* crom up launches Chrome on it without a word.
        self.assertFalse(self.dest.exists())

    def test_the_run_after_a_failed_copy_can_still_seed_the_profile(self):
        source = self._source()
        with self._dies_partway(), self.assertRaises(OSError):
            seed.materialize(profile(self.dest, SeedPath(source)))

        self.assertTrue(seed.materialize(profile(self.dest, SeedPath(source))))
        self.assertTrue((self.dest / "sub").is_dir())

    def _stray_directories(self) -> list[Path]:
        """Leftover directories beside the profile.

        Directories are the discriminator: a staging area is always one, while the lock
        `materialize` serializes on is a file that is *meant* to persist and be reused —
        the same way the port ledger keeps its own lock beside it.
        """
        return [p for p in self.dest.parent.iterdir() if p.is_dir()]

    def test_staging_leaves_no_debris_beside_the_profile(self):
        source = self._source()
        with self._dies_partway(), self.assertRaises(OSError):
            seed.materialize(profile(self.dest, SeedPath(source)))

        self.assertEqual(self._stray_directories(), [])

    def test_a_failed_commit_still_clears_the_staging_directory(self):
        """The commit is inside the guarded block, so even it can fail cleanly.

        `os.replace` onto a non-empty directory raises ENOTEMPTY — the shape a lost race
        for a first-time profile takes. It used to sit outside the try, so the loser's
        staging directory was orphaned beside the profile forever.
        """
        source = self._source()
        with mock.patch.object(seed.os, "replace", side_effect=OSError("ENOTEMPTY")):
            with self.assertRaises(OSError):
                seed.materialize(profile(self.dest, SeedPath(source)))

        self.assertFalse(self.dest.exists())
        self.assertEqual(self._stray_directories(), [])

    def test_a_seed_pointing_outside_itself_is_refused(self):
        """Neither handling of an escaping link is safe, so the seed is refused.

        Dereferencing copies the *content* of whatever the link names, pulling a file
        into a profile whose CDP port is reachable by local tooling. Preserving it is
        worse in the other direction: profile_dir becomes Chrome's live user-data-dir,
        and Chrome writes `Default/Preferences` and friends with ordinary `open()`,
        which follows symlinks — so the link becomes a write primitive aimed at any file
        the invoking user can modify.
        """
        source = self._source()
        secret = self.root / "secret.txt"
        secret.write_text("private key")
        (source / "Default").mkdir()
        # Relative, so this exercises the escape rule specifically. An absolute link
        # would be refused by the absolute-link rule first, and this test would pass
        # without ever proving that escaping is caught.
        (source / "Default" / "Preferences").symlink_to(Path("..") / ".." / "secret.txt")

        with self.assertRaisesRegex(CromError, "points outside it"):
            seed.materialize(profile(self.dest, SeedPath(source)))

        self.assertFalse(self.dest.exists())
        self.assertEqual(secret.read_text(), "private key")  # untouched

    def test_a_relative_link_inside_the_seed_resolves_inside_the_profile(self):
        """Preserving a link is only correct if it still means the same thing afterwards.

        `.is_symlink()` cannot establish that: it is true whether the link resolves into
        the finished profile or back at the original seed, so an assertion built on it
        alone passes in the correct world and the broken one alike. Where the link
        *points* is the actual claim, so that is what this asserts.
        """
        source = self._source()
        (source / "inner").symlink_to(Path("sub") / "a.txt")

        self.assertTrue(seed.materialize(profile(self.dest, SeedPath(source))))
        link = self.dest / "inner"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), (self.dest / "sub" / "a.txt").resolve())

    def test_an_absolute_link_inside_the_seed_is_refused(self):
        """`copytree` recreates a link from its raw target and never rewrites it for the
        new root, so an absolute link survives the copy still naming the original seed.
        The finished profile would then be live-linked back to the directory it was
        supposed to be an isolated copy of, and Chrome would write through it into the
        real one — the same write-through hazard an escaping link carries, reached by a
        link that points *inside* the seed."""
        source = self._source()
        (source / "inner").symlink_to(source / "sub" / "a.txt")  # absolute by construction

        with self.assertRaisesRegex(CromError, "absolute symlink") as caught:
            seed.materialize(profile(self.dest, SeedPath(source)))

        self.assertIs(caught.exception.reason, Reason.SEED_UNSAFE)
        self.assertFalse(self.dest.exists())

    def test_a_symlinked_directory_cycle_does_not_hang_the_check(self):
        """The walk must not follow links, or a cycle spins forever."""
        source = self._source()
        (source / "loop").symlink_to(Path("."))

        # `loop` resolves to the seed root itself, which counts as inside.
        self.assertTrue(seed.materialize(profile(self.dest, SeedPath(source))))

    def test_a_second_materialize_of_the_same_profile_is_a_no_op(self):
        """The steady state: an existing directory is never re-seeded.

        Sequential on purpose — this covers the plain existence check, not the lock. The
        race is covered by `ConcurrentMaterializeTest` below, which fails without it.
        """
        source = self._source()
        self.assertTrue(seed.materialize(profile(self.dest, SeedPath(source))))
        self.assertFalse(seed.materialize(profile(self.dest, SeedPath(source))))


class ConcurrentMaterializeTest(unittest.TestCase):
    """Two `crom up` calls racing on a profile that does not exist yet.

    Unlocked, both see no directory, both build a full staging copy, and the loser's
    commit hits ENOTEMPTY on the winner's finished profile — leaking its staging
    directory, since that raise used to happen outside the guarded block.

    The threads take separate descriptors, so `fcntl.flock` serializes them exactly as
    it would two processes; the copy is slowed to force the interleaving. Verified to
    fail with `exclusive()` stubbed out.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.dest = self.root / "profiles" / "myapp" / "dev"
        self.source = self.root / "template"
        (self.source / "sub").mkdir(parents=True)
        (self.source / "sub" / "a.txt").write_text("a")

    def test_exactly_one_caller_seeds_and_nothing_is_left_behind(self):
        real_copytree = shutil.copytree

        def slow_copytree(*args, **kwargs):
            time.sleep(0.05)
            return real_copytree(*args, **kwargs)

        results, errors = [], []

        def go():
            try:
                results.append(seed.materialize(profile(self.dest, SeedPath(self.source))))
            except BaseException as e:
                errors.append(e)

        with mock.patch.object(seed.shutil, "copytree", slow_copytree):
            threads = [threading.Thread(target=go) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [False, True])  # one seeded, one found it done
        self.assertTrue((self.dest / "sub" / "a.txt").is_file())
        self.assertEqual([p for p in self.dest.parent.iterdir() if p.is_dir()], [self.dest])


class ChromeUserDataDirTest(unittest.TestCase):
    """Which real browser directory a `chrome` seed copies from.

    `browser._CANDIDATES` treats Chromium as first-class on both platforms, so a table
    here naming only Google Chrome meant `find_chrome()` could succeed on a Chromium-only
    machine while the first command failed — `_bootstrap_user_config` seeds `user/default`
    with `SeedChrome()` unconditionally, so a fresh install could not run once.
    """

    def setUp(self):
        self.home = Path(tempfile.mkdtemp()).resolve()
        self.chrome = self.home / "chrome-data"
        self.chromium = self.home / "chromium-data"
        self.candidates = (self.chrome, self.chromium)
        patch = mock.patch.dict(seed._CHROME_USER_DATA, {sys.platform: self.candidates})
        patch.start()
        self.addCleanup(patch.stop)

    def test_google_chrome_wins_when_both_are_installed(self):
        self.chrome.mkdir()
        self.chromium.mkdir()
        self.assertEqual(seed.chrome_user_data_dir(), self.chrome)

    def test_chromium_is_used_when_it_is_the_only_one_installed(self):
        self.chromium.mkdir()
        self.assertEqual(seed.chrome_user_data_dir(), self.chromium)

    def test_with_neither_installed_it_names_a_real_path_to_report(self):
        """The caller's "seed 'chrome' does not exist: …" has to name something."""
        self.assertEqual(seed.chrome_user_data_dir(), self.chrome)


class LiveSeedTest(unittest.TestCase):
    """A seed is copied only while nothing is writing it.

    A user-data-dir holds SQLite databases the browser writes continuously, and a
    recursive copy has no transaction: it can take a database mid-write, or a table
    without the journal that makes sense of it. The copy reports success either way and
    the damage surfaces weeks later, which is why this is refused rather than mitigated.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.dest = self.root / "profiles" / "myapp" / "dev"
        self.source = self.root / "user-data"
        (self.source / "Default").mkdir(parents=True)
        (self.source / "Default" / "Cookies").write_text("sqlite")

    def hold(self, directory: Path) -> None:
        """Leave behind exactly what a running Chrome leaves: `<hostname>-<our pid>`."""
        os.symlink(
            f"{socket.gethostname()}-{os.getpid()}", directory / chrome.SINGLETON_LOCK
        )

    def test_a_path_seed_whose_browser_is_running_is_refused(self):
        self.hold(self.source)

        with self.assertRaises(CromError) as caught:
            seed.materialize(profile(self.dest, SeedPath(self.source)))

        self.assertIn("is in use", str(caught.exception))
        self.assertIn(str(self.source), str(caught.exception))
        # The only one of the four a caller can act on and retry: quit the browser
        # and the same command works. The other three need the seed itself changed.
        self.assertIs(caught.exception.reason, Reason.SEED_BUSY)

    def test_the_refusal_names_both_ways_out(self):
        """The message is the product: this is the first thing a new user can hit.

        `_bootstrap_user_config` seeds `user/default` from the real Chrome, so the very
        first `crom up` on a machine with the browser open lands here — and a refusal
        that does not say what to do next is only half the fix.
        """
        self.hold(self.source)

        with self.assertRaises(CromError) as caught:
            seed.materialize(profile(self.dest, SeedPath(self.source)))

        message = str(caught.exception)
        self.assertIn("Quit that browser", message)
        self.assertIn('seed = "fresh"', message)

    def test_a_refused_seed_leaves_no_profile_behind(self):
        """Refusing has to be as clean as failing: the directory's existence is the flag.

        A stump here would be read as "already seeded" by the next `crom up`, which would
        then launch Chrome on an empty profile without a word — the loud failure becoming
        a silent one on the very next run.
        """
        self.hold(self.source)

        with self.assertRaises(CromError):
            seed.materialize(profile(self.dest, SeedPath(self.source)))

        self.assertFalse(self.dest.exists())
        self.assertEqual([p for p in self.dest.parent.iterdir() if p.is_dir()], [])

    def test_a_browser_opened_during_the_copy_is_caught_and_the_copy_discarded(self):
        """One check before the copy would only prove the browser was shut a moment ago.

        Chrome takes its singleton at startup and holds it for the session, so a browser
        opened while `copytree` was still walking leaves the lock behind for the second
        read to find.
        """
        real_copytree = shutil.copytree

        # `copytree` recurses into itself for subdirectories, and patching the module
        # attribute catches those inner calls too — hence `*args`, and hence the latch:
        # a browser starts once, on whichever directory the walk reaches first.
        started = []

        def opens_chrome_midway(*args, **kwargs):
            if not started:
                started.append(self.hold(self.source))
            return real_copytree(*args, **kwargs)

        with mock.patch.object(seed.shutil, "copytree", side_effect=opens_chrome_midway):
            with self.assertRaises(CromError) as caught:
                seed.materialize(profile(self.dest, SeedPath(self.source)))

        self.assertIn("while crom was copying", str(caught.exception))
        self.assertFalse(self.dest.exists())
        self.assertEqual([p for p in self.dest.parent.iterdir() if p.is_dir()], [])

    def test_a_chrome_seed_reads_the_lock_from_the_user_data_dir_not_the_profile(self):
        """The singleton sits at the user-data-dir root, one level above what is copied.

        A `chrome` seed copies `<user-data-dir>/<profile>`, so looking for the lock
        beside the copy source would find nothing and copy a live profile every time.
        """
        self.hold(self.source)
        with mock.patch.dict(seed._CHROME_USER_DATA, {sys.platform: (self.source,)}):
            with self.assertRaises(CromError) as caught:
                seed.materialize(profile(self.dest, SeedChrome(profile="Default")))

        self.assertIn("is in use", str(caught.exception))

    def test_a_stale_lock_from_a_crashed_browser_does_not_block_seeding(self):
        """Nothing but the next Chrome ever removes that file — refusing would be forever."""
        os.symlink(f"{socket.gethostname()}-{_dead_pid()}", self.source / chrome.SINGLETON_LOCK)

        self.assertTrue(seed.materialize(profile(self.dest, SeedPath(self.source))))
        self.assertTrue((self.dest / "Default" / "Cookies").is_file())

    def test_a_seed_that_is_a_file_is_reported_as_missing_not_as_in_use(self):
        """The stillness check runs first, so it is the one that must not misread a file.

        `readlink` on `<file>/SingletonLock` raises ENOTDIR, and reading that as held
        would answer a question the operator did not ask — telling them to quit a
        browser instead of that the path they named is not a user-data-dir.
        """
        file = self.root / "a-file"
        file.write_text("")

        with self.assertRaises(CromError) as caught:
            seed.materialize(profile(self.dest, SeedPath(file)))

        self.assertIn("does not exist", str(caught.exception))
        self.assertNotIn("Quit that browser", str(caught.exception))
        self.assertIs(caught.exception.reason, Reason.SEED_MISSING)

    def test_a_seed_below_a_running_user_data_dir_is_refused(self):
        """Chrome's one lock sits at the root and governs everything under it.

        `config` accepts a path seed naming a profile inside a user-data-dir, where there
        is no lock to find — so checking only the named directory would copy `Default`
        out from under the browser writing it and report success.
        """
        inner = self.source / "Default" / "Extensions"
        inner.mkdir(parents=True)
        self.hold(self.source)

        with self.assertRaises(CromError) as caught:
            seed.materialize(profile(self.dest, SeedPath(inner)))

        self.assertIn("is in use", str(caught.exception))
        # The refusal names the directory actually held, not the one the seed named.
        self.assertIn(str(self.source), str(caught.exception))

    @unittest.skipIf(os.geteuid() == 0, "root traverses any directory, so nothing is denied")
    def test_an_unreachable_seed_names_the_seed_rather_than_a_browser(self):
        """The walk reads directories the user never named, so its errors must not lie.

        An unsearchable ancestor makes every read in the walk fail; reporting that as a
        running browser would send the user to quit one, naming a directory they may not
        recognise, for what is a permissions problem on the path they asked for.
        """
        blocked = self.root / "blocked"
        (blocked / "seed").mkdir(parents=True)
        self.addCleanup(os.chmod, blocked, 0o755)
        os.chmod(blocked, 0o000)

        with self.assertRaises(CromError) as caught:
            seed.materialize(profile(self.dest, SeedPath(blocked / "seed")))

        self.assertNotIn("browser", str(caught.exception))
        self.assertIn("cannot be read", str(caught.exception))
        self.assertIn(str(blocked / "seed"), str(caught.exception))
        self.assertIs(caught.exception.reason, Reason.SEED_UNREADABLE)

    def test_a_seed_path_through_a_regular_file_is_reported_as_missing(self):
        """ENOTDIR says the path is not there, not that crom was refused it.

        A typo routing the seed through a file — `.../notes.txt/Default` — is a path
        mistake, and calling it unreadable sends the user after a permissions fix.
        """
        file = self.root / "a-file"
        file.write_text("")

        with self.assertRaises(CromError) as caught:
            seed.materialize(profile(self.dest, SeedPath(file / "Default")))

        self.assertIn("does not exist", str(caught.exception))
        self.assertNotIn("cannot be read", str(caught.exception))

    def test_a_refusal_never_carries_an_escape_sequence_out_of_a_directory_name(self):
        """Every name in this sentence is foreign text; a POSIX name is any bytes but NUL.

        A `.crom.toml` may arrive with a cloned repository, so the tree it points at is
        the attacker's: they name a directory with an escape sequence and plant the lock
        beside it themselves, and the refusal repaints the terminal it prints on.
        """
        spoofed = self.root / "\x1b[2Jpwned"
        (spoofed / "Default").mkdir(parents=True)
        self.hold(spoofed)

        with self.assertRaises(CromError) as caught:
            seed.materialize(profile(self.dest, SeedPath(spoofed / "Default")))

        self.assertNotIn("\x1b", str(caught.exception))

    def test_a_missing_seed_never_carries_an_escape_sequence_out_of_its_name(self):
        """Being absent is the cheapest way to reach a message: the name is the whole attack.

        No lock to plant and no permissions to arrange — a `.crom.toml` naming a directory
        that was never there reaches this sentence, and every seed that is simply typo'd
        arrives the same way.
        """
        spoofed = self.root / "\x1b[2Jpwned"

        with self.assertRaises(CromError) as caught:
            seed.materialize(profile(self.dest, SeedPath(spoofed / "Default")))

        self.assertIn("does not exist", str(caught.exception))
        self.assertNotIn("\x1b", str(caught.exception))

    @unittest.skipIf(os.geteuid() == 0, "root traverses any directory, so nothing is denied")
    def test_an_unreadable_seed_never_carries_an_escape_sequence_out_of_its_name(self):
        """An unsearchable ancestor is what reaches this arm: every other way is read as held.

        `singleton_holder` answers *held* for a `SingletonLock` it cannot read at all, so a
        symlink loop or a sick disk is refused before the copy begins and speaks through
        `_refuse`. Only the errors it reads as free — a permissions wall — arrive here.
        """
        blocked = self.root / "blocked"
        spoofed = blocked / "\x1b[2Jpwned"
        spoofed.mkdir(parents=True)
        self.addCleanup(os.chmod, blocked, 0o755)
        os.chmod(blocked, 0o000)

        with self.assertRaises(CromError) as caught:
            seed.materialize(profile(self.dest, SeedPath(spoofed)))

        self.assertIn("cannot be read", str(caught.exception))
        self.assertNotIn("\x1b", str(caught.exception))

    def test_a_fresh_seed_is_unaffected_by_any_running_browser(self):
        """`fresh` reads no directory at all, so there is nothing that could be moving."""
        self.hold(self.source)

        self.assertTrue(seed.materialize(profile(self.dest, SeedFresh())))


if __name__ == "__main__":
    unittest.main()
