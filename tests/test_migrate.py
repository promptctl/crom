"""Tests for the one-time move from the flat layout into the `user` namespace."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crom import config, migrate, registry
from crom.model import CromError, ProfileRef
from crom.paths import state_home

LEGACY = {"profiles": {"default": {"port": 4222}, "worker1": {"port": 4223}}}


class MigrateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        self.env = mock.patch.dict(
            os.environ,
            {"XDG_CONFIG_HOME": str(root / "config"), "XDG_STATE_HOME": str(root / "state")},
        )
        self.env.start()

        migrate.legacy_registry_file().parent.mkdir(parents=True)
        migrate.legacy_registry_file().write_text(json.dumps(LEGACY))
        for name in LEGACY["profiles"]:
            (state_home() / name).mkdir(parents=True)
            (state_home() / name / "Default").mkdir()
            (state_home() / f"{name}.pid").write_text("123")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_migration(self):
        with mock.patch("crom.chrome.scan", return_value={}):
            migrate.run(log=lambda _: None)

    def test_profiles_keep_their_existing_ports(self):
        self.run_migration()
        ports = registry.reservations()
        self.assertEqual(ports["user/default"].port, 4222)
        self.assertEqual(ports["user/worker1"].port, 4223)

    def test_profiles_become_declarations_in_the_user_config(self):
        self.run_migration()
        scope = config.load_user_scope()
        self.assertEqual(sorted(scope.profiles), ["default", "worker1"])

    def test_profile_data_moves_under_the_namespaced_layout(self):
        self.run_migration()
        moved = state_home() / "profiles" / "user" / "worker1" / "Default"
        self.assertTrue(moved.is_dir())
        self.assertFalse((state_home() / "worker1").exists())

    def test_stale_pid_files_are_swept(self):
        self.run_migration()
        self.assertFalse((state_home() / "worker1.pid").exists())

    def test_the_old_registry_is_kept_as_a_backup_not_deleted(self):
        self.run_migration()
        self.assertFalse(migrate.legacy_registry_file().exists())
        self.assertTrue(migrate.legacy_registry_file().with_suffix(".json.migrated").exists())

    def test_it_runs_once_and_then_reports_nothing_to_do(self):
        self.run_migration()
        self.assertFalse(migrate.needed())

    def test_it_refuses_to_move_a_directory_out_from_under_a_running_chrome(self):
        live = {str(state_home() / "worker1"): (4242,)}
        with mock.patch("crom.chrome.scan", return_value=live):
            with self.assertRaisesRegex(CromError, "worker1 .pid 4242."):
                migrate.run(log=lambda _: None)
        # Nothing moved, so the running browser is still exactly where crom left it.
        self.assertTrue((state_home() / "worker1").is_dir())
        self.assertTrue(migrate.legacy_registry_file().exists())

    def test_an_attempt_that_dies_partway_is_resumed_by_the_next_one(self):
        # The legacy registry is only retired after the whole loop, so a failed attempt
        # is retried on the user's next command — and migration runs before anything
        # else in `main`. A step that could not survive being run twice would therefore
        # raise on every command from then on, with no command left to recover with.
        real_move = migrate.shutil.move

        def fail_on_worker1(source, destination):
            if "worker1" in str(source):
                raise OSError("simulated: no space left on device")
            return real_move(source, destination)

        with mock.patch("crom.chrome.scan", return_value={}):
            with mock.patch.object(migrate.shutil, "move", fail_on_worker1):
                with self.assertRaises(OSError):
                    migrate.run(log=lambda _: None)

            # Half done: `default` moved and both names are already declared.
            self.assertTrue(migrate.needed())
            migrate.run(log=lambda _: None)

        self.assertFalse(migrate.needed())
        ports = registry.reservations()
        self.assertEqual(ports["user/default"].port, 4222)
        self.assertEqual(ports["user/worker1"].port, 4223)
        for name in LEGACY["profiles"]:
            self.assertTrue((state_home() / "profiles" / "user" / name / "Default").is_dir())

    def test_a_resumed_attempt_declares_each_profile_exactly_once(self):
        real_move = migrate.shutil.move

        def fail_on_worker1(source, destination):
            if "worker1" in str(source):
                raise OSError("simulated: no space left on device")
            return real_move(source, destination)

        with mock.patch("crom.chrome.scan", return_value={}):
            with mock.patch.object(migrate.shutil, "move", fail_on_worker1):
                with self.assertRaises(OSError):
                    migrate.run(log=lambda _: None)
            migrate.run(log=lambda _: None)

        scope = config.load_user_scope()
        self.assertEqual(sorted(scope.profiles), ["default", "worker1"])

    def _rewrite_legacy(self, profiles: dict) -> None:
        migrate.legacy_registry_file().write_text(json.dumps({"profiles": profiles}))

    def test_a_profile_that_was_never_launched_gets_a_port_instead_of_crashing(self):
        """The old registry created entries as `{}` and added "port" only on first
        launch, so `crom add ci` without ever bringing it up left a portless entry.
        Indexing it raised KeyError — not a CromError — and since migration reruns at the
        top of every command, that bricked the CLI with no way back."""
        self._rewrite_legacy({"default": {"port": 4222}, "ci": {}})
        (state_home() / "ci").mkdir(parents=True, exist_ok=True)

        self.run_migration()

        ports = registry.reservations()
        self.assertEqual(ports["user/default"].port, 4222)
        self.assertIn("user/ci", ports)  # assigned now, since there was nothing to keep
        self.assertGreater(ports["user/ci"].port, 0)

    def test_an_illegal_legacy_name_is_refused_before_anything_is_written(self):
        """The old scheme never validated names, so `QA env` or `Default` were possible;
        the new parser rejects them. Writing one into the generated TOML would make every
        later command fail to load it — and a successful run retires the legacy registry,
        so there would be no retry path."""
        self._rewrite_legacy({"default": {"port": 4222}, "QA env": {"port": 4223}})

        with mock.patch("crom.chrome.scan", return_value={}):
            with self.assertRaisesRegex(CromError, "QA env"):
                migrate.run(log=lambda _: None)

        # Refused before the first write: the legacy file is intact, so the user can
        # rename and try again.
        self.assertTrue(migrate.legacy_registry_file().exists())
        self.assertEqual(config.load_user_scope().profiles, {})

    def test_a_corrupt_legacy_registry_is_reported_not_crashed_through(self):
        """A raw JSONDecodeError is not a CromError, so it escapes the exit-code contract
        — and migration runs before every command until it succeeds, so that traceback
        would be the only thing crom could do."""
        migrate.legacy_registry_file().write_text("{ truncated")
        with mock.patch("crom.chrome.scan", return_value={}):
            with self.assertRaisesRegex(CromError, "not valid JSON"):
                migrate.run(log=lambda _: None)

    def test_a_move_that_dies_partway_leaves_the_retry_a_clean_destination(self):
        """Across filesystems `shutil.move` degrades to copy-then-delete, so a failure
        mid-copy leaves the destination partly populated *and* the source in place. This
        module is built so a failed attempt is resumed by the next command, and that
        retry would re-enter with a non-empty destination.
        """
        real_move = migrate.shutil.move

        def fail_on_worker1(source, destination):
            if "worker1" in str(source):
                # Populate the staging destination first, the way a partial copy would.
                Path(destination).mkdir(parents=True, exist_ok=True)
                (Path(destination) / "half").write_text("x")
                raise OSError("simulated: no space left on device")
            return real_move(source, destination)

        with mock.patch("crom.chrome.scan", return_value={}):
            with mock.patch.object(migrate.shutil, "move", fail_on_worker1):
                with self.assertRaises(OSError):
                    migrate.run(log=lambda _: None)

            destination_root = state_home() / "profiles" / "user"
            self.assertEqual([p.name for p in destination_root.iterdir() if p.is_dir()], ["default"])

            migrate.run(log=lambda _: None)

        self.assertFalse(migrate.needed())
        self.assertTrue((state_home() / "profiles" / "user" / "worker1" / "Default").is_dir())

    def test_migrated_profiles_resolve_to_their_original_ports(self):
        self.run_migration()
        from crom import resolve

        scope = config.load_user_scope()
        profile = resolve.resolve(ProfileRef("user", "default"), scope)
        self.assertEqual(profile.port, 4222)
        self.assertEqual(profile.profile_dir, state_home() / "profiles" / "user" / "default")


if __name__ == "__main__":
    unittest.main()
