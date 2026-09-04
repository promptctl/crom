"""Tests for the one-time move from the flat layout into the `user` namespace."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crom import config, migrate, registry
from crom.model import CromError, ProfileRef, Reason
from crom.paths import state_home

LEGACY = {"profiles": {"default": {"port": 4222}, "worker1": {"port": 4223}}}


def setUpModule():
    """Keep migration's tests about migration.

    Migration declares profiles without a `chrome_binary`, so every `load_user_scope()`
    below falls through to a live `find_chrome()` — making these tests depend on Chrome
    being installed on the machine running them, and fail with "no Chrome executable
    found" on a CI runner that has none. `test_config.py` already stubs this for the
    same reason; the fix belonged to the condition, not to that one file.
    """
    global _chrome_stub
    _chrome_stub = mock.patch.object(config, "find_chrome", return_value=Path("/stub/chrome"))
    _chrome_stub.start()


def tearDownModule():
    _chrome_stub.stop()


class MigrateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        # `HOME` is redirected as well as the XDG variables, because migration locates
        # the legacy data through `Path.home()` — exactly as the module that wrote it
        # did. The two are set to *different* places on purpose: that is the real
        # user's situation this module has to survive, and the fixture would not
        # otherwise distinguish a correct lookup from one that happens to coincide.
        self.env = mock.patch.dict(
            os.environ,
            {
                "HOME": str(root / "home"),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_STATE_HOME": str(root / "state"),
            },
        )
        self.env.start()

        migrate.legacy_registry_file().parent.mkdir(parents=True)
        migrate.legacy_registry_file().write_text(json.dumps(LEGACY))
        for name in LEGACY["profiles"]:
            (migrate._legacy_state_dir() / name).mkdir(parents=True)
            (migrate._legacy_state_dir() / name / "Default").mkdir()
            (migrate._legacy_state_dir() / f"{name}.pid").write_text("123")

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
        self.assertFalse((migrate._legacy_state_dir() / "worker1").exists())

    def test_stale_pid_files_are_swept(self):
        self.run_migration()
        self.assertFalse((migrate._legacy_state_dir() / "worker1.pid").exists())

    def test_the_old_registry_is_kept_as_a_backup_not_deleted(self):
        self.run_migration()
        self.assertFalse(migrate.legacy_registry_file().exists())
        self.assertTrue(migrate.legacy_registry_file().with_suffix(".json.migrated").exists())

    def test_it_runs_once_and_then_reports_nothing_to_do(self):
        self.run_migration()
        self.assertFalse(migrate.needed())

    def test_it_refuses_to_move_a_directory_out_from_under_a_running_chrome(self):
        live = {str(migrate._legacy_state_dir() / "worker1"): (4242,)}
        with mock.patch("crom.chrome.scan", return_value=live):
            with self.assertRaisesRegex(CromError, "worker1 .pid 4242.") as caught:
                migrate.run(log=lambda _: None)
        # The retryable half of the pair: quit the browsers and the same command
        # goes through. Both halves are exit 1 and `kind: failure`, and both read as
        # "migration refused", so nothing but the slug tells a caller whether trying
        # again is worth anything.
        self.assertIs(caught.exception.reason, Reason.MIGRATION_NEEDS_QUIET)
        # Nothing moved, so the running browser is still exactly where crom left it.
        self.assertTrue((migrate._legacy_state_dir() / "worker1").is_dir())
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
        (migrate._legacy_state_dir() / "ci").mkdir(parents=True, exist_ok=True)

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
            with self.assertRaisesRegex(CromError, "QA env") as caught:
                migrate.run(log=lambda _: None)

        # The other half: nothing changes until a human edits the legacy file, so a
        # caller that retries on this one loops forever.
        self.assertIs(caught.exception.reason, Reason.MIGRATION_BLOCKED)

        # Refused before the first write: the legacy file is intact, so the user can
        # rename and try again.
        self.assertTrue(migrate.legacy_registry_file().exists())
        self.assertEqual(config.load_user_scope().profiles, {})

    def test_the_legacy_registry_is_found_even_when_xdg_points_elsewhere(self):
        """The legacy data is at the path the *old* crom wrote it to, not the new one.

        `crom/profiles.py` hardcoded `~/.config/crom` and `~/.local/state/crom` and never
        read `XDG_CONFIG_HOME`/`XDG_STATE_HOME`. The namespaced layout's `config_home()`
        honors them, so locating the legacy registry through it would search a directory
        the legacy writer never used. For a user who has those variables set — common
        enough in a dotfiles setup — `needed()` would be False, crom would bootstrap as
        if fresh, and their profiles would be silently abandoned with new ports.

        The fixture points HOME and the XDG variables at different directories precisely
        so this distinction is observable; with them coincident the bug is invisible.
        """
        self.assertTrue(migrate.needed())
        self.assertTrue(str(migrate.legacy_registry_file()).startswith(str(Path.home())))
        self.assertNotIn(os.environ["XDG_CONFIG_HOME"], str(migrate.legacy_registry_file()))

        self.run_migration()

        # And the data actually moved, rather than the run being a no-op that reported
        # success over an empty legacy set.
        ports = registry.reservations()
        self.assertEqual(ports["user/default"].port, 4222)
        self.assertEqual(ports["user/worker1"].port, 4223)

    def test_a_malformed_legacy_entry_is_refused_rather_than_crashing(self):
        """`entry.get("port")` assumes a dict. A hand-edited registry whose entry is a
        string or null raised `AttributeError` — not a `CromError`, so it escaped the
        exit-code contract as a traceback. And because migration reruns at the top of
        every command until it succeeds, that traceback became the only thing crom could
        do, leaving no command to repair it with."""
        for broken in ("a string", None, ["a", "list"], 42):
            with self.subTest(entry=broken):
                self._rewrite_legacy({"default": {"port": 4222}, "ci": broken})
                with mock.patch("crom.chrome.scan", return_value={}):
                    with self.assertRaisesRegex(CromError, "entry for 'ci'"):
                        migrate.run(log=lambda _: None)
                # Refused before the first write, so the user can still repair and retry.
                self.assertTrue(migrate.legacy_registry_file().exists())

    def test_a_legacy_registry_that_is_not_an_object_is_refused(self):
        migrate.legacy_registry_file().write_text(json.dumps(["not", "an", "object"]))
        with mock.patch("crom.chrome.scan", return_value={}):
            with self.assertRaisesRegex(CromError, "is a JSON list, not an object"):
                migrate.run(log=lambda _: None)

    def test_liveness_is_answered_with_one_ps_for_every_profile(self):
        """`scan()` documents one `ps` for every profile at once, and `list_cmd` uses it
        that way. Asking `find_pids_for_dir` per profile re-ran `scan()` — and so spawned
        a subprocess — once per legacy profile, which is the cost the design exists to
        avoid, paid at exactly the moment the user is upgrading."""
        with mock.patch("crom.chrome.scan", return_value={}) as scan:
            migrate.run(log=lambda _: None)
        self.assertEqual(scan.call_count, 1)

    def test_a_corrupt_legacy_registry_is_reported_not_crashed_through(self):
        """A raw JSONDecodeError is not a CromError, so it escapes the exit-code contract
        — and migration runs before every command until it succeeds, so that traceback
        would be the only thing crom could do."""
        migrate.legacy_registry_file().write_text("{ truncated")
        with mock.patch("crom.chrome.scan", return_value={}):
            with self.assertRaisesRegex(CromError, "not valid JSON") as caught:
                migrate.run(log=lambda _: None)
        self.assertIs(caught.exception.reason, Reason.REGISTRY_INVALID)

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


    def test_a_commit_interrupted_after_the_move_is_finished_by_the_next_run(self):
        """The window between `shutil.move` completing and `os.replace` committing.

        On one filesystem the move is a rename, so a kill here leaves the data whole in
        staging with `old_dir` already gone. The retry saw no `old_dir`, decided there
        was nothing to do, and left a complete profile in a hidden directory nothing ever
        looked at again — and since these profiles are declared `seed = "default"`, the
        next `crom up` rebuilt them from the real Chrome profile instead of the user's
        data. Silent substitution of someone else's browser state for your own.

        Rather than simulate the kill, this constructs exactly the state one leaves.
        """
        destination_root = state_home() / "profiles" / "user"
        destination_root.mkdir(parents=True)
        staging = destination_root / ".worker1.partial"
        shutil.move(str(migrate._legacy_state_dir() / "worker1"), str(staging))
        (staging / "Default" / "marker").write_text("the user's real data")

        self.run_migration()

        moved = destination_root / "worker1" / "Default" / "marker"
        self.assertTrue(moved.is_file(), "the staged profile was never committed")
        self.assertEqual(moved.read_text(), "the user's real data")
        self.assertFalse(staging.exists())

    def test_staging_beside_a_committed_destination_is_discarded(self):
        """The other reading of the same directory: once `destination` exists the move is
        done — `os.replace` is atomic and is the only thing that creates it — so staging
        really is debris and must not be mistaken for work outstanding."""
        destination_root = state_home() / "profiles" / "user"
        (destination_root / "worker1" / "Default").mkdir(parents=True)
        (destination_root / "worker1" / "Default" / "marker").write_text("committed")
        staging = destination_root / ".worker1.partial"
        (staging / "Default").mkdir(parents=True)
        (staging / "Default" / "marker").write_text("stale debris")

        self.run_migration()

        keeper = destination_root / "worker1" / "Default" / "marker"
        self.assertEqual(keeper.read_text(), "committed")
        self.assertFalse(staging.exists())

    def test_two_legacy_profiles_claiming_one_port_are_refused_before_any_write(self):
        """`adopt` refuses the second claimant, but from inside the loop — after earlier
        profiles are declared and adopted, at the same point on every retry."""
        self._rewrite_legacy({"default": {"port": 4222}, "worker1": {"port": 4222}})

        with mock.patch("crom.chrome.scan", return_value={}):
            with self.assertRaisesRegex(CromError, "both claim port 4222"):
                migrate.run(log=lambda _: None)

        self.assertTrue(migrate.legacy_registry_file().exists())
        self.assertEqual(config.load_user_scope().profiles, {})
        self.assertEqual(registry.reservations(), {})

    def test_a_legacy_port_already_held_by_another_profile_is_refused_before_any_write(self):
        """The other source of collision, and not an exotic one.

        `_allocate` hands out the lowest free port at or above `BASE_PORT` and the legacy
        layout numbered from the same base, so a project that ran `crom add` before the
        upgrade holds exactly the ports a legacy profile is likely to have pinned.
        Without the pre-flight, `adopt` refuses from inside the loop after earlier
        profiles are declared — and since the legacy registry is retired only on full
        success, every later crom command re-enters migration and dies at the same point.
        """
        registry.adopt(ProfileRef("myapp", "dev"), 4222, None)
        self._rewrite_legacy({"default": {"port": 4222}})

        with mock.patch("crom.chrome.scan", return_value={}):
            with self.assertRaisesRegex(CromError, "already reserved by 'myapp/dev'"):
                migrate.run(log=lambda _: None)

        # Refused before the first write: nothing declared, nothing adopted, and the
        # legacy registry still there for the retry.
        self.assertTrue(migrate.legacy_registry_file().exists())
        self.assertEqual(config.load_user_scope().profiles, {})
        self.assertEqual(set(registry.reservations()), {"myapp/dev"})

    def test_a_port_the_profile_itself_already_adopted_is_not_a_foreign_claim(self):
        """A resumed migration re-enters with its own earlier adoptions in the ledger.
        Refusing those would turn the recovery path into the brick it exists to prevent —
        the check must exclude the profile's own ref, not merely the port."""
        registry.adopt(ProfileRef("user", "default"), 4222, None)
        self._rewrite_legacy({"default": {"port": 4222}})

        with mock.patch("crom.chrome.scan", return_value={}):
            migrate.run(log=lambda _: None)

        self.assertEqual(registry.reservations()["user/default"].port, 4222)
        self.assertFalse(migrate.legacy_registry_file().exists())

    def test_a_malformed_legacy_port_is_refused_before_any_write(self):
        """`adopt` checks who may hold a number, not whether it is one. An unvalidated
        value reached the new ledger and surfaced much later, from `socket.bind`."""
        for bad in ("9222", 0, -1, 99999, 3.5, True):
            with self.subTest(port=bad):
                self._rewrite_legacy({"default": {"port": 4222}, "ci": {"port": bad}})
                with mock.patch("crom.chrome.scan", return_value={}):
                    with self.assertRaisesRegex(CromError, "bad port for 'ci'"):
                        migrate.run(log=lambda _: None)
                self.assertTrue(migrate.legacy_registry_file().exists())

    def test_a_legacy_profile_named_for_the_profiles_root_is_refused(self):
        """With XDG_STATE_HOME unset — the ordinary case — the legacy state directory and
        the new one are the same, which is deliberate and harmless for every name but
        this one. A profile called `profiles` sat exactly where the namespaced layout
        keeps all profiles, so the move became "put this directory inside itself" and
        `shutil` raised a bare `shutil.Error` partway through the loop.
        """
        self.env.stop()  # the collision needs HOME and XDG_STATE_HOME to coincide
        home = Path(self.tmp.name).resolve() / "collide"
        env = mock.patch.dict(os.environ, {"HOME": str(home)})
        for key in ("XDG_STATE_HOME", "XDG_CONFIG_HOME"):
            os.environ.pop(key, None)
        env.start()
        self.addCleanup(env.stop)

        migrate.legacy_registry_file().parent.mkdir(parents=True)
        self._rewrite_legacy({"profiles": {"port": 4222}})
        (migrate._legacy_state_dir() / "profiles" / "Default").mkdir(parents=True)

        with mock.patch("crom.chrome.scan", return_value={}):
            with self.assertRaisesRegex(CromError, "inside itself"):
                migrate.run(log=lambda _: None)

        self.assertTrue(migrate.legacy_registry_file().exists())


if __name__ == "__main__":
    unittest.main()
