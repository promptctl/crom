"""Tests for reference parsing, argv composition, and variable interpolation."""

import os
import tempfile
import unittest
from pathlib import Path

from crom import config, flags, registry, resolve
from crom.model import (
    CromError,
    FailedProfile,
    Flag,
    Layer,
    ProfileRef,
    Reason,
    Resolution,
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
        with self.assertRaisesRegex(CromError, "invalid profile reference") as caught:
            parse_ref("a/b/c", "myapp")
        # `parse_ref` reuses `validate_name`'s slug for a reference typed at the CLI,
        # which is the case the grouping comment in `Reason` used to hide.
        self.assertIs(caught.exception.reason, Reason.INVALID_NAME)

    def test_the_reference_type_validates_its_own_fields(self):
        """The module docstring promises names are "checked once, where they enter, and
        never again" — which is a property of the type, not of caller discipline. A
        caller that never went through `parse_ref` gets the same guarantee, so
        `resolve_spec` can compose a path from these fields without wondering."""
        with self.assertRaisesRegex(CromError, "invalid namespace") as caught:
            ProfileRef("../escape", "dev")
        self.assertIs(caught.exception.reason, Reason.INVALID_NAME)
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


def rendered(emitted) -> tuple[str, ...]:
    """The flag texts an emitted list carries, for tests about what Chrome is given."""
    return flags.render(tuple(item.flag for item in emitted))


def dropped_switches(profile) -> tuple[str, ...]:
    """Just the switches a `drop_flags` entry removed from a resolved profile."""
    return tuple(removal.what.question for removal in profile.provenance.dropped)


class ArgvTest(unittest.TestCase):
    def test_crom_owned_switches_come_last_so_config_cannot_displace_them(self):
        argv = resolve.build_argv(Path("/chrome"), Path("/data"), 9300, ("--headless=new",))
        self.assertEqual(argv[0], "/chrome")
        self.assertEqual(argv[-2:], ("--user-data-dir=/data", "--remote-debugging-port=9300"))
        self.assertLess(argv.index("--headless=new"), argv.index("--user-data-dir=/data"))


class ComposeTest(unittest.TestCase):
    """The layering rule itself, without a config file or a port ledger in the way."""

    def layer(self, *texts: str, drops: tuple[str, ...] = (), origin: str = "test") -> Layer:
        return Layer(flags.layer(texts, "test"), flags.drops(drops, "test"), origin=origin)

    def dropped(self, composed) -> tuple[str, ...]:
        """Just the switches a drop removed — what most of these tests are about."""
        return tuple(removal.what.question for removal in composed.dropped)

    def test_a_later_layer_replaces_an_earlier_layers_value_for_the_same_switch(self):
        composed = flags.compose(
            self.layer("--disable-blink-features=A"), self.layer("--disable-blink-features=B")
        )
        self.assertEqual(flags.render(composed.flags), ("--disable-blink-features=B",))

    def test_a_switch_only_one_layer_names_survives_untouched(self):
        composed = flags.compose(self.layer("--a"), self.layer("--b=1"))
        self.assertEqual(flags.render(composed.flags), ("--a", "--b=1"))

    def test_an_overridden_switch_keeps_the_position_it_was_introduced_at(self):
        """Order is stable across runs, and overriding one early switch must not shuffle
        the rest of the list out from under a reader of `crom config`."""
        composed = flags.compose(self.layer("--a=1", "--b"), self.layer("--a=2"))
        self.assertEqual(flags.render(composed.flags), ("--a=2", "--b"))

    def test_a_valueless_switch_is_not_the_same_as_an_empty_value(self):
        composed = flags.compose(self.layer("--a"), self.layer("--a="))
        self.assertEqual(flags.render(composed.flags), ("--a=",))

    def test_only_the_first_equals_sign_splits_switch_from_value(self):
        composed = flags.compose(self.layer("--host-resolver-rules=MAP a b=1.2.3.4"))
        self.assertEqual(flags.render(composed.flags), ("--host-resolver-rules=MAP a b=1.2.3.4",))

    def test_a_layer_may_not_both_set_and_drop_one_switch(self):
        """The constructor is what makes `Layer`'s disjointness true, not the care of the
        one parser that happens to build them — a test or a future caller reaching the type
        directly would otherwise construct the state the docstring says cannot exist."""
        with self.assertRaisesRegex(CromError, "both sets and drops --headless") as caught:
            Layer(sets=(Flag("--headless", "new"),), drops=frozenset({"--headless"}), origin="test")
        self.assertIs(caught.exception.reason, Reason.FLAGS_INVALID)

    def test_a_later_layer_removes_a_switch_it_drops(self):
        composed = flags.compose(self.layer("--a=1", "--b"), self.layer(drops=("--a",)))
        self.assertEqual(flags.render(composed.flags), ("--b",))

    def test_a_drop_reaches_every_layer_beneath_it_not_just_the_one_below(self):
        composed = flags.compose(
            self.layer("--a=1"), self.layer("--b"), self.layer(drops=("--a",))
        )
        self.assertEqual(flags.render(composed.flags), ("--b",))

    def test_a_layer_below_a_drop_can_set_the_switch_again(self):
        """A drop is not a veto over the layers above it: `profile > defaults > policy`
        means the *last* layer to speak decides, and dropping is one way to speak."""
        composed = flags.compose(
            self.layer("--a=1"), self.layer(drops=("--a",)), self.layer("--a=2")
        )
        self.assertEqual(flags.render(composed.flags), ("--a=2",))

    def test_a_switch_set_again_after_a_drop_lands_with_the_layer_that_supplies_it(self):
        """A drop takes the switch's place in the list with it, so the layer that sets it
        afterwards is introducing it rather than restoring it. Every other switch keeps
        its slot — the stability the ordering rule exists for is about the list, not about
        a switch whose removal the config asked for."""
        composed = flags.compose(
            self.layer("--policy=1", "--kept"),
            self.layer(drops=("--policy",)),
            self.layer("--policy=2"),
        )
        self.assertEqual(flags.render(composed.flags), ("--kept", "--policy=2"))

    def test_dropping_a_switch_no_layer_supplies_changes_nothing(self):
        composed = flags.compose(self.layer("--a"), self.layer(drops=("--b",)))
        self.assertEqual(flags.render(composed.flags), ("--a",))
        self.assertEqual(self.dropped(composed), ())

    def test_a_drop_that_removed_something_is_reported_as_dropped(self):
        """The one fact `flags` alone cannot carry: a switch that was removed and a switch
        nobody ever set are both simply absent from it."""
        composed = flags.compose(self.layer("--a=1", "--b"), self.layer(drops=("--a", "--b")))
        self.assertEqual(composed.flags, ())
        self.assertEqual(self.dropped(composed), ("--a", "--b"))

    def test_a_switch_dropped_and_set_again_below_is_not_reported_as_dropped(self):
        """`crom config` prints this beside argv, so it must describe the argv it stands
        next to: a switch that is right there is not one the reader lost."""
        composed = flags.compose(
            self.layer("--a=1"), self.layer(drops=("--a",)), self.layer("--a=2")
        )
        self.assertEqual(self.dropped(composed), ())


    def test_a_layer_that_says_something_must_say_where_it_was_said(self):
        """An unattributed layer would reach `crom config` as a flag whose origin renders
        blank, and the renderer has nothing to fall back on. The constructor is what makes
        that unreachable rather than the care of whichever caller builds the layer."""
        with self.assertRaisesRegex(CromError, "must name where it was written") as caught:
            Layer(sets=(Flag("--headless", None),))
        # `internal` is the one slug that means "file a bug" rather than "fix your
        # input", so it must not be reachable by anything a config file can say — and
        # a reader who gets it has been told the right thing about whose fault it is.
        self.assertIs(caught.exception.reason, Reason.INTERNAL)

    def test_a_resolution_that_answers_nothing_is_a_crom_bug_not_a_config_fault(self):
        """`Resolution` exists to say where a value came from, so one holding no answers
        has nothing to report and no honest way to render itself. Nothing a config file
        can say reaches this — only crom building the type wrong does, which is what
        `internal` is for and why it must stay unreachable from any input."""
        with self.assertRaisesRegex(CromError, "nothing was resolved") as caught:
            Resolution(question="--headless", answers=())
        self.assertIs(caught.exception.reason, Reason.INTERNAL)
        with self.assertRaisesRegex(CromError, "must name where it was written"):
            Layer(drops=frozenset({"--headless"}))
        # A stanza that says nothing contributes nothing to the report, so it needs no name.
        self.assertEqual(Layer().origin, "")

    def test_the_standing_answer_is_the_last_layer_to_speak(self):
        composed = flags.compose(
            self.layer("--a=1", origin="policy"), self.layer("--a=2", origin="profile")
        )
        (answered,) = composed.emitted[0].why
        self.assertEqual(answered.question, "--a")
        self.assertEqual((answered.stands.layer, answered.stands.said), ("profile", "--a=2"))

    def test_every_layer_a_switch_outranked_is_kept_in_the_order_they_spoke(self):
        """A reader hunting for a flag they wrote needs their own layer named, not just
        whoever won — and with three layers, only the loser list can tell them which."""
        composed = flags.compose(
            self.layer("--a=1", origin="policy"),
            self.layer("--a=2", origin="defaults"),
            self.layer("--a=3", origin="profile"),
        )
        (answered,) = composed.emitted[0].why
        self.assertEqual(
            [(a.layer, a.said) for a in answered.replaced],
            [("policy", "--a=1"), ("defaults", "--a=2")],
        )

    def test_a_drop_reports_the_layer_that_removed_it_and_the_one_that_supplied_it(self):
        composed = flags.compose(
            self.layer("--a=1", origin="policy"),
            self.layer("--a=2", origin="defaults"),
            self.layer(drops=("--a",), origin="profile"),
        )
        (removal,) = composed.dropped
        self.assertEqual(removal.by, "profile")
        self.assertEqual(removal.what.stands.layer, "defaults")
        self.assertEqual([a.layer for a in removal.what.replaced], ["policy"])


class FeaturesTest(unittest.TestCase):
    """Folding feature tables into the at-most-two switches that carry them."""

    def features(self, *tables: dict) -> tuple[str, ...]:
        """The switches these layers fold to, named for the test so origins stay legible."""
        return rendered(
            flags.features(*((f"layer{n}", table) for n, table in enumerate(tables)))
        )

    def test_a_feature_reaches_the_switch_its_state_selects(self):
        self.assertEqual(
            self.features({"On": True, "Off": False}),
            ("--enable-features=On", "--disable-features=Off"),
        )

    def test_an_unmentioned_feature_reaches_neither_switch(self):
        """Three states, and the third is silence. A feature no layer names must not be
        turned on *or* off — which is why this is a table and not two lists."""
        self.assertEqual(self.features({}), ())
        self.assertEqual(self.features({"Off": False}), ("--disable-features=Off",))

    def test_names_in_one_state_are_comma_joined_into_one_switch(self):
        self.assertEqual(self.features({"A": False, "B": False}), ("--disable-features=A,B",))

    def test_a_later_layer_moves_a_feature_rather_than_adding_a_second_answer(self):
        """The reason a table beats two lists: flipping a feature *removes* it from the
        switch it was in. Two lists would leave the name in both, and Chrome resolves that
        pair by disabling — so the later layer would silently lose."""
        self.assertEqual(self.features({"X": False}, {"X": True}), ("--enable-features=X",))

    def test_layers_fold_later_wins_leaving_untouched_features_alone(self):
        self.assertEqual(
            self.features({"Kept": False}, {"Flipped": False}, {"Flipped": True}),
            ("--enable-features=Flipped", "--disable-features=Kept"),
        )


    def test_one_switch_carries_one_resolution_per_feature_it_names(self):
        """The shape that makes feature provenance different from a flag's: the switch is a
        union, so 'the profile overrode the policy's --disable-features' is a false
        sentence. Attribution is per name, and the emitted value is joined from exactly the
        names the report explains."""
        (emitted,) = flags.features(
            ("policy", {"FromPolicy": False}), ("profile", {"FromProfile": False})
        )
        self.assertEqual(str(emitted.flag), "--disable-features=FromPolicy,FromProfile")
        self.assertEqual(
            [(r.question, r.stands.layer) for r in emitted.why],
            [("FromPolicy", "policy"), ("FromProfile", "profile")],
        )

    def test_a_flipped_feature_names_the_layer_it_outranked_in_that_layers_vocabulary(self):
        (emitted,) = flags.features(("policy", {"X": False}), ("profile", {"X": True}))
        (answered,) = emitted.why
        self.assertEqual(str(emitted.flag), "--enable-features=X")
        self.assertEqual((answered.stands.layer, answered.stands.said), ("profile", "true"))
        self.assertEqual([(a.layer, a.said) for a in answered.replaced], [("policy", "false")])


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
        """The defect this composition exists for: two occurrences of one switch reached
        Chrome, which resolves the pair by its own per-switch rules — so a project setting
        a switch for its own reasons silently changed whatever `[defaults]` had set.

        `--disable-blink-features` rather than `--disable-features`: the latter is now
        crom's to compose from `features` tables and a `flags` list may not name it, so it
        can no longer stand as the example of a switch two layers both answer."""
        scope = self.scope(
            MINIMAL
            + '[defaults]\nflags = ["--disable-blink-features=FromDefaults"]\n'
            + '[profiles.dev]\nflags = ["--disable-blink-features=FromProfile"]\n'
        )
        argv = resolve.resolve(ProfileRef("myapp", "dev"), scope).argv
        self.assertEqual(
            [a for a in argv if a.startswith("--disable-blink-features=")],
            ["--disable-blink-features=FromProfile"],
        )

    def test_a_profile_flag_replaces_the_launch_policys_entry_for_the_same_switch(self):
        scope = self.scope(MINIMAL + '[profiles.dev]\nflags = ["--no-default-browser-check=0"]\n')
        argv = resolve.resolve(ProfileRef("myapp", "dev"), scope).argv
        self.assertIn("--no-default-browser-check=0", argv)
        self.assertNotIn("--no-default-browser-check", argv)

    def test_a_profile_can_drop_a_switch_the_launch_policy_supplies(self):
        """The case `flags` alone cannot express: leaving sync on. Overriding
        `--disable-sync` with a value would still hand Chrome the switch, and crom's policy
        list is crom's, not something a project edits."""
        scope = self.scope(MINIMAL + '[profiles.dev]\ndrop_flags = ["--disable-sync"]\n')
        profile = resolve.resolve(ProfileRef("myapp", "dev"), scope)

        self.assertNotIn("--disable-sync", profile.argv)
        self.assertIn("--no-first-run", profile.argv)
        self.assertEqual(dropped_switches(profile), ("--disable-sync",))

    def test_a_profile_can_drop_a_flag_it_would_inherit_from_defaults(self):
        scope = self.scope(
            MINIMAL
            + '[defaults]\nflags = ["--headless=new"]\n'
            + '[profiles.dev]\ndrop_flags = ["--headless"]\n'
        )
        profile = resolve.resolve(ProfileRef("myapp", "dev"), scope)

        self.assertNotIn("--headless=new", profile.argv)
        self.assertEqual(dropped_switches(profile), ("--headless",))

    def test_dropping_a_switch_no_layer_supplies_resolves_without_complaint(self):
        """A drop states what this profile must not run with, and a layer below dropping
        the same idea first is a config that agrees with itself, not one that errs."""
        scope = self.scope(MINIMAL + '[profiles.dev]\ndrop_flags = ["--nothing-sets-this"]\n')
        profile = resolve.resolve(ProfileRef("myapp", "dev"), scope)

        self.assertEqual(dropped_switches(profile), ())

    def test_a_drop_in_defaults_reaches_the_policy_and_the_profile_can_set_it_back(self):
        """`profile > defaults > policy` for drops as for everything else: `[defaults]`
        removes the policy's switch, and a profile below it still gets the last word."""
        dropped = self.scope(
            MINIMAL + '[defaults]\ndrop_flags = ["--no-pings"]\n[profiles.dev]\n'
        )
        self.assertNotIn("--no-pings", resolve.resolve(ProfileRef("myapp", "dev"), dropped).argv)

        restored = self.scope(
            MINIMAL
            + '[defaults]\ndrop_flags = ["--no-pings"]\n'
            + '[profiles.dev]\nflags = ["--no-pings"]\n'
        )
        profile = resolve.resolve(ProfileRef("myapp", "dev"), restored)
        self.assertIn("--no-pings", profile.argv)
        self.assertEqual(dropped_switches(profile), ())

    def test_every_switch_reaches_chrome_exactly_once(self):
        scope = self.scope(
            MINIMAL
            + '[defaults]\nflags = ["--disable-blink-features=A", "--window-size=800,600"]\n'
            + '[profiles.dev]\nflags = ["--disable-blink-features=B", "--no-pings"]\n'
        )
        argv = resolve.resolve(ProfileRef("myapp", "dev"), scope).argv[1:]
        switches = [a.split("=", 1)[0] for a in argv]
        self.assertEqual(sorted(switches), sorted(set(switches)))

    def test_a_profiles_feature_joins_croms_own_rather_than_replacing_it(self):
        """The defect the `features` table exists for. A config that wanted one feature off
        used to write `--disable-features=SharedStorageAPI`, which replaced crom's whole
        entry for that switch — deleting the What's New suppression with nothing to show
        for it. Composed as a table, both names ride the one switch Chrome reads."""
        scope = self.scope(
            MINIMAL + '[profiles.dev]\n[profiles.dev.features]\nSharedStorageAPI = false\n'
        )
        argv = resolve.resolve(ProfileRef("myapp", "dev"), scope).argv

        disabled = [a for a in argv if a.startswith("--disable-features=")]
        self.assertEqual(len(disabled), 1)
        self.assertCountEqual(
            disabled[0].removeprefix("--disable-features=").split(","),
            ["ChromeWhatsNewUI", "SharedStorageAPI"],
        )

    def test_a_profile_can_turn_a_policy_feature_back_on(self):
        """Turning it on is implemented by *removing* the name from the disable list, which
        is the only thing that could work: an added `--enable-features` loses to a
        `--disable-features` naming the same feature, in either order."""
        scope = self.scope(
            MINIMAL + '[profiles.dev]\n[profiles.dev.features]\nChromeWhatsNewUI = true\n'
        )
        argv = resolve.resolve(ProfileRef("myapp", "dev"), scope).argv

        disabled = [a for a in argv if a.startswith("--disable-features=")]
        self.assertEqual(disabled, [])
        self.assertIn("--enable-features=ChromeWhatsNewUI", argv)

    def test_a_profile_feature_overrides_the_defaults_table(self):
        scope = self.scope(
            MINIMAL
            + "[defaults.features]\nShared = false\nKept = false\n"
            + "[profiles.dev]\n[profiles.dev.features]\nShared = true\n"
        )
        argv = resolve.resolve(ProfileRef("myapp", "dev"), scope).argv

        self.assertIn("--enable-features=Shared", argv)
        disabled = [a for a in argv if a.startswith("--disable-features=")]
        self.assertEqual(len(disabled), 1)
        self.assertCountEqual(
            disabled[0].removeprefix("--disable-features=").split(","),
            ["ChromeWhatsNewUI", "Kept"],
        )

    def test_variables_expand_in_flags(self):
        scope = self.scope(
            MINIMAL + '[profiles.dev]\nflags = ["--load-extension=${CROM_CONFIG_DIR}/ext"]\n'
        )
        profile = resolve.resolve(ProfileRef("myapp", "dev"), scope)
        self.assertIn(f"--load-extension={self.root}/ext", profile.argv)

    def test_a_variable_in_a_switch_name_is_refused_rather_than_emitted_twice(self):
        """crom resolves a switch by the name the file spells and expands afterwards, so a
        variable in a switch half names something that can never meet the switch Chrome is
        given. `--${CROM_PROFILE}-x` in `[defaults]` and `--dev-x` in the profile used to be
        two switches to crom and one to Chrome, and both reached the command line — the one
        thing single emission exists to prevent.

        The refusal lands at load rather than at resolve, so the file is refused once for
        every profile it declares rather than per profile that happens to read it.
        """
        with self.assertRaisesRegex(CromError, "interpolates a variable into the switch name"):
            self.scope(
                MINIMAL
                + '[defaults]\nflags = ["--${CROM_PROFILE}-x=1"]\n'
                + '[profiles.dev]\nflags = ["--dev-x=2"]\n'
            )

    def test_variables_expand_in_env_values(self):
        scope = self.scope(MINIMAL + '[profiles.dev]\nenv = { DEBUG_URL = "${CROM_PORT}" }\n')
        profile = resolve.resolve(ProfileRef("myapp", "dev"), scope)
        self.assertEqual(profile.env["DEBUG_URL"], str(profile.port))

    def test_an_unknown_variable_is_caught_even_in_a_flag_that_gets_overridden(self):
        """The variable check reads every layer's text, not the composed result.

        A `${TYPO}` in a `[defaults]` flag that this profile happens to override never
        reaches argv, so checking only what is launched would report it for the profiles
        that inherit it and stay silent for the ones that don't — a diagnostic that
        depends on which stanza you were resolving. It is a typo either way.
        """
        scope = self.scope(
            MINIMAL
            + '[defaults]\nflags = ["--x=${CROM_NOPE}"]\n'
            + '[profiles.dev]\nflags = ["--x=fine"]\n'
        )
        with self.assertRaisesRegex(CromError, "unknown variable"):
            resolve.resolve(ProfileRef("myapp", "dev"), scope)

    def test_an_unknown_variable_is_an_error_not_an_empty_string(self):
        scope = self.scope(MINIMAL + '[profiles.dev]\nflags = ["--x=${CROM_NOPE}"]\n')
        with self.assertRaisesRegex(CromError, "unknown variable") as caught:
            resolve.resolve(ProfileRef("myapp", "dev"), scope)
        self.assertIs(caught.exception.reason, Reason.VARIABLE_UNKNOWN)

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
        with self.assertRaisesRegex(CromError, "Declared there: ci, dev") as caught:
            resolve.resolve(ProfileRef("myapp", "nope"), scope)
        self.assertIs(caught.exception.reason, Reason.PROFILE_UNKNOWN)

    def test_an_unknown_namespace_lists_the_known_ones(self):
        scope = self.scope(MINIMAL + "[profiles.dev]\n")
        with self.assertRaisesRegex(CromError, "unknown namespace 'ghost'") as caught:
            resolve.resolve(ProfileRef("ghost", "dev"), scope)
        # Against `PROFILE_UNKNOWN` above: both `NotFound`, both exit 3, both "crom
        # cannot find what you named" — and they send the reader to different lines of
        # a different file. The clearest confusable pair in the codebase.
        self.assertIs(caught.exception.reason, Reason.NAMESPACE_UNKNOWN)

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
