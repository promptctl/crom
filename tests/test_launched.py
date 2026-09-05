"""Tests for the launch record — the one fact about a running browser `ps` cannot supply.

Chrome rewrites its own argv, so the flags crom launched with are knowable only at the
moment crom spends them. Everything here is about that written-down fact surviving to a
later invocation intact, and about the two ways it can be missing staying
distinguishable: a directory crom never launched from and a record crom cannot use are
both `Unknown`, and a user is told which one they have.
"""

import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from crom import launched
from crom.model import ProfileRef, ResolvedProfile, SeedFresh
from crom.resolve import build_argv


def profile(profile_dir: Path, *flags: str, env: dict[str, str] | None = None) -> ResolvedProfile:
    return ResolvedProfile(
        ref=ProfileRef("myapp", "dev"),
        port=9300,
        profile_dir=profile_dir,
        chrome_binary=Path("/chrome"),
        argv=build_argv(Path("/chrome"), profile_dir, 9300, flags),
        env=env or {},
        seed=SeedFresh(),
        source=None,
    )


class RoundTripTest(unittest.TestCase):
    """What a later crom gets back, which is the whole point of writing anything down."""

    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.profile_dir = root / "myapp" / "dev"
        self.profile_dir.mkdir(parents=True)

    def test_a_recorded_launch_reads_back_equal_to_the_one_that_was_launched(self):
        """Equality, not similarity: a drift check is built on `==` and nothing else.

        JSON has no tuples, so a reader that hands back what `json.loads` gave it returns
        `["--flag"]` where `Launch.of` produces `("--flag",)`. Every field would still be
        *there*, every value would still be right, and every comparison against the current
        resolution would report a difference that is not there — a browser relaunched on
        every `crom up`, forever.
        """
        spec = profile(self.profile_dir, "--disable-extensions")
        entry = launched.Launch.of(spec)

        launched.record(self.profile_dir, entry)

        self.assertEqual(launched.read(self.profile_dir), entry)
        self.assertEqual(launched.read(self.profile_dir), launched.Launch.of(spec))

    def test_the_env_a_profile_launches_with_is_recorded_beside_its_flags(self):
        """`env` is layered configuration exactly as `flags` is, so it drifts the same way.

        Two profiles differing only in `env` launch two different browsers. A record that
        kept the flags alone would read as identical across both, and an `env` a user
        edited would be a configuration change no later crom could ever notice.
        """
        quiet = launched.Launch.of(profile(self.profile_dir, env={"CHROME_LOG_FILE": "/dev/null"}))
        loud = launched.Launch.of(profile(self.profile_dir, env={"CHROME_LOG_FILE": "/tmp/loud"}))
        self.assertNotEqual(quiet, loud)

        launched.record(self.profile_dir, loud)

        self.assertEqual(launched.read(self.profile_dir), loud)
        self.assertNotEqual(launched.read(self.profile_dir), quiet)

    def test_a_second_launch_replaces_what_the_first_one_recorded(self):
        """The record answers "how was the browser that is running now launched", singular."""
        launched.record(self.profile_dir, launched.Launch.of(profile(self.profile_dir, "--old")))
        current = launched.Launch.of(profile(self.profile_dir, "--new"))

        launched.record(self.profile_dir, current)

        self.assertEqual(launched.read(self.profile_dir), current)

    def test_a_directory_crom_has_not_launched_from_reports_that_it_does_not_know(self):
        """Not an empty `Launch`: a browser launched with no flags is a thing that exists."""
        answer = launched.read(self.profile_dir)

        self.assertIsInstance(answer, launched.Unknown)
        self.assertIn(str(self.profile_dir), answer.why)

    def test_a_record_is_written_where_crom_rm_will_take_it(self):
        """Inside the user-data-dir, which is what makes its lifetime need no rule of its own."""
        launched.record(self.profile_dir, launched.Launch.of(profile(self.profile_dir)))

        self.assertEqual(
            [entry.name for entry in self.profile_dir.iterdir()], [launched.FILENAME]
        )


