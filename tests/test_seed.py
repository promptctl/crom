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

    def test_a_copy_that_fails_partway_leaves_no_profile_to_mistake_for_a_finished_one(self):
        # A dangling symlink is what a real user-data-dir's `SingletonSocket` looks like
        # once the browser that owned it is gone, and it is enough to abort copytree.
        source = self.root / "template"
        (source / "sub").mkdir(parents=True)
        (source / "sub" / "a.txt").write_text("a")
        (source / "SingletonSocket").symlink_to("/nonexistent/socket")

        with self.assertRaises(Exception):
            seed.materialize(profile(self.dest, SeedPath(source)))

        # The whole point: without an atomic commit this directory survives half-built,
        # and the *next* crom up launches Chrome on it without a word.
        self.assertFalse(self.dest.exists())

    def test_the_run_after_a_failed_copy_can_still_seed_the_profile(self):
        source = self.root / "template"
        (source / "sub").mkdir(parents=True)
        (source / "SingletonSocket").symlink_to("/nonexistent/socket")

        with self.assertRaises(Exception):
            seed.materialize(profile(self.dest, SeedPath(source)))

        (source / "SingletonSocket").unlink()
        self.assertTrue(seed.materialize(profile(self.dest, SeedPath(source))))
        self.assertTrue((self.dest / "sub").is_dir())

    def test_staging_leaves_no_debris_beside_the_profile(self):
        source = self.root / "template"
        (source / "sub").mkdir(parents=True)
        (source / "SingletonSocket").symlink_to("/nonexistent/socket")

        with self.assertRaises(Exception):
            seed.materialize(profile(self.dest, SeedPath(source)))

        self.assertEqual(list(self.dest.parent.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
