"""Tests for the drift verdict — whether a running browser is still the config's browser.

The property under test throughout is that the verdict is decided by comparing whole
launches and only *reported* by naming entries. So these drive the four verdicts through
their real inputs — a record on disk and a resolved profile — rather than asserting on the
diff, and the naming tests all sit downstream of a verdict that was already `drifted`.

The one answer this file exists to make impossible is a quiet `matches`: a browser running
flags its config stopped asking for, reported as current because the comparison could not
see the layer that moved.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from crom import drift, launched
from crom.model import ProfileRef, ResolvedProfile, SeedFresh
from crom.resolve import build_argv

RUNNING = (4242,)
NOTHING: tuple[int, ...] = ()


def profile(
    profile_dir: Path,
    *flags: str,
    env: dict[str, str] | None = None,
    binary: str = "/chrome",
) -> ResolvedProfile:
    """A profile resolved exactly as `resolve` would resolve it, framed by `build_argv`.

    Through the real framer rather than a hand-written argv, so these tests compare the
    same shape a launch produces — crom's own `--user-data-dir` and `--remote-debugging-port`
    included, which is what makes a repointed `state_dir` or a re-pinned port drift here
    and not merely in principle.
    """
    return ResolvedProfile(
        ref=ProfileRef("myapp", "dev"),
        port=9300,
        profile_dir=profile_dir,
        chrome_binary=Path(binary),
        argv=build_argv(Path(binary), profile_dir, 9300, flags),
        env=env or {},
        seed=SeedFresh(),
        source=None,
    )


class DriftTestCase(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.profile_dir = root / "myapp" / "dev"
        self.profile_dir.mkdir(parents=True)

    def launched_with(self, *flags: str, **kwargs) -> None:
        """Record a launch the way `chrome.launch` records one: through `launched`."""
        launched.record(self.profile_dir, launched.Launch.of(profile(self.profile_dir, *flags, **kwargs)))

    def subjects(self, verdict: drift.Verdict) -> list[str]:
        return [change.subject for change in verdict.changes]


class VerdictTest(DriftTestCase):
    """The four answers, and which evidence produces which."""

    def test_a_browser_launched_with_this_configuration_matches_it(self):
        self.launched_with("--window-size=800,600")

        verdict = drift.of(profile(self.profile_dir, "--window-size=800,600"), RUNNING)

        self.assertIsInstance(verdict, drift.Matches)
        self.assertEqual(verdict.changes, ())

    def test_a_browser_launched_before_the_config_changed_is_drifted(self):
        """The whole point: an edit crom cannot otherwise notice, noticed."""
        self.launched_with("--window-size=800,600")

        verdict = drift.of(profile(self.profile_dir, "--window-size=1280,800"), RUNNING)

        self.assertIsInstance(verdict, drift.Drifted)
        self.assertEqual(self.subjects(verdict), ["--window-size"])

    def test_a_profile_with_nothing_running_is_not_compared_against_its_old_record(self):
        """`crom down` leaves the record behind, and a stopped profile is not stale.

        The record outlives the browser by design — it lives in the user-data-dir, so
        only `crom rm` takes it. A comparison that ignored liveness would therefore
        report every stopped profile whose config has since been edited as drifted,
        which is a browser that does not exist described as running the wrong flags.
        The next `crom up` launches from the current config, which is what `Stopped`
        says.
        """
        self.launched_with("--window-size=800,600")

        verdict = drift.of(profile(self.profile_dir, "--window-size=1280,800"), NOTHING)

        self.assertIsInstance(verdict, drift.Stopped)
        self.assertEqual(verdict.changes, ())

    def test_a_running_browser_crom_never_recorded_is_unmeasured_not_matching(self):
        """No record is "crom cannot tell", never "nothing changed". [LAW:no-silent-failure]

        Reachable on any browser started by a crom older than the record, and the verdict
        `crom up` will act on. Answered `matches`, that browser keeps flags its config
        stopped asking for and crom reports it checked.
        """
        verdict = drift.of(profile(self.profile_dir, "--window-size=800,600"), RUNNING)

        self.assertIsInstance(verdict, drift.Unmeasured)
        self.assertIn(str(self.profile_dir), verdict.finding)

    def test_a_record_crom_cannot_read_is_unmeasured_and_says_so(self):
        """The damaged arm reaches the reader with `launched`'s own sentence, unreworded."""
        self.launched_with("--window-size=800,600")
        launched.path(self.profile_dir).write_text("{ not json")

        verdict = drift.of(profile(self.profile_dir, "--window-size=800,600"), RUNNING)

        self.assertIsInstance(verdict, drift.Unmeasured)
        self.assertEqual(verdict.finding, launched.read(self.profile_dir).why)