class UnusableRecordTest(unittest.TestCase):
    """A record crom cannot use, which must not be mistaken for a launch.

    Every one of these is a file crom itself wrote and can write again, so none of them
    stops a command: `read` hands back `Unknown` rather than raising, and the profile's
    next successful launch replaces the file. What a refusal would cost is a chore handed
    to the user — delete this, then run your command again — falling hardest on `crom up`,
    which is the command that would have fixed it.
    """

    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.profile_dir = root / "myapp" / "dev"
        self.profile_dir.mkdir(parents=True)
        self.record = launched.path(self.profile_dir)

    def test_a_record_crom_cannot_understand_is_never_read_as_a_launch(self):
        good = {"version": launched.SCHEMA_VERSION, "argv": ["/chrome"], "env": {}}
        damaged = {
            "truncated mid-write": "{\"version\": 1, \"argv\": [",
            "a JSON list rather than an object": "[]",
            "written by a crom speaking a later schema": json.dumps({**good, "version": 2}),
            "carrying no version at all": json.dumps({"argv": ["/chrome"], "env": {}}),
            "missing its argv": json.dumps({k: v for k, v in good.items() if k != "argv"}),
            "carrying an argv that is not a list": json.dumps({**good, "argv": "/chrome"}),
            "carrying a non-string among its argv": json.dumps({**good, "argv": ["/chrome", 9300]}),
            "missing its env": json.dumps({k: v for k, v in good.items() if k != "env"}),
            "carrying an env that is not a table": json.dumps({**good, "env": []}),
            "carrying a non-string env value": json.dumps({**good, "env": {"PORT": 9300}}),
            "naming one switch twice": json.dumps({**good, "argv": ["/chrome", "--f=1", "--f=2"]}),
            "repeating a valueless switch": json.dumps({**good, "argv": ["/chrome", "--x", "--x"]}),
        }
        for described, content in damaged.items():
            with self.subTest(described):
                self.record.write_text(content)

                answer = launched.read(self.profile_dir)

                self.assertIsInstance(answer, launched.Unknown)
                self.assertIn(str(self.record), answer.why)

    def test_a_switch_named_twice_is_refused_rather_than_quietly_collapsed(self):
        """A duplicate is not a launch crom composed, and a consumer keying by switch
        cannot see it: `drift` compares by switch name, so a collapsed duplicate would let
        two different launches read as one — and be explained as a reordering, which is a
        sentence about a file that was corrupt, not reordered. The invariant is the
        type's, so no consumer has to assert it. [LAW:parse-dont-validate]
        """
        self.record.write_text(
            json.dumps(
                {"version": launched.SCHEMA_VERSION, "argv": ["/chrome", "--f=1", "--f=2"], "env": {}}
            )
        )

        answer = launched.read(self.profile_dir)

        self.assertIsInstance(answer, launched.Unknown)
        self.assertIn("more than once", answer.why)

    def test_two_switches_sharing_a_prefix_are_not_one_switch_named_twice(self):
        """Split at the first `=`, which is `Flag`'s rule — so `--f` and `--f-g` are two.

        Read through the same parser the comparison uses, so the border and the consumer
        cannot come to disagree about where a switch ends. [LAW:one-source-of-truth]
        """
        launched.record(self.profile_dir, launched.Launch(("/chrome", "--f=1", "--f-g=2"), {}))

        self.assertIsInstance(launched.read(self.profile_dir), launched.Launch)

    def test_a_record_with_no_argv_at_all_is_not_read_as_a_repeated_switch(self):
        """The uniqueness check spans `argv[1:]`, which is empty here rather than negative.

        Hand-edited down to nothing is a shape `drift` handles — it reports an executable
        the current side has and the record does not — so this must not be diverted into
        the duplicate arm on the way. Total by construction, not by a guard.
        """
        launched.record(self.profile_dir, launched.Launch((), {}))

        self.assertEqual(launched.read(self.profile_dir), launched.Launch((), {}))

    def test_a_record_crom_cannot_open_is_not_a_traceback(self):
        """`read` promises not to raise, and the filesystem is the half it does not author."""
        self.record.mkdir()

        answer = launched.read(self.profile_dir)

        self.assertIsInstance(answer, launched.Unknown)
        self.assertIn(str(self.record), answer.why)

    def test_a_damaged_record_and_a_missing_one_are_told_apart(self):
        """One type, because nothing acts differently on the two — and two sentences,
        because the user does: one of them has a file sitting there that crom wrote."""
        missing = launched.read(self.profile_dir)
        self.record.write_text("{")
        damaged = launched.read(self.profile_dir)

        self.assertNotEqual(missing.why, damaged.why)
        self.assertNotIn(str(self.record), missing.why)
        self.assertIn(str(self.record), damaged.why)
        self.assertIn("next launch", damaged.why)


class RecordFailureTest(unittest.TestCase):
    """A launch that came up and a record that could not be written down.

    The browser is already running by then, so the failure is reported rather than raised:
    a raise would tell the user the launch failed while their Chrome sat there running,
    which is a worse answer than the one this loses.
    """

    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.profile_dir = root / "myapp" / "dev"
        self.profile_dir.mkdir(parents=True)
        self.said: list[str] = []

    def test_a_record_that_cannot_be_written_is_reported_rather_than_raised(self):
        os.chmod(self.profile_dir, stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, self.profile_dir, stat.S_IRWXU)

        launched.record(
            self.profile_dir, launched.Launch.of(profile(self.profile_dir)), log=self.said.append
        )

        self.assertEqual(len(self.said), 1)
        self.assertIn(str(launched.path(self.profile_dir)), self.said[0])

    def test_a_failed_record_leaves_no_half_written_file_behind(self):
        """Debris in the user-data-dir is Chrome's problem to trip over, not crom's to leave.

        A directory standing where the record belongs is the reachable way to fail *after*
        the staging file exists — the replace has to be the step that refuses, or the
        cleanup this pins is never exercised.
        """
        launched.path(self.profile_dir).mkdir()

        launched.record(
            self.profile_dir, launched.Launch.of(profile(self.profile_dir)), log=self.said.append
        )

        self.assertEqual(len(self.said), 1)
        self.assertEqual(
            [entry.name for entry in self.profile_dir.iterdir()], [launched.FILENAME]
        )
