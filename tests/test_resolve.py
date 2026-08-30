"""Tests for reference parsing, argv composition, and variable interpolation."""

import os
import tempfile
import unittest
from pathlib import Path

from crom import config, flags, registry, resolve
from crom.model import (
    CromError,
    FailedProfile,
    ProfileRef,
    ProfileSpec,
    ResolvedProfile,
    parse_ref,
)

MINIMAL = 'namespace = "myapp"\n'


class ParseRefTest(unittest.TestCase):
    def test_bare_name_uses_the_ambient_namespace(self):
        self.assertEqual(parse_ref("dev", "myapp"), ProfileRef("myapp", "dev"))

    def test_qualified_name_overrides_the_ambient_namespace(self):
        self.assertEqual(parse_ref("other/dev", "myapp"), ProfileRef("other", "dev"))

    def test_a_third_segment_is_an_error_not_a_guess(self):
        with self.assertRaisesRegex(CromError, "invalid profile reference"):
            parse_ref("a/b/c", "myapp")

    def test_the_reference_type_validates_its_own_fields(self):
        """The module docstring promises names are "checked once, where they enter, and
        never again" — which is a property of the type, not of caller discipline. A
        caller that never went through `parse_ref` gets the same guarantee, so
        `resolve_spec` can compose a path from these fields without wondering."""
        with self.assertRaisesRegex(CromError, "invalid namespace"):
            ProfileRef("../escape", "dev")
        with self.assertRaisesRegex(CromError, "invalid profile name"):
            ProfileRef("myapp", "Not A Name")

    def test_the_spec_type_validates_its_own_name_too(self):
        """`spec.name` is the only identity a `ProfileSpec` carries: `_declare` indexes
        `profiles[spec.name]` straight into a TOML document and `resolve_spec` builds a
        `ProfileRef` from it. Every call site validated first, which is the convention
        this constructor replaces — `ProfileRef` was given the same guarantee for the
        same reason."""
        with self.assertRaisesRegex(CromError, "invalid profile name"):
            ProfileSpec(name="../escape")
        with self.assertRaisesRegex(CromError, "invalid profile name"):
            ProfileSpec(name="Not A Name")

    def test_path_traversal_cannot_reach_the_name_type(self):
        # Names become directory components, so `.` and `..` must be unrepresentable.
        # Both segments are checked; whichever is illegal is the one that reports.
        with self.assertRaisesRegex(CromError, "invalid namespace"):
            parse_ref("../escape", "myapp")
        with self.assertRaisesRegex(CromError, "invalid profile name"):
            parse_ref("myapp/..", "myapp")
        with self.assertRaisesRegex(CromError, "invalid profile reference"):
            parse_ref("/etc/passwd", "myapp")

    def test_a_trailing_newline_is_not_part_of_a_name(self):
        # Python's `$` matches before a trailing newline, so an anchor of `$` would let
        # "dev\n" through — a directory component with a newline in it, which then
        # splits that process into two lines when chrome.scan reads `ps` output.
        with self.assertRaisesRegex(CromError, "invalid profile name"):
            parse_ref("dev\n", "myapp")
        with self.assertRaisesRegex(CromError, "invalid namespace"):
            parse_ref("myapp\n/dev", "myapp")


class ArgvTest(unittest.TestCase):
    def test_crom_owned_switches_come_last_so_config_cannot_displace_them(self):
        argv = resolve.build_argv(Path("/chrome"), Path("/data"), 9300, ("--headless=new",))
        self.assertEqual(argv[0], "/chrome")
        self.assertEqual(argv[-2:], ("--user-data-dir=/data", "--remote-debugging-port=9300"))
        self.assertLess(argv.index("--headless=new"), argv.index("--user-data-dir=/data"))


class ComposeTest(unittest.TestCase):
    """The layering rule itself, without a config file or a port ledger in the way."""

    def flags(self, *texts: str) -> tuple:
        return flags.layer(texts, "test")

    def test_a_later_layer_replaces_an_earlier_layers_value_for_the_same_switch(self):
        composed = flags.compose(
            self.flags("--disable-features=A"), self.flags("--disable-features=B")
        )
        self.assertEqual(flags.render(composed), ("--disable-features=B",))

    def test_a_switch_only_one_layer_names_survives_untouched(self):
        composed = flags.compose(self.flags("--a"), self.flags("--b=1"))
        self.assertEqual(flags.render(composed), ("--a", "--b=1"))

    def test_an_overridden_switch_keeps_the_position_it_was_introduced_at(self):
        """Order is stable across runs, and overriding one early switch must not shuffle
        the rest of the list out from under a reader of `crom config`."""
        composed = flags.compose(self.flags("--a=1", "--b"), self.flags("--a=2"))
        self.assertEqual(flags.render(composed), ("--a=2", "--b"))

    def test_a_valueless_switch_is_not_the_same_as_an_empty_value(self):
        composed = flags.compose(self.flags("--a"), self.flags("--a="))
        self.assertEqual(flags.render(composed), ("--a=",))

    def test_only_the_first_equals_sign_splits_switch_from_value(self):
        composed = flags.compose(self.flags("--host-resolver-rules=MAP a b=1.2.3.4"))
        self.assertEqual(flags.render(composed), ("--host-resolver-rules=MAP a b=1.2.3.4",))


