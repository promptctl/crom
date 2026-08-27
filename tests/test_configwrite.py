"""Tests for editing config files in place — the half of crom that writes what a human owns.

Two concerns dominate: an edit must survive a concurrent one (several agents at once is
the case crom exists for), and what crom writes back must be readable by crom's own
parser, in the spelling the author would have used.
"""

import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from crom import config, configwrite, locking
from crom.model import (
    DEFAULT_SEED,
    CromError,
    NotFound,
    ProfileSpec,
    SeedChrome,
    SeedFresh,
    SeedPath,
)


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
        self.assertEqual(configwrite.render_seed(SeedChrome(), self.base), "default")
        self.assertEqual(configwrite.render_seed(SeedChrome(profile="Work"), self.base), "chrome:Work")


class WriteDefaultTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.target = self.root / ".crom.toml"

    def test_an_existing_file_is_left_alone_and_reported_as_not_written(self):
        """The bool is the whole contract: `crom init` turns it into a verb, and the
        repair paths in `config` and `resolve` ignore it. What none of them may get is a
        clobbered file, so the existing bytes must survive untouched."""
        self.target.write_text("mine\n")

        self.assertFalse(
            configwrite.write_default(self.target, namespace="myapp", seed=DEFAULT_SEED)
        )
        self.assertEqual(self.target.read_text(), "mine\n")

    def test_only_one_of_two_concurrent_inits_writes_the_config(self):
        """The refusal is the kernel's, via O_CREAT|O_EXCL, not a check of ours.

        An `exists()` test followed by a write is check-then-act: both callers could pass
        it and the second would clobber the first's config — possibly with a different
        namespace — while both reported True.
        """
        results: list[bool] = []

        def go(namespace: str):
            results.append(configwrite.write_default(self.target, namespace=namespace, seed=DEFAULT_SEED))

        threads = [threading.Thread(target=go, args=(n,)) for n in ("alpha", "beta")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(results), [False, True])
        # And the file belongs wholly to the winner — not a mix of both templates.
        written = self.target.read_text()
        self.assertEqual(written.count("namespace = "), 1)

    def test_the_value_at_a_key_the_file_does_not_have_is_none(self):
        """`crom init`'s convergence reads two facts back out of an existing file, and a
        hand-written config need not carry either. A walk that assumed each level was a
        table would raise `AttributeError` from a command whose job is to report that the
        project is already configured."""
        self.target.write_text('namespace = "myapp"\nprofiles = "typo"\n')

        self.assertEqual(configwrite.value_at(self.target, "namespace"), "myapp")
        self.assertIsNone(configwrite.value_at(self.target, "defaults", "seed"))
        self.assertIsNone(configwrite.value_at(self.target, "profiles", "ci"))

    def test_the_value_at_a_key_is_what_the_written_template_holds(self):
        """Read back through `value_at` rather than compared to a spelling: `crom init`
        reports these two values as the project's own, so what it reads must be what the
        template writes."""
        configwrite.write_default(self.target, namespace="myapp", seed=SeedFresh())

        self.assertEqual(configwrite.value_at(self.target, "namespace"), "myapp")
        self.assertEqual(configwrite.value_at(self.target, "defaults", "seed"), "fresh")


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

    def test_a_filesystem_that_refuses_locking_is_reported_not_crashed_through(self):
        """The guarantee has to cover acquiring the lock, not only creating the file.

        Advisory locking is not universal — a network or FUSE mount under a relocated
        XDG_STATE_HOME can refuse it, and ENOLCK is reachable under resource exhaustion.
        `exclusive` sits under nearly every command, so a raw OSError from `flock` escapes
        as a traceback exactly as one from `open` did.
        """
        root = Path(tempfile.mkdtemp())
        with mock.patch("crom.locking.fcntl.flock", side_effect=OSError("ENOLCK")):
            with self.assertRaisesRegex(CromError, "could not take the lock"):
                with locking.exclusive(root / "target"):
                    pass

    def test_a_failure_to_unlock_does_not_replace_the_real_error(self):
        """Closing the descriptor releases the lock on every path out, so an explicit
        LOCK_UN that fails changes nothing — while raising from the `finally` would
        discard whatever exception the body was already raising."""
        root = Path(tempfile.mkdtemp())
        import fcntl as real_fcntl

        def flock(handle, operation):
            if operation == real_fcntl.LOCK_UN:
                raise OSError("cannot release")
            return None

        with mock.patch("crom.locking.fcntl.flock", side_effect=flock):
            with self.assertRaisesRegex(RuntimeError, "the real failure"):
                with locking.exclusive(root / "target"):
                    raise RuntimeError("the real failure")


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
                configwrite.ensure_profile(
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
            configwrite.ensure_profile(self.target, ProfileSpec(name="ci", seed=SeedFresh()))
        self.assertFalse(self.target.exists())

    def test_an_existing_file_needs_no_header(self):
        """The header is for creation only — an existing file owns its own preamble, and
        appending to one must never duplicate or displace it."""
        self.target.write_text('namespace = "myapp"\n')
        configwrite.ensure_profile(self.target, ProfileSpec(name="ci", seed=SeedFresh()))

        scope = config.parse(self.target.read_text(), self.target)
        self.assertEqual(sorted(scope.profiles), ["ci"])
        self.assertEqual(self.target.read_text().count("namespace = "), 1)

    def test_a_created_config_round_trips_through_the_parser(self):
        configwrite.ensure_profile(
            self.target,
            ProfileSpec(name="ci", seed=SeedFresh()),
            header='namespace = "myapp"\n',
        )
        scope = config.parse(self.target.read_text(), self.target)
        self.assertEqual(scope.namespace, "myapp")
        self.assertEqual(sorted(scope.profiles), ["ci"])




class MalformedConfigTest(unittest.TestCase):
    """A config crom cannot read must produce an error, not a traceback.

    These paths run *before* `config.parse` ever validates the file:
    `cli.main` calls `_bootstrap_user_config()` on every invocation, which reaches
    `_declare` → `_load` and `declares` without a scope being loaded. So anything raw
    escaping here came back on every command, including the ones a user would reach for
    to repair the file.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.target = self.root / "config.toml"

    def test_unparseable_toml_is_reported_against_the_file(self):
        self.target.write_text("this is not = = valid toml [[[")
        with self.assertRaisesRegex(CromError, "cannot be read as TOML"):
            configwrite.declares(self.target, "ci")

    def test_a_profiles_key_that_is_not_a_table_is_refused_when_declaring(self):
        """`setdefault` returns the existing value, so the later item assignment raised a
        raw TypeError rather than a CromError."""
        self.target.write_text('namespace = "myapp"\nprofiles = "typo"\n')
        with self.assertRaisesRegex(CromError, "`profiles` must be a table"):
            configwrite.ensure_profile(self.target, ProfileSpec(name="ci", seed=SeedFresh()))

    def test_a_profiles_key_that_is_not_a_table_cannot_answer_declares(self):
        """The quieter half of the same defect: `name in "typo"` is a *substring* test.

        It raises nothing and answers True for a profile nobody declared — `declares` is
        what `add_cmd` uses to decide a name is taken and what its cleanup path uses to
        decide whether it lost a race, so a wrong answer here is worse than a crash.
        """
        self.target.write_text('namespace = "myapp"\nprofiles = "typo"\n')
        with self.assertRaisesRegex(CromError, "`profiles` must be a table"):
            configwrite.declares(self.target, "yp")  # a substring of "typo"

    def test_a_profiles_key_that_is_not_a_table_is_refused_when_removing(self):
        """The third reader of the same key, which the shared helper originally missed.

        `rm_cmd` keeps its `click.confirm` prompt outside the lock deliberately, so this
        read happens an unbounded time after the one that validated the file — the window
        in which another agent can rewrite it.
        """
        self.target.write_text('namespace = "myapp"\nprofiles = "typo"\n')
        with self.assertRaisesRegex(CromError, "`profiles` must be a table"):
            configwrite.remove_profile(self.target, "yp")

    def test_removing_from_a_file_with_no_profiles_is_still_not_found(self):
        """Routing through the shared helper must not turn "absent" into "malformed"."""
        self.target.write_text('namespace = "myapp"\n')
        with self.assertRaises(NotFound):
            configwrite.remove_profile(self.target, "ci")


class WriteFailureTest(unittest.TestCase):
    """A filesystem that refuses the write is a `CromError`, not a traceback.

    `CromGroup.invoke` catches only `CromError`, so a full disk or a read-only mount used
    to escape the CLI's exit-code contract entirely. `migrate.run` reaches `_save` through
    `ensure_profile` before anything else in `main`, which made that traceback every
    command rather than one.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.target = self.root / "config.toml"

    def test_a_failing_write_is_reported_against_the_file(self):
        self.target.write_text('namespace = "myapp"\n')
        with mock.patch.object(
            Path, "write_text", side_effect=OSError(28, "No space left on device")
        ):
            with self.assertRaisesRegex(CromError, "No space left on device"):
                configwrite.ensure_profile(self.target, ProfileSpec(name="ci", seed=SeedFresh()))

    def test_a_declaration_that_already_exists_is_a_reported_no_op(self):
        """Not a failure, and not silent either: the bool is how `crom add` chooses
        between "Declared" and "Already declared", and how it knows the port reservation
        belongs to a declaration that is already on disk rather than to a write of its own
        that must be rolled back. A second call must also leave the first's stanza intact
        rather than rewriting it with the new spec."""
        configwrite.ensure_profile(
            self.target, ProfileSpec(name="ci", seed=SeedFresh()), header='namespace = "m"\n'
        )
        before = self.target.read_text()

        self.assertFalse(
            configwrite.ensure_profile(self.target, ProfileSpec(name="ci", seed=SeedChrome()))
        )
        self.assertEqual(self.target.read_text(), before)

    def test_init_project_reports_an_unwritable_directory(self):
        """`os.open` raises `PermissionError`, which is an `OSError` but not a
        `FileExistsError`, so it fell straight through the narrow collision handler."""
        with mock.patch("crom.configwrite.os.open", side_effect=PermissionError(13, "denied")):
            with self.assertRaisesRegex(CromError, "denied"):
                configwrite.write_default(self.target, namespace="myapp", seed=DEFAULT_SEED)

    def test_init_project_reports_a_failing_mkdir(self):
        """The `mkdir` sat above the `try` entirely — not merely unconverted, but outside
        the block written to handle this call's failures."""
        with mock.patch.object(Path, "mkdir", side_effect=PermissionError(13, "denied")):
            with self.assertRaisesRegex(CromError, "denied"):
                configwrite.write_default(self.root / "sub" / "config.toml", namespace="myapp", seed=DEFAULT_SEED)

    def test_the_template_records_the_seed_it_was_given(self):
        """`[defaults].seed` is written from the caller's value, never a literal.

        The template used to spell `fresh` itself while `cli._bootstrap_user_config`
        spelled `SeedChrome()`, so the same word `default` meant a copy of the user's
        real browser outside a project and an empty one inside it. Rendering whatever it
        is handed is what keeps the two templates from disagreeing again.
        """
        configwrite.write_default(self.target, namespace="myapp", seed=SeedChrome(profile="Work"))
        self.assertIn('seed = "chrome:Work"', self.target.read_text())

    def test_a_seed_containing_a_quote_is_written_as_readable_toml(self):
        """The template interpolates into a TOML document, so the value must be escaped.

        `chrome:<Profile Name>` accepts any Chrome profile name — only `/`, `~` and path
        components are refused — so a quote reaches here. Interpolated into a hand-written
        `seed = "{seed}"` it produced `seed = "chrome:My"Work"`: `crom init` exited 0
        reporting success, and every later command in that directory died on invalid
        TOML, including the ones that could have repaired it. The assertion is that the
        file parses back to the value we put in, not that it has some particular spelling
        — escaping is tomlkit's business, and pinning its output would be a second copy
        of a rule it already owns.
        """
        seed = SeedChrome(profile='My"Work')
        configwrite.write_default(self.target, namespace="myapp", seed=seed)

        # Read back through crom's own parser, which is the property that actually
        # matters: the config crom writes is one crom can load on the next command.
        scope = config.parse(self.target.read_text(), self.target)
        self.assertEqual(scope.default_seed, seed)

    def test_a_diagnosis_from_inside_the_write_keeps_its_own_message(self):
        """`_writing` translates `OSError` into a `CromError` naming the file. A
        `CromError` raised by something nested inside it is already the precise message,
        and rewording it as a filesystem failure would send the reader to check the wrong
        fact — so it must pass through untouched."""
        with mock.patch.object(Path, "mkdir", side_effect=CromError("the precise reason")):
            with self.assertRaisesRegex(CromError, "^the precise reason$"):
                configwrite.write_default(self.target, namespace="myapp", seed=DEFAULT_SEED)


if __name__ == "__main__":
    unittest.main()
