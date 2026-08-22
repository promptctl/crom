"""Tests for the config checkpoint: what it accepts, and what it refuses to accept."""

import tempfile
import unittest
from pathlib import Path

from crom import config
from crom.model import CromError, SeedChrome, SeedFresh, SeedPath

SOURCE = Path("/proj/.crom.toml")

MINIMAL = 'namespace = "myapp"\n'


def parse(text: str, **kwargs):
    return config.parse(text, SOURCE, **kwargs)


class NamespaceTest(unittest.TestCase):
    def test_namespace_is_required(self):
        with self.assertRaisesRegex(CromError, "namespace"):
            parse("[profiles.dev]\n")

    def test_user_namespace_is_reserved(self):
        with self.assertRaisesRegex(CromError, "reserved"):
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