class LedgerFixture(unittest.TestCase):
    """A private state directory per test, and nothing else.

    Deliberately holds no test methods. A class that carries both a fixture and tests
    cannot be subclassed for the fixture alone — the subclass silently re-runs every
    parent test under its own name, which is how `ResolveSpecTest` came to re-run the
    whole of `ResolveTest` without exercising `resolve_spec` at all. Separating the two
    is what makes the inheritance mean what it looks like. [LAW:decomposition]
    """

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


class ResolveTest(LedgerFixture):
    """Resolution touches the port ledger, so each test gets its own state directory."""

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

    def test_a_profile_flag_replaces_the_defaults_entry_for_the_same_switch(self):
        """The defect this composition exists for: two `--disable-features` switches
        reached Chrome, which silently discards all but the last — so a project setting
        the switch for its own reasons deleted whatever `[defaults]` had set."""
        scope = self.scope(
            MINIMAL
            + '[defaults]\nflags = ["--disable-features=FromDefaults"]\n'
            + '[profiles.dev]\nflags = ["--disable-features=FromProfile"]\n'
        )
        argv = resolve.resolve(ProfileRef("myapp", "dev"), scope).argv
        self.assertEqual(
            [a for a in argv if a.startswith("--disable-features=")],
            ["--disable-features=FromProfile"],
        )

    def test_a_profile_flag_replaces_the_launch_policys_entry_for_the_same_switch(self):
        scope = self.scope(MINIMAL + '[profiles.dev]\nflags = ["--no-default-browser-check=0"]\n')
        argv = resolve.resolve(ProfileRef("myapp", "dev"), scope).argv
        self.assertIn("--no-default-browser-check=0", argv)
        self.assertNotIn("--no-default-browser-check", argv)

    def test_every_switch_reaches_chrome_exactly_once(self):
        scope = self.scope(
            MINIMAL
            + '[defaults]\nflags = ["--disable-features=A", "--window-size=800,600"]\n'
            + '[profiles.dev]\nflags = ["--disable-features=B", "--no-pings"]\n'
        )
        argv = resolve.resolve(ProfileRef("myapp", "dev"), scope).argv[1:]
        switches = [a.split("=", 1)[0] for a in argv]
        self.assertEqual(sorted(switches), sorted(set(switches)))

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

    def test_a_failed_resolution_reserves_no_port(self):
        """`port_for` writes to the machine-wide ledger the moment it is called.

        Reserving before the last thing that can fail left a port held by a profile that
        never resolved — unreleasable, and able to refuse a legitimate profile that port
        later. Every fallible step now runs while resolution is still pure.
        """
        scope = self.scope(MINIMAL + '[profiles.dev]\nflags = ["--x=${CROM_PROFIL_DIR}"]\n')
        with self.assertRaisesRegex(CromError, "unknown variable"):
            resolve.resolve(ProfileRef("myapp", "dev"), scope)
        self.assertEqual(registry.reservations(), {})

    def test_an_unknown_variable_in_env_also_reserves_no_port(self):
        scope = self.scope(MINIMAL + '[profiles.dev]\nenv = {A = "${CROM_NOPE}"}\n')
        with self.assertRaisesRegex(CromError, "unknown variable"):
            resolve.resolve(ProfileRef("myapp", "dev"), scope)
        self.assertEqual(registry.reservations(), {})

    def test_one_bad_profile_does_not_hide_the_others_in_a_listing(self):
        """`crom list` is what a user runs *because* something is broken.

        Letting one unresolvable declaration propagate made the command fail hardest in
        the situation it exists for. The failure is returned as a value instead — still
        reported, just no longer fatal to its neighbours.
        """
        scope = self.scope(
            MINIMAL + '[profiles.good]\n[profiles.bad]\nflags = ["--x=${CROM_NOPE}"]\n'
        )
        entries = resolve.resolve_all(scope)
        by_name = {str(e.ref): e for e in entries}

        self.assertIsInstance(by_name["myapp/good"], ResolvedProfile)
        self.assertIsInstance(by_name["myapp/bad"], FailedProfile)
        self.assertIn("unknown variable", by_name["myapp/bad"].error)

    def test_an_undeclared_profile_names_what_is_declared(self):
        scope = self.scope(MINIMAL + "[profiles.dev]\n[profiles.ci]\n")
        with self.assertRaisesRegex(CromError, "Declared there: ci, dev"):
            resolve.resolve(ProfileRef("myapp", "nope"), scope)

    def test_an_unknown_namespace_lists_the_known_ones(self):
        scope = self.scope(MINIMAL + "[profiles.dev]\n")
        with self.assertRaisesRegex(CromError, "unknown namespace 'ghost'"):
            resolve.resolve(ProfileRef("ghost", "dev"), scope)

    def test_a_renamed_namespace_is_dropped_not_silently_resolved(self):
        """`remember_namespace` is additive, so renaming a namespace in place leaves the
        old name pointing at the same file. Loading it would return a Scope whose real
        namespace differs from the one asked for, and `resolve_spec` would then build a
        profile directory and port under the requested name — a plausible-looking profile
        belonging to nothing.

        The old name is dropped here rather than reported with a `crom forget` to run:
        the ledger entry is crom's own memory outliving what it remembered, and forgetting
        it is both the only possible response and one crom can make itself. The name is
        then simply unknown, which is what it now is.
        """
        other_source = self.root / "other" / ".crom.toml"
        other_source.parent.mkdir()
        other_source.write_text('namespace = "other"\n[profiles.dev]\n')
        registry.remember_namespace("other", other_source)

        # The project renames itself; the ledger still maps the old name to this file.
        other_source.write_text('namespace = "other2"\n[profiles.dev]\n')

        here = self.scope(MINIMAL + "[profiles.dev]\n")
        with self.assertRaisesRegex(CromError, "unknown namespace 'other'"):
            resolve.resolve(ProfileRef("other", "dev"), here)

        self.assertNotIn("other", registry.namespaces())

    def test_a_namespace_whose_config_is_gone_is_dropped(self):
        other_source = self.root / "other" / ".crom.toml"
        other_source.parent.mkdir()
        other_source.write_text('namespace = "other"\n[profiles.dev]\n')
        registry.remember_namespace("other", other_source)
        other_source.unlink()

        here = self.scope(MINIMAL + "[profiles.dev]\n")
        with self.assertRaisesRegex(CromError, "unknown namespace 'other'"):
            resolve.resolve(ProfileRef("other", "dev"), here)

        self.assertNotIn("other", registry.namespaces())

    def test_an_undeclared_profile_is_declared_when_the_caller_means_to_use_it(self):
        """`resolve_or_declare` is what every command that asks *where profile X is* uses.
        The declaration it writes is bare, so `[defaults]` still governs the profile."""
        scope = self.scope(MINIMAL + "[profiles.dev]\n")

        profile = resolve.resolve_or_declare(ProfileRef("myapp", "nope"), scope, log=lambda _: None)

        self.assertEqual(str(profile.ref), "myapp/nope")
        self.assertIn("[profiles.nope]", scope.source.read_text())

    def test_a_remembered_namespace_resolves_from_a_foreign_scope(self):
        other_source = self.root / "other" / ".crom.toml"
        other_source.parent.mkdir()
        other_source.write_text('namespace = "other"\n[profiles.dev]\n')
        registry.remember_namespace("other", other_source)

        here = self.scope(MINIMAL + "[profiles.dev]\n")
        profile = resolve.resolve(ProfileRef("other", "dev"), here)
        self.assertEqual(str(profile.ref), "other/dev")


