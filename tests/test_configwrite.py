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

from crom import config, configwrite, locking
from crom.model import Conflict, CromError, ProfileSpec, SeedChrome, SeedFresh, SeedPath


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
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.target = self.root / ".crom.toml"

    def test_refusing_an_existing_file_is_the_documented_conflict(self):
        """A bare FileExistsError escaped the CLI's exit-code contract as a traceback."""
        self.target.write_text("")
        with self.assertRaises(Conflict):
            configwrite.init_project(self.target, "myapp")

    def test_only_one_of_two_concurrent_inits_writes_the_config(self):
        """The refusal is the kernel's, via O_CREAT|O_EXCL, not a check of ours.

        An `exists()` test followed by a write is check-then-act: both callers could pass
        it and the second would clobber the first's config — possibly with a different
        namespace — while both reported success.
        """
        results: list[str] = []

        def go(namespace: str):
            try:
                configwrite.init_project(self.target, namespace)
                results.append("wrote")
            except Conflict:
                results.append("refused")

        threads = [threading.Thread(target=go, args=(n,)) for n in ("alpha", "beta")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(results), ["refused", "wrote"])
        # And the file belongs wholly to the winner — not a mix of both templates.
        written = self.target.read_text()
        self.assertEqual(written.count("namespace = "), 1)


class LockingTest(unittest.TestCase):
    def test_an_unusable_lock_path_is_reported_not_crashed_through(self):
        """`exclusive` sits under nearly every command, so a raw OSError here would
        escape as a traceback from all of them."""
        root = Path(tempfile.mkdtemp())
        blocker = root / "file"
        blocker.write_text("not a directory")

        with self.assertRaisesRegex(CromError, "could not take the lock"):
            with locking.exclusive(blocker / "child" / "target"):
                pass


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
                configwrite.add_profile(
                    target,
                    ProfileSpec(name=name, seed=SeedFresh()),
                    header='namespace = "myapp"\n',
                )
            except BaseException as e:  # surfaced below rather than dying in the thread
                errors.append(e)

        with mock.patch.object(configwrite, "_save", slow_save):
            threads = [threading.Thread(target=declare, args=(n,)) for n in ("ci", "staging")]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(errors, [])
        # Round-tripped through the parser rather than checked for substrings. Asserting
        # that the text contains `[profiles.ci]` says nothing about whether crom can read
        # the file back, so this test previously produced a config with no `namespace`
        # key — one every later command would reject — and reported success.
        scope = config.parse(target.read_text(), target)
        self.assertEqual(sorted(scope.profiles), ["ci", "staging"])
        self.assertEqual(scope.namespace, "myapp")




class HeaderInvariantTest(unittest.TestCase):
    """Creating a config without a header produces a file crom cannot read back.

    The document would have no `namespace` key, so `config.parse` rejects it wholesale
    on the next load — every command in that project failing on a file crom itself just
    wrote, with no command left to repair it.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.target = self.root / ".crom.toml"

    def test_creating_a_config_without_a_header_is_refused(self):
        with self.assertRaisesRegex(CromError, "will not create a config without a header"):
            configwrite.add_profile(self.target, ProfileSpec(name="ci", seed=SeedFresh()))
        self.assertFalse(self.target.exists())

    def test_the_same_refusal_applies_to_the_converging_twin(self):
        """`ensure_profile` is the path migration takes, so it needs the rule too."""
        with self.assertRaisesRegex(CromError, "will not create a config without a header"):
            configwrite.ensure_profile(self.target, ProfileSpec(name="ci", seed=SeedFresh()))
        self.assertFalse(self.target.exists())

    def test_an_existing_file_needs_no_header(self):
        """The header is for creation only — an existing file owns its own preamble, and
        appending to one must never duplicate or displace it."""
        self.target.write_text('namespace = "myapp"\n')
        configwrite.add_profile(self.target, ProfileSpec(name="ci", seed=SeedFresh()))

        scope = config.parse(self.target.read_text(), self.target)
        self.assertEqual(sorted(scope.profiles), ["ci"])
        self.assertEqual(self.target.read_text().count("namespace = "), 1)

    def test_a_created_config_round_trips_through_the_parser(self):
        configwrite.add_profile(
            self.target,
            ProfileSpec(name="ci", seed=SeedFresh()),
            header='namespace = "myapp"\n',
        )
        scope = config.parse(self.target.read_text(), self.target)
        self.assertEqual(scope.namespace, "myapp")
        self.assertEqual(sorted(scope.profiles), ["ci"])


if __name__ == "__main__":
    unittest.main()
