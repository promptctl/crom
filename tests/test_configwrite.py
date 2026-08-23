"""Tests for editing config files in place — the half of crom that writes what a human owns.

Two concerns dominate: an edit must survive a concurrent one (several agents at once is
the case crom exists for), and what crom writes back must be readable by crom's own
parser, in the spelling the author would have used.
"""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from crom import config, configwrite
from crom.model import Conflict, ProfileSpec, SeedChrome, SeedFresh, SeedPath


class RenderSeedTest(unittest.TestCase):
    """`render_seed` is the inverse of `config.parse_seed`, including for paths.

    `parse_seed` absolutizes every path seed against the config's directory, so rendering
    the resolved path verbatim would bake one machine's layout into a file the README
    expects to be committed and shared.
    """

    def setUp(self):
        self.base = Path(tempfile.mkdtemp()).resolve()

    def test_a_path_under_the_config_is_written_back_relative(self):
        seed = SeedPath(self.base / "local-seed")
        self.assertEqual(configwrite.render_seed(seed, self.base), "./local-seed")

    def test_a_rendered_path_parses_back_to_the_same_seed(self):
        source = self.base / ".crom.toml"
        original = SeedPath(self.base / "local-seed")
        rendered = configwrite.render_seed(original, self.base)
        self.assertEqual(config.parse_seed(rendered, "[profiles.dev]", source, self.base), original)

    def test_a_path_outside_the_config_stays_absolute(self):
        # `../` chains out of the project would be portable in form and wrong in meaning.
        outside = Path("/somewhere/else/seed")
        self.assertEqual(configwrite.render_seed(SeedPath(outside), self.base), str(outside))

    def test_the_other_seed_spellings_are_unchanged(self):
        self.assertEqual(configwrite.render_seed(SeedFresh(), self.base), "fresh")
        self.assertEqual(configwrite.render_seed(SeedChrome(), self.base), "chrome")
        self.assertEqual(configwrite.render_seed(SeedChrome(profile="Work"), self.base), "chrome:Work")


class InitProjectTest(unittest.TestCase):
    def test_refusing_an_existing_file_is_the_documented_conflict(self):
        """A bare FileExistsError escaped the CLI's exit-code contract as a traceback.

        `crom init`'s own existence check is check-then-act, so this raise is what covers
        the window where another process wins the race.
        """
        root = Path(tempfile.mkdtemp())
        target = root / ".crom.toml"
        target.write_text("")
        with self.assertRaises(Conflict):
            configwrite.init_project(target, "myapp")


class ConcurrentDeclareTest(unittest.TestCase):
    """Two `crom add` calls against one config must not lose each other's profile.

    The threads take separate file descriptors, so `fcntl.flock` serializes them exactly
    as it would two processes. `_save` is slowed to force the interleaving that the old
    unlocked read-modify-write lost a profile to; without the lock the later writer
    reinstates the document it read before the earlier one wrote.
    """

    def test_neither_of_two_concurrent_declarations_is_lost(self):
        root = Path(tempfile.mkdtemp())
        target = root / ".crom.toml"
        real_save = configwrite._save

        def slow_save(path, doc):
            time.sleep(0.05)
            real_save(path, doc)

        errors: list[BaseException] = []

        def declare(name: str):
            try:
                configwrite.add_profile(target, ProfileSpec(name=name, seed=SeedFresh()))
            except BaseException as e:  # surfaced below rather than dying in the thread
                errors.append(e)

        with mock.patch.object(configwrite, "_save", slow_save):
            threads = [threading.Thread(target=declare, args=(n,)) for n in ("ci", "staging")]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(errors, [])
        written = target.read_text()
        self.assertIn("[profiles.ci]", written)
        self.assertIn("[profiles.staging]", written)


if __name__ == "__main__":
    unittest.main()
