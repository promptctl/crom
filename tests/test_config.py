"""Tests for the config checkpoint: what it accepts, and what it refuses to accept."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crom import config, flags
from crom.paths import default_profiles_root
from crom.model import DEFAULT_SEED, Conflict, CromError, Scope, SeedChrome, SeedFresh, SeedPath

SOURCE = Path("/proj/.crom.toml")

MINIMAL = 'namespace = "myapp"\n'

_chrome_stub = None


def setUpModule():
    """Keep the parser's tests about the parser.

    `config.parse` resolves `chrome_binary` unconditionally, and the fixtures here omit
    it — so without this every test below falls through to a live `find_chrome()`, and
    the whole module fails with "no Chrome executable found" on a machine that has none,
    while claiming to test unknown-key rejection and friends. The tests that are
    genuinely about `chrome_binary` pass one explicitly and never reach the stub.
    """
    global _chrome_stub
    _chrome_stub = mock.patch.object(config, "find_chrome", return_value=Path("/stub/chrome"))
    _chrome_stub.start()


def tearDownModule():
    _chrome_stub.stop()


def parse(text: str, **kwargs):
    return config.parse(text, SOURCE, **kwargs)


class NamespaceTest(unittest.TestCase):
    def test_namespace_is_required(self):
        with self.assertRaisesRegex(CromError, "namespace"):
            parse("[profiles.dev]\n")

    def test_user_namespace_is_reserved(self):
        # Conflict specifically, not merely CromError: `crom init` and
        # `registry.forget_namespace` refuse the same reserved name as exit 4, and a
        # script branching on that code must see it from every path that decides it.
        # Conflict subclasses CromError, so asserting the base would pass either way.
        with self.assertRaisesRegex(Conflict, "reserved"):
            parse('namespace = "user"\n')

    def test_user_scope_file_may_not_redeclare_its_namespace(self):
        with self.assertRaisesRegex(CromError, "remove the `namespace` key"):
            parse('namespace = "other"\n', namespace="user")

    def test_invalid_namespace_is_rejected(self):
        with self.assertRaisesRegex(CromError, "invalid namespace"):
            parse('namespace = "My App"\n')


class ProfileTest(unittest.TestCase):
    def test_each_stanza_keeps_its_own_flags_for_composition_to_resolve(self):
        """Parsing separates the layers; `flags.compose` is what resolves between them.

        A parser that merged them here would decide the layering rule in two places —
        this one and `resolve_spec` — so the stanzas stay distinct all the way down.
        """
        scope = parse(MINIMAL + '[defaults]\nflags = ["--a"]\n[profiles.dev]\nflags = ["--b"]\n')
        self.assertEqual(flags.render(scope.default_flags.sets), ("--a",))
        self.assertEqual(flags.render(scope.profiles["dev"].flags.sets), ("--b",))

    def test_one_stanza_may_not_set_the_same_switch_twice(self):
        """One list disagreeing with itself, which single emission gives no meaning.

        Not an override refusal — a profile overriding `[defaults]` is the feature. This
        is the case where the earlier entry could not reach Chrome under any reading, so
        the message names the comma-joined form that expresses what was meant.
        """
        with self.assertRaisesRegex(CromError, r"--disable-blink-features=A,B"):
            parse(
                MINIMAL
                + '[profiles.dev]\n'
                + 'flags = ["--disable-blink-features=A", "--disable-blink-features=B"]\n'
            )

    def test_a_duplicated_valueless_switch_is_not_told_to_join_values(self):
        """There is nothing to comma-join, and saying otherwise is a false claim."""
        with self.assertRaisesRegex(CromError, r"Write the switch once: --no-pings"):
            parse(MINIMAL + '[profiles.dev]\nflags = ["--no-pings", "--no-pings"]\n')

    def test_a_switch_repeated_verbatim_is_told_to_write_it_once(self):
        """A copy-paste duplicate has nothing distinct to join, so suggesting
        `--foo=bar,bar` would present a redundant rewrite as the intended fix."""
        with self.assertRaisesRegex(CromError, r"Write the switch once: --foo=bar"):
            parse(MINIMAL + '[profiles.dev]\nflags = ["--foo=bar", "--foo=bar"]\n')

    def test_a_switch_given_both_a_value_and_none_is_not_guessed_at(self):
        """The two forms can mean different things, so crom names the disagreement rather
        than picking one and presenting it as what the author meant."""
        with self.assertRaisesRegex(CromError, r"disagree about whether --foo takes a value"):
            parse(MINIMAL + '[profiles.dev]\nflags = ["--foo", "--foo=bar"]\n')

    def test_a_duplicated_reserved_switch_is_refused_as_reserved_not_as_a_duplicate(self):
        """The duplicate remedy — write it once, comma-joined — fails again on the next
        load, because no occurrence of a reserved switch is allowed however it is spelled.
        A diagnostic whose remedy does not work is worse than none."""
        with self.assertRaisesRegex(CromError, "crom owns it"):
            parse(MINIMAL + '[profiles.dev]\nflags = ["--user-data-dir=/a", "--user-data-dir=/b"]\n')

    def test_a_stanza_keeps_its_drops_beside_the_flags_they_travel_with(self):
        scope = parse(
            MINIMAL
            + '[defaults]\ndrop_flags = ["--disable-sync"]\n'
            + '[profiles.dev]\nflags = ["--b"]\ndrop_flags = ["--no-pings", "--a"]\n'
        )
        self.assertEqual(scope.default_flags.drops, frozenset({"--disable-sync"}))
        self.assertEqual(scope.profiles["dev"].flags.drops, frozenset({"--no-pings", "--a"}))

    def test_a_drop_entry_carrying_a_value_is_refused_rather_than_truncated(self):
        """A drop removes the switch whatever value it inherited, so the value could not
        be honoured — and honouring the rest of the entry silently would let the file say
        something crom does not do."""
        with self.assertRaisesRegex(CromError, r"(?s)carries a value.*'--disable-sync'"):
            parse(MINIMAL + '[profiles.dev]\ndrop_flags = ["--disable-sync=false"]\n')

    def test_a_switch_dropped_twice_is_refused_like_one_set_twice(self):
        with self.assertRaisesRegex(CromError, r"--no-pings is named twice"):
            parse(MINIMAL + '[profiles.dev]\ndrop_flags = ["--no-pings", "--no-pings"]\n')

    def test_a_stanza_may_not_both_set_and_drop_one_switch(self):
        """Setting it here already replaces whatever was inherited, so the drop could not
        mean anything — and which of the two the author meant is only theirs to say."""
        with self.assertRaisesRegex(CromError, r"both sets and drops --headless"):
            parse(
                MINIMAL
                + '[profiles.dev]\nflags = ["--headless=new"]\ndrop_flags = ["--headless"]\n'
            )

    def test_a_drop_entry_that_could_never_match_a_switch_is_refused(self):
        """Each of these would drop nothing and say nothing — a config stating a removal
        crom does not perform. A merely misspelled switch cannot be told from one naming a
        switch no layer happens to set, which is allowed; these are wrong on their face."""
        for entry, fault in (
            ('""', "names an empty switch"),
            ('" --no-pings"', "whitespace"),
            ('"--${CROM_PROFILE}"', "interpolates a variable"),
        ):
            with self.subTest(entry=entry), self.assertRaisesRegex(CromError, fault):
                parse(MINIMAL + f"[profiles.dev]\ndrop_flags = [{entry}]\n")

    def test_a_drop_entry_is_diagnosed_by_the_fault_its_remedy_would_not_cure(self):
        """An entry can be both ill-shaped and value-carrying, and the value remedy is the
        switch itself — so the switch is what has to be legal for that remedy to work.
        Answered the other way round, `"--${FOO}=bar"` was told to write `"--${FOO}"` and
        `"=foo"` to write `""`, each failing again on the next load. The same rule the
        reserved check follows: a diagnostic whose remedy does not work is worse than none.
        """
        for entry, fault in (
            ('"--${FOO}=bar"', "interpolates a variable"),
            ('" --foo=bar"', "whitespace"),
            ('"=foo"', "names an empty switch"),
        ):
            with self.subTest(entry=entry), self.assertRaisesRegex(CromError, fault):
                parse(MINIMAL + f"[profiles.dev]\ndrop_flags = [{entry}]\n")

    def test_a_well_formed_drop_entry_with_a_value_is_told_to_drop_the_value(self):
        """The remedy the other test protects: here the switch *is* legal, so naming the
        value is the useful thing to say, and writing what it suggests loads cleanly."""
        with self.assertRaisesRegex(CromError, r"(?s)carries a value.*'--disable-sync'"):
            parse(MINIMAL + '[profiles.dev]\ndrop_flags = ["--disable-sync=false"]\n')
        scope = parse(MINIMAL + '[profiles.dev]\ndrop_flags = ["--disable-sync"]\n')
        self.assertEqual(scope.profiles["dev"].flags.drops, frozenset({"--disable-sync"}))

    def test_the_switches_crom_owns_are_refused_in_drop_flags_too(self):
        """Reserved is a fact about the switch, not about which list named it: a config
        that could drop `--user-data-dir` would leave crom unable to name the profile."""
        for switch in ("--user-data-dir", "--remote-debugging-port", "--remote-debugging-pipe"):
            with self.subTest(switch=switch), self.assertRaisesRegex(CromError, "crom owns it"):
                parse(MINIMAL + f'[profiles.dev]\ndrop_flags = ["{switch}"]\n')

    def test_dropping_a_feature_switch_is_answered_in_the_polarity_it_was_meant(self):
        """Dropping `--disable-features` means "stop turning X off", which is `X = true` —
        the opposite polarity from *setting* the same switch. Answering with the switch's
        own polarity would send the author to write the entry they already have."""
        for switch, entry in (
            ("--disable-features", "FeatureName = true"),
            ("--enable-features", "FeatureName = false"),
        ):
            with self.subTest(switch=switch), self.assertRaises(CromError) as caught:
                parse(MINIMAL + f'[profiles.dev]\ndrop_flags = ["{switch}"]\n')
            self.assertIn("[profiles.dev].features", str(caught.exception))
            self.assertIn(entry, str(caught.exception))

    def test_a_reserved_drop_carrying_a_value_is_refused_as_reserved_not_for_the_value(self):
        """Same load-bearing order as the `flags` sibling: "an entry here is a switch name"
        would send the author to write `--enable-features`, which fails again on the next
        load, because no occurrence of a reserved switch is allowed however it is spelled.
        A diagnostic whose remedy does not work is worse than none."""
        with self.assertRaises(CromError) as caught:
            parse(MINIMAL + '[profiles.dev]\ndrop_flags = ["--enable-features=X"]\n')
        self.assertIn("FeatureName = false", str(caught.exception))
        self.assertNotIn("carries a value", str(caught.exception))

    def test_drop_flags_must_be_a_list_of_strings(self):
        with self.assertRaisesRegex(CromError, r"drop_flags must be a list of switch names"):
            parse(MINIMAL + "[profiles.dev]\ndrop_flags = 3\n")

    def test_unknown_keys_are_refused_rather_than_ignored(self):
        with self.assertRaisesRegex(CromError, "unknown key"):
            parse(MINIMAL + "[profiles.dev]\nfalgs = []\n")

    def test_reserved_switches_are_refused(self):
        for flag in ("--user-data-dir=/tmp/x", "--remote-debugging-port=1234"):
            with self.subTest(flag=flag), self.assertRaisesRegex(CromError, "crom owns it"):
                parse(MINIMAL + f'[profiles.dev]\nflags = ["{flag}"]\n')

    def test_a_feature_switch_in_flags_is_refused_and_points_at_the_features_table(self):
        """crom composes these two switches itself, so a flag naming one would be replaced
        rather than merged — the exact defect `features` exists to fix. The message has to
        name the key that *does* work, in the polarity the author reached for."""
        for flag, entry in (
            ("--disable-features=SharedStorageAPI", "FeatureName = false"),
            ("--enable-features=SharedStorageAPI", "FeatureName = true"),
        ):
            with self.subTest(flag=flag), self.assertRaises(CromError) as caught:
                parse(MINIMAL + f'[profiles.dev]\nflags = ["{flag}"]\n')
            self.assertIn("[profiles.dev].features", str(caught.exception))
            self.assertIn(entry, str(caught.exception))

    def test_features_must_be_a_table_of_booleans(self):
        """`false` is a state, `"false"` is a string that would render into the switch as
        a feature name — so the table takes booleans and says so."""
        for table in ('SharedStorageAPI = "false"', "SharedStorageAPI = 0"):
            with self.subTest(table=table), self.assertRaisesRegex(CromError, "true/false"):
                parse(MINIMAL + f"[profiles.dev]\n[profiles.dev.features]\n{table}\n")

    def test_a_feature_name_that_would_not_reach_chrome_as_written_is_refused(self):
        """Every one of these reaches Chrome meaning something other than what the config
        shows, and Chrome ignores names it does not know — so without the checkpoint the
        toggle silently does nothing. Refused rather than repaired: stripping `" Foo"`
        would make crom disagree with the file about the feature's name."""
        for name, fault in (
            ('""', "is empty"),
            ('"   "', "is empty"),
            ('" Foo"', "whitespace"),
            ('"Foo "', "whitespace"),
            ('"A,B"', "comma"),
            ('"${CROM_PROFILE}Foo"', "interpolates a variable"),
            ('"${UNCLOSED"', "interpolates a variable"),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(CromError, fault):
                parse(MINIMAL + f"[profiles.dev]\n[profiles.dev.features]\n{name} = false\n")

    def test_a_feature_name_chrome_parameterizes_is_left_alone(self):
        """crom holds no table of Chrome's feature grammar, for the same reason it holds no
        table of which switches merge. A name it cannot break is a name it passes through."""
        scope = parse(
            MINIMAL
            + '[profiles.dev]\n[profiles.dev.features]\n"Feature:param/value" = true\n'
        )
        self.assertEqual(scope.profiles["dev"].features, {"Feature:param/value": True})

    def test_features_are_read_from_both_defaults_and_a_profile(self):
        scope = parse(
            MINIMAL
            + "[defaults.features]\nFromDefaults = false\n"
            + "[profiles.dev]\n[profiles.dev.features]\nFromProfile = true\n"
        )
        self.assertEqual(scope.default_features, {"FromDefaults": False})
        self.assertEqual(scope.profiles["dev"].features, {"FromProfile": True})

    def test_two_profiles_may_not_pin_the_same_port(self):
        with self.assertRaisesRegex(CromError, "both pin port"):
            parse(MINIMAL + "[profiles.a]\nport = 9401\n[profiles.b]\nport = 9401\n")

    def test_port_must_be_a_real_port(self):
        with self.assertRaisesRegex(CromError, "1..65535"):
            parse(MINIMAL + "[profiles.dev]\nport = 99999\n")


class ChromeSeedNameTest(unittest.TestCase):
    """`chrome:<name>` must name one directory inside Chrome's user-data-dir.

    `seed.materialize` builds the copy source as `chrome_user_data_dir() / which`, and
    `Path.__truediv__` throws away its left side when the right is absolute — so without
    this checkpoint a config could name any readable directory and have it copied into a
    profile reachable over CDP.
    """

    def test_an_absolute_path_cannot_masquerade_as_a_profile_name(self):
        with self.assertRaisesRegex(CromError, "not a profile name"):
            parse(MINIMAL + '[profiles.dev]\nseed = "chrome:/etc"\n')

    def test_a_traversal_cannot_masquerade_as_a_profile_name(self):
        # The last three are the ones that slipped an earlier version of this check:
        # `Path("/").parts` is `('/',)` — one component — and pathlib drops `.` and
        # trailing-empty components, so `../` and `./..` normalize to `('..',)` while
        # the raw string equals neither `.` nor `..`. Listed explicitly because they are
        # exactly what a reader will not re-derive.
        for which in ("../../../etc", "..", ".", "sub/dir", "~/secrets", "/", "../", "./.."):
            with self.subTest(which=which), self.assertRaisesRegex(CromError, "not a profile name"):
                parse(MINIMAL + f'[profiles.dev]\nseed = "chrome:{which}"\n')

    def test_an_empty_profile_name_is_refused_rather_than_copying_everything(self):
        # `Path('/a') / ''` is `Path('/a')`, so this would seed from the user's entire
        # Chrome directory — every profile and every cookie — instead of one profile.
        with self.assertRaisesRegex(CromError, "names no profile"):
            parse(MINIMAL + '[profiles.dev]\nseed = "chrome:"\n')

    def test_an_ordinary_profile_name_with_a_space_is_still_accepted(self):
        scope = parse(MINIMAL + '[profiles.dev]\nseed = "chrome:Profile 1"\n')
        self.assertEqual(scope.profiles["dev"].seed, SeedChrome(profile="Profile 1"))


class ChromeBinaryTest(unittest.TestCase):
    """An explicit `chrome_binary` gets the same treatment as any other path."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.source = self.root / ".crom.toml"
        self.binary = self.root / "tools" / "chrome-wrapper"
        self.binary.parent.mkdir(parents=True)
        self.binary.write_text("#!/bin/sh\n")
        self.binary.chmod(0o755)

    def _parse(self, text: str):
        return config.parse(text, self.source)

    def test_a_relative_binary_resolves_against_the_config_not_the_cwd(self):
        # The whole promise of a namespace is that `crom up myapp/dev` means the same
        # thing from anywhere; a cwd-relative binary would break exactly that.
        scope = self._parse(MINIMAL + 'chrome_binary = "./tools/chrome-wrapper"\n')
        self.assertEqual(scope.chrome_binary, self.binary)

    def test_a_missing_binary_is_refused_at_parse_time(self):
        with self.assertRaisesRegex(CromError, "does not exist"):
            self._parse(MINIMAL + 'chrome_binary = "./tools/absent"\n')

    def test_a_non_executable_binary_is_refused_at_parse_time(self):
        plain = self.root / "tools" / "notes.txt"
        plain.write_text("hi")
        plain.chmod(0o644)
        with self.assertRaisesRegex(CromError, "not executable"):
            self._parse(MINIMAL + 'chrome_binary = "./tools/notes.txt"\n')

    def test_an_empty_binary_is_refused_by_name_not_resolved(self):
        """`Path("")` is `Path(".")`, so the empty string resolves to the config's own
        directory — which exists. The `is_file()` check still refuses it, but the message
        then says a directory that is plainly there "does not exist", sending the reader
        to check the wrong fact. Same stance `state_dir` takes."""
        with self.assertRaisesRegex(CromError, "chrome_binary is empty"):
            self._parse(MINIMAL + 'chrome_binary = ""\n')


class SeedTest(unittest.TestCase):
    def test_keyword_seeds(self):
        cases = {
            "fresh": SeedFresh(),
            "default": SeedChrome(),
            "chrome:Profile 1": SeedChrome(profile="Profile 1"),
        }
        for text, expected in cases.items():
            with self.subTest(seed=text):
                scope = parse(MINIMAL + f'[profiles.dev]\nseed = "{text}"\n')
                self.assertEqual(scope.profiles["dev"].seed, expected)

    def test_path_seed_resolves_against_the_config_file(self):
        scope = parse(MINIMAL + '[profiles.dev]\nseed = "./fixtures/prof"\n')
        self.assertEqual(scope.profiles["dev"].seed, SeedPath(Path("/proj/fixtures/prof")))

    def test_a_bare_word_is_not_silently_treated_as_a_path(self):
        with self.assertRaisesRegex(CromError, "not recognised"):
            parse(MINIMAL + '[profiles.dev]\nseed = "chorme"\n')

    def test_profile_without_a_seed_falls_back_to_the_scope_default(self):
        scope = parse(MINIMAL + '[defaults]\nseed = "default"\n[profiles.dev]\n')
        self.assertIsNone(scope.profiles["dev"].seed)
        self.assertEqual(scope.default_seed, SeedChrome())

    def test_an_unstated_schema_default_is_the_one_shared_default_seed(self):
        """Asserted against the constant, not against today's value of it.

        Spelling `SeedChrome()` here would make this a fourth independent answer to
        "where does a profile's data come from" — the same duplication that let the
        project template and the user-config bootstrap drift apart in the first place.
        [LAW:one-source-of-truth]
        """
        self.assertEqual(parse(MINIMAL).default_seed, DEFAULT_SEED)

    def test_a_scope_built_without_a_seed_agrees_with_the_parsed_default(self):
        """`Scope`'s dataclass default is a sixth answer to the same question.

        `load_user_scope` builds a fileless `Scope` without `default_seed` on a machine
        with no user config, so a literal here made that scope report `fresh` while every
        file-backed scope reported `chrome` — the divergent second map `DEFAULT_SEED`
        exists to delete, sitting eighty lines below its own comment saying so.
        """
        bare = Scope(
            namespace="user",
            source=None,
            profiles_root=Path("/tmp/profiles"),
            chrome_binary=Path("/chrome"),
        )
        self.assertEqual(bare.default_seed, parse(MINIMAL).default_seed)


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def test_finds_config_in_an_ancestor_directory(self):
        (self.root / ".crom.toml").write_text(MINIMAL)
        deep = self.root / "a" / "b"
        deep.mkdir(parents=True)
        self.assertEqual(config.discover(deep), self.root / ".crom.toml")

    def test_directory_form_is_discovered(self):
        (self.root / ".crom").mkdir()
        (self.root / ".crom" / "config.toml").write_text(MINIMAL)
        self.assertEqual(config.discover(self.root), self.root / ".crom" / "config.toml")

    def test_bare_file_wins_over_directory_form(self):
        (self.root / ".crom").mkdir()
        (self.root / ".crom" / "config.toml").write_text(MINIMAL)
        (self.root / ".crom.toml").write_text(MINIMAL)
        self.assertEqual(config.discover(self.root), self.root / ".crom.toml")

    def test_nearest_config_wins_and_the_walk_stops(self):
        (self.root / ".crom.toml").write_text('namespace = "outer"\n')
        inner = self.root / "inner"
        inner.mkdir()
        (inner / ".crom.toml").write_text('namespace = "inner"\n')
        self.assertEqual(config.discover(inner), inner / ".crom.toml")


class SeedPathGuardTest(unittest.TestCase):
    """`seed` names a directory crom will copy in full, from a file that may have arrived
    with a cloned repository — the same untrusted-config threat model the `chrome:`
    vocabulary beside it was hardened against."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp()).resolve()
        self.source = self.base / ".crom.toml"

    def _parse(self, raw: str):
        return config.parse_seed(raw, "[profiles.dev]", self.source, self.base)

    def test_the_home_directory_is_refused(self):
        with self.assertRaisesRegex(CromError, "your whole home directory or filesystem"):
            self._parse("~")

    def test_the_filesystem_root_is_refused(self):
        with self.assertRaisesRegex(CromError, "your whole home directory or filesystem"):
            self._parse("/")

    def test_an_ancestor_of_home_is_refused(self):
        ancestor = str(Path.home().resolve().parent)
        with self.assertRaisesRegex(CromError, "your whole home directory or filesystem"):
            self._parse(ancestor)

    def test_an_ordinary_profile_directory_is_still_accepted(self):
        """The guard has to refuse the class without refusing the feature."""
        seed = self._parse("./local-seed")
        self.assertEqual(seed.path, self.base / "local-seed")

    def test_a_directory_inside_home_is_still_accepted(self):
        inside = Path.home().resolve() / "some" / "profile"
        self.assertEqual(self._parse(str(inside)).path, inside)


class NamespaceDiagnosisTest(unittest.TestCase):
    """Two different problems deserve two different messages."""

    def setUp(self):
        self.source = Path(tempfile.mkdtemp()).resolve() / ".crom.toml"

    def test_an_absent_namespace_says_it_is_missing(self):
        with self.assertRaisesRegex(CromError, "missing required key"):
            config.parse("[profiles.dev]\n", self.source)

    def test_a_wrong_typed_namespace_says_so_instead_of_missing(self):
        with self.assertRaisesRegex(CromError, "`namespace` must be a string, not int"):
            config.parse("namespace = 123\n", self.source)


class StateDirTest(unittest.TestCase):
    def setUp(self):
        self.source = Path(tempfile.mkdtemp()).resolve() / ".crom.toml"

    def test_an_empty_state_dir_is_refused_rather_than_ignored(self):
        """A truthy test treated this as absent and silently used the default, leaving
        someone debugging an "ignored" setting with nothing to go on. Resolving it
        instead would be worse — it lands on the config's own directory."""
        with self.assertRaisesRegex(CromError, "state_dir is empty"):
            config.parse('namespace = "myapp"\nstate_dir = ""\n', self.source)

    def test_an_absent_state_dir_still_uses_the_default(self):
        scope = config.parse('namespace = "myapp"\n', self.source)
        self.assertEqual(scope.profiles_root, default_profiles_root())


if __name__ == "__main__":
    unittest.main()
