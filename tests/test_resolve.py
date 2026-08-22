"""Tests for reference parsing, argv composition, and variable interpolation."""

import os
import tempfile
import unittest
from pathlib import Path

from crom import config, registry, resolve
from crom.model import CromError, ProfileRef, parse_ref
from crom.policy import LAUNCH_POLICY_FLAGS

MINIMAL = 'namespace = "myapp"\n'


class ParseRefTest(unittest.TestCase):
    def test_bare_name_uses_the_ambient_namespace(self):
        self.assertEqual(parse_ref("dev", "myapp"), ProfileRef("myapp", "dev"))

    def test_qualified_name_overrides_the_ambient_namespace(self):
        self.assertEqual(parse_ref("other/dev", "myapp"), ProfileRef("other", "dev"))

    def test_a_third_segment_is_an_error_not_a_guess(self):
        with self.assertRaisesRegex(CromError, "invalid profile reference"):
            parse_ref("a/b/c", "myapp")

    def test_path_traversal_cannot_reach_the_name_type(self):
        # Names become directory components, so `.` and `..` must be unrepresentable.
        # Both segments are checked; whichever is illegal is the one that reports.
        with self.assertRaisesRegex(CromError, "invalid namespace"):
            parse_ref("../escape", "myapp")
        with self.assertRaisesRegex(CromError, "invalid profile name"):
            parse_ref("myapp/..", "myapp")
        with self.assertRaisesRegex(CromError, "invalid profile reference"):
            parse_ref("/etc/passwd", "myapp")


class ArgvTest(unittest.TestCase):
    def test_crom_owned_switches_come_last_so_config_cannot_displace_them(self):
        argv = resolve.build_argv(Path("/chrome"), Path("/data"), 9300, ("--headless=new",))
        self.assertEqual(argv[0], "/chrome")
        self.assertEqual(argv[-2:], ("--user-data-dir=/data", "--remote-debugging-port=9300"))
        self.assertLess(argv.index("--headless=new"), argv.index("--user-data-dir=/data"))

    def test_launch_policy_precedes_configured_flags(self):
        argv = resolve.build_argv(Path("/chrome"), Path("/data"), 9300, ("--headless=new",))
        self.assertLess(argv.index(LAUNCH_POLICY_FLAGS[0]), argv.index("--headless=new"))


class ResolveTest(unittest.TestCase):
    """Resolution touches the port ledger, so each test gets its own state directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self._old_state = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = str(self.root / "state")

    def tearDown(self):
        if self._old_state is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = self._old_state
        self.tmp.cleanup()

    def scope(self, text: str):
        source = self.root / ".crom.toml"
        source.write_text(text)
        return config.parse(text, source)

    def test_profile_dir_is_namespaced(self):
        scope = self.scope(MINIMAL + "[profiles.dev]\n")
        profile = resolve.resolve(ProfileRef("myapp", "dev"), scope)
        self.assertEqual(profile.profile_dir.parts[-2:], ("myapp", "dev"))

    def test_two_namespaces_with_the_same_profile_name_do_not_collide(self):
        one = self.scope('namespace = "one"\n[profiles.dev]\n')
        two_source = self.root / "two.toml"
        two_source.write_text('namespace = "two"\n[profiles.dev]\n')
        two = config.parse(two_source.read_text(), two_source)

        a = resolve.resolve(ProfileRef("one", "dev"), one)
        b = resolve.resolve(ProfileRef("two", "dev"), two)
        self.assertNotEqual(a.port, b.port)
        self.assertNotEqual(a.profile_dir, b.profile_dir)

    def test_port_assignment_is_idempotent(self):
        scope = self.scope(MINIMAL + "[profiles.dev]\n")
        first = resolve.resolve(ProfileRef("myapp", "dev"), scope).port
        second = resolve.resolve(ProfileRef("myapp", "dev"), scope).port
        self.assertEqual(first, second)

    def test_a_pinned_port_is_used_verbatim(self):
        scope = self.scope(MINIMAL + "[profiles.dev]\nport = 9401\n")
        self.assertEqual(resolve.resolve(ProfileRef("myapp", "dev"), scope).port, 9401)

    def test_a_pinned_port_already_held_by_another_profile_is_a_conflict(self):
        first = self.scope(MINIMAL + "[profiles.dev]\nport = 9402\n")
        resolve.resolve(ProfileRef("myapp", "dev"), first)

        other_source = self.root / "other.toml"
        other_source.write_text('namespace = "other"\n[profiles.dev]\nport = 9402\n')
        other = config.parse(other_source.read_text(), other_source)
        with self.assertRaisesRegex(CromError, "already held by profile 'myapp/dev'"):
            resolve.resolve(ProfileRef("other", "dev"), other)

    def test_state_dir_relocates_profiles_relative_to_the_config(self):
        scope = self.scope(MINIMAL + 'state_dir = "./.crom/profiles"\n[profiles.dev]\n')
        profile = resolve.resolve(ProfileRef("myapp", "dev"), scope)
        self.assertTrue(str(profile.profile_dir).startswith(str(self.root / ".crom" / "profiles")))

    def test_variables_expand_in_flags(self):
        scope = self.scope(
            MINIMAL + '[profiles.dev]\nflags = ["--load-extension=${CROM_CONFIG_DIR}/ext"]\n'
        )
        profile = resolve.resolve(ProfileRef("myapp", "dev"), scope)
        self.assertIn(f"--load-extension={self.root}/ext", profile.argv)

    def test_variables_expand_in_env_values(self):
        scope = self.scope(MINIMAL + '[profiles.dev]\nenv = { DEBUG_URL = "${CROM_PORT}" }\n')
        profile = resolve.resolve(ProfileRef("myapp", "dev"), scope)
        self.assertEqual(profile.env["DEBUG_URL"], str(profile.port))

    def test_an_unknown_variable_is_an_error_not_an_empty_string(self):
        scope = self.scope(MINIMAL + '[profiles.dev]\nflags = ["--x=${CROM_NOPE}"]\n')
        with self.assertRaisesRegex(CromError, "unknown variable"):
            resolve.resolve(ProfileRef("myapp", "dev"), scope)

    def test_an_undeclared_profile_names_what_is_declared(self):
        scope = self.scope(MINIMAL + "[profiles.dev]\n[profiles.ci]\n")
        with self.assertRaisesRegex(CromError, "Declared there: ci, dev"):
            resolve.resolve(ProfileRef("myapp", "nope"), scope)

    def test_an_unknown_namespace_lists_the_known_ones(self):
        scope = self.scope(MINIMAL + "[profiles.dev]\n")
        with self.assertRaisesRegex(CromError, "unknown namespace 'ghost'"):
            resolve.resolve(ProfileRef("ghost", "dev"), scope)

    def test_a_remembered_namespace_resolves_from_a_foreign_scope(self):
        other_source = self.root / "other" / ".crom.toml"
        other_source.parent.mkdir()
        other_source.write_text('namespace = "other"\n[profiles.dev]\n')
        registry.remember_namespace("other", other_source)

        here = self.scope(MINIMAL + "[profiles.dev]\n")
        profile = resolve.resolve(ProfileRef("other", "dev"), here)
        self.assertEqual(str(profile.ref), "other/dev")


if __name__ == "__main__":
    unittest.main()
