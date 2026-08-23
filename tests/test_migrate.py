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

    def test_migrated_profiles_resolve_to_their_original_ports(self):
        self.run_migration()
        from crom import resolve

        scope = config.load_user_scope()
        profile = resolve.resolve(ProfileRef("user", "default"), scope)
        self.assertEqual(profile.port, 4222)
        self.assertEqual(profile.profile_dir, state_home() / "profiles" / "user" / "default")


if __name__ == "__main__":
    unittest.main()
