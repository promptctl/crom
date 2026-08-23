"""Tests for profile materialization — that a profile directory appears whole or not at all.

`materialize` reads the directory's existence as "already seeded", so these tests are
mostly about the failure path: a copy that dies partway must not leave a stump behind
for the next run to mistake for a finished profile.
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crom import seed
from crom.model import CromError, ProfileRef, ResolvedProfile, SeedChrome, SeedFresh, SeedPath


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

    def test_a_symlink_in_a_seed_is_copied_as_a_link_not_as_its_target(self):
        """A seed must not be able to pull in a file it only points at.

        Dereferencing would copy the *content* of whatever the link names — an
        `~/.ssh/id_rsa` link in a seed checked into a repo would land as the real key
        inside a profile whose CDP port is reachable by local tooling.
        """
        source = self._source()
        secret = self.root / "secret.txt"
        secret.write_text("private key")
        (source / "link").symlink_to(secret)

        self.assertTrue(seed.materialize(profile(self.dest, SeedPath(source))))
        self.assertTrue((self.dest / "link").is_symlink())
        self.assertEqual((self.dest / "link").readlink(), secret)

    def test_a_second_materialize_of_the_same_profile_is_a_no_op(self):
        """The existence check and the copy are one critical section.

        Serialized, the second caller sees a finished directory and reports False, which
        is what `crom up`'s advertised idempotence means.
        """
        source = self._source()
        self.assertTrue(seed.materialize(profile(self.dest, SeedPath(source))))
        self.assertFalse(seed.materialize(profile(self.dest, SeedPath(source))))


if __name__ == "__main__":
    unittest.main()