class ResolveSpecTest(LedgerFixture):
    """`resolve_spec` takes its namespace from the scope, and cannot be told otherwise.

    It used to accept a `ProfileRef` *and* a `Scope` as independent arguments while
    silently assuming they agreed — the profile directory was built from the ref's
    namespace and the ledger keyed on the ref, so pairing a ref with a foreign scope
    would have created a directory and a port reservation under a namespace that scope
    does not own, with nothing raised. Taking a bare name removes the second namespace
    rather than guarding against it.
    """

    def test_the_namespace_comes_from_the_scope(self):
        scope = self.scope(MINIMAL + "[profiles.dev]\n")
        profile = resolve.resolve_spec(scope, ProfileSpec(name="dev"))

        self.assertEqual(profile.ref, ProfileRef("myapp", "dev"))
        self.assertEqual(profile.profile_dir.parts[-2:], ("myapp", "dev"))
        self.assertIn("myapp/dev", registry.reservations())

    def test_the_name_comes_from_the_spec_that_carries_it(self):
        """`configwrite._declare` and `config.reject_duplicate_ports` key off `spec.name`
        — the latter iterates `.values()` and has no other identity available. Taking the
        name separately meant a caller could resolve one profile and declare another,
        with every call site keeping them in step only by convention."""
        scope = self.scope(MINIMAL + "[profiles.dev]\n")
        profile = resolve.resolve_spec(scope, ProfileSpec(name="dev"))

        self.assertEqual(profile.ref.name, "dev")
        self.assertEqual(profile.profile_dir.name, "dev")

    def test_there_is_no_second_namespace_to_disagree_with_the_first(self):
        """The guarantee is structural, so this asserts the signature itself: a caller
        has nowhere to put a namespace that differs from the scope's."""
        import inspect

        parameters = list(inspect.signature(resolve.resolve_spec).parameters)
        self.assertEqual(parameters, ["scope", "spec"])


if __name__ == "__main__":
    unittest.main()
