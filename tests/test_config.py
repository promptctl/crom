"""Tests for the config checkpoint: what it accepts, and what it refuses to accept."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crom import config
from crom.model import Conflict, CromError, SeedChrome, SeedFresh, SeedPath

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
    def test_profiles_inherit_default_flags_before_their_own(self):
        scope = parse(MINIMAL + '[defaults]\nflags = ["--a"]\n[profiles.dev]\nflags = ["--b"]\n')
        self.assertEqual(scope.default_flags, ("--a",))
        self.assertEqual(scope.profiles["dev"].flags, ("--b",))

    def test_unknown_keys_are_refused_rather_than_ignored(self):
        with self.assertRaisesRegex(CromError, "unknown key"):
            parse(MINIMAL + "[profiles.dev]\nfalgs = []\n")

    def test_reserved_switches_are_refused(self):
        for flag in ("--user-data-dir=/tmp/x", "--remote-debugging-port=1234"):
            with self.subTest(flag=flag), self.assertRaisesRegex(CromError, "crom owns it"):
                parse(MINIMAL + f'[profiles.dev]\nflags = ["{flag}"]\n')

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


class SeedTest(unittest.TestCase):
    def test_keyword_seeds(self):
        cases = {
            "fresh": SeedFresh(),
            "chrome": SeedChrome(),
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
        scope = parse(MINIMAL + '[defaults]\nseed = "chrome"\n[profiles.dev]\n')
        self.assertIsNone(scope.profiles["dev"].seed)
        self.assertEqual(scope.default_seed, SeedChrome())

    def test_the_schema_default_is_fresh(self):
        self.assertEqual(parse(MINIMAL).default_seed, SeedFresh())


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


if __name__ == "__main__":
    unittest.main()