class NamingTest(DriftTestCase):
    """What a drifted verdict says, once the verdict itself is settled."""

    def test_a_changed_switch_is_one_subject_carrying_both_of_its_values(self):
        """Keyed by switch, not by position: one answer moved, not one gone and one new.

        A positional diff reports `--window-size=800,600` removed and
        `--window-size=1280,800` added — two findings for one edit, and neither of them
        able to say what the other was.
        """
        self.launched_with("--window-size=800,600")

        verdict = drift.of(profile(self.profile_dir, "--window-size=1280,800"), RUNNING)

        (change,) = verdict.changes
        self.assertEqual(change.subject, "--window-size")
        self.assertEqual(change.launched, "--window-size=800,600")
        self.assertEqual(change.resolves, "--window-size=1280,800")
        self.assertIn("--window-size", verdict.finding)

    def test_a_switch_added_and_a_switch_dropped_each_name_the_side_that_has_nothing(self):
        self.launched_with("--no-pings")

        verdict = drift.of(profile(self.profile_dir, "--incognito"), RUNNING)

        self.assertEqual(self.subjects(verdict), ["--incognito", "--no-pings"])
        added, dropped = verdict.changes
        self.assertEqual((added.launched, added.resolves), (None, "--incognito"))
        self.assertEqual((dropped.launched, dropped.resolves), ("--no-pings", None))
        self.assertEqual(str(dropped), "--no-pings: launched --no-pings, now (absent)")

    def test_an_env_edit_is_drift_and_is_named_apart_from_the_switches(self):
        """`env` layers exactly as `flags` does, so an edit to it is a different browser."""
        self.launched_with(env={"TZ": "UTC"})

        verdict = drift.of(profile(self.profile_dir, env={"TZ": "America/Denver"}), RUNNING)

        self.assertIsInstance(verdict, drift.Drifted)
        (change,) = verdict.changes
        self.assertEqual(change.subject, "env TZ")
        self.assertEqual((change.launched, change.resolves), ("UTC", "America/Denver"))

    def test_a_variable_cleared_to_empty_is_not_reported_as_one_that_was_removed(self):
        """`""` is a variable set to nothing; `None` is a variable that is not there."""
        self.launched_with(env={"TZ": "UTC"})

        verdict = drift.of(profile(self.profile_dir, env={"TZ": ""}), RUNNING)

        (change,) = verdict.changes
        self.assertEqual(change.resolves, "")
        self.assertEqual(str(change), "env TZ: launched UTC, now ")

    def test_a_repointed_chrome_binary_is_drift_and_is_named(self):
        """`chrome_binary` is layered configuration too, and it is `argv[0]`, not a switch."""
        self.launched_with(binary="/old/Chrome")

        verdict = drift.of(profile(self.profile_dir, binary="/new/Chrome"), RUNNING)

        (change,) = verdict.changes
        self.assertEqual(change.subject, "chrome binary")
        self.assertEqual((change.launched, change.resolves), ("/old/Chrome", "/new/Chrome"))

    def test_reordering_the_flags_drifts_without_leaving_the_sentence_trailing(self):
        """`==` decides, so a reorder is drift — and the naming has to survive naming nothing.

        Every switch is emitted exactly once, so two launches listing the same switches in
        a different order have no entry that differs. The verdict still has to read as a
        sentence rather than as `drifted — `.
        """
        self.launched_with("--no-pings", "--incognito")

        verdict = drift.of(profile(self.profile_dir, "--incognito", "--no-pings"), RUNNING)

        self.assertIsInstance(verdict, drift.Drifted)
        self.assertEqual(verdict.changes, ())
        self.assertEqual(verdict.finding, "drifted — its flags are in a different order")


class PublishedShapeTest(DriftTestCase):
    """`describe` is the `--json` contract, and it is one shape for all four verdicts."""

    def test_every_verdict_publishes_the_same_keys(self):
        """A consumer reads `verdict` and `finding` without first asking which arm it got."""
        self.launched_with("--no-pings")
        stale = profile(self.profile_dir, "--incognito")

        published = [
            drift.describe(drift.of(stale, NOTHING)),
            drift.describe(drift.of(profile(self.profile_dir, "--no-pings"), RUNNING)),
            drift.describe(drift.of(stale, RUNNING)),
        ]

        self.assertEqual([set(entry) for entry in published], [{"verdict", "finding", "changes"}] * 3)
        self.assertEqual([entry["verdict"] for entry in published], ["stopped", "matches", "drifted"])
        self.assertEqual(
            published[2]["changes"],
            [
                {"subject": "--incognito", "launched": None, "resolves": "--incognito"},
                {"subject": "--no-pings", "launched": "--no-pings", "resolves": None},
            ],
        )


if __name__ == "__main__":
    unittest.main()
