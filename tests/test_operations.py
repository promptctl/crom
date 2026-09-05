"""Driving crom's mutating half the way a caller that is not the CLI drives it.

Every operation in `crom.operations` was reachable only through `CliRunner` until this
file, which meant the suite could not tell an orchestration bug from a rendering bug:
a `crom up` that printed "Relaunched" told you those two facts jointly and neither one
alone. Here the operations are called directly, and what is asserted is the value they
hand back — the `Outcome` member, the `Declaration`, the PIDs, the refusal's `reason`
and `fields`. The sentences a user reads stay `test_cli`'s subject, because they are
the CLI's and not these functions'.

The stubs are the OS boundary and stop there. `chrome.find_pids`, `chrome.launch` and
`chrome.kill` are the process table; nothing above them is replaced, so `drift.of`,
`seed.materialize_under_lock`, `configwrite` and the real locks all run. A stub placed
above the mechanism under test voids the test silently, which is how `CliTest.setUp`
stubbing `chrome.scan` hid the argv-rewrite gap in crom-converge-4je.

Which stubs each class starts is therefore a statement about its subject, and one
absence is load-bearing: `InitTest` does not stub `config.find_chrome`, because `crom
init` is the command `Session`'s laziness exists to keep working on a machine that has
no Chrome yet. Started in a shared base, that property would hold by accident here and
nothing would notice it breaking.
"""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crom import cli, config, drift, launched, operations
from crom.model import CromError, ProfileSpec, Reason, SeedFresh
from crom.session import Session

# The pid the fake process table hands out. It and the binary below are planted here and
# computed by no part of crom, so a claim measured against either is measured against
# something the code under test did not produce. [LAW:one-source-of-truth] read the way a
# test has to read it: sharing a source with the subject is the defect, not the goal.
PID = 4242

# Planted into a launch record to make a profile look launched by something else. The
# binary is not the stubbed `find_chrome` answer, so `drift.of` finds a real difference
# and the text is greppable in the record afterwards.
FOREIGN = launched.Launch(("/other/chrome", "--window-size=800,600"), {})


class FakeChrome:
    """The process table and the launcher, as one thing that agrees with itself.

    Three `mock.patch`es returning fixed values could describe a machine where
    `find_pids` reports a browser that `launch` never started and `kill` never stops —
    a world crom cannot be in, and one in which `up`'s five arms mean nothing. Modelled
    instead: launching makes the browser findable and writes the record `drift.of`
    reads, killing makes it unfindable and hands back what it stopped.
    [LAW:types-are-the-program] the illegal world state is unrepresentable, so a test
    cannot assert an ending against a machine that could not have produced it.

    Writing the launch record here rather than stubbing `launched` is what makes
    `MATCHED` honest: the record under comparison is the one this launch wrote, exactly
    as `chrome.launch` writes it, so the verdict is `drift.of`'s own work and not a
    fixture's.
    """

    def __init__(self):
        self.running: tuple[int, ...] = ()

    def find_pids(self, profile) -> tuple[int, ...]:
        return self.running

    def launch(self, profile) -> tuple[int, ...]:
        # The real `chrome.launch` cannot succeed here: Chrome binds the CDP port well
        # before it answers on it, so a second launch against a live profile fails
        # `_require_port_available`. Modelled, because a fake that quietly allowed it
        # would let `up` launch unconditionally — dropping the `running or` that makes
        # this idempotent — and still report `MATCHED` with the right pids.
        assert not self.running, "launched a profile that already had a browser on its port"
        launched.record(profile.profile_dir, launched.Launch.of(profile))
        self.running = (PID,)
        return self.running

    def kill(self, profile) -> tuple[int, ...]:
        stopped, self.running = self.running, ()
        return stopped


class OperationTest(unittest.TestCase):
    """A machine with no crom on it, and a directory to stand in.

    Both halves are required and they are different facts. `HOME` and the XDG variables
    decide where the user config and the state directory go, so without them these tests
    write into the developer's own crom. The working directory is a separate input no
    argument carries — `config.discover` walks *up* from the cwd, and this repository
    carries its own tracked `.crom.toml`, so a test that does not `chdir` resolves
    against namespace `crom` rather than `user`.
    [LAW:no-ambient-temporal-coupling] the ambient state is set, not inherited.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.env = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.root / "home"),
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_STATE_HOME": str(self.root / "state"),
            },
        )
        self.env.start()
        self.previous = Path.cwd()
        # A directory of its own rather than `self.root`, so a test that writes a project
        # config writes it somewhere the temp root's other contents cannot be mistaken for.
        self.work = self.root / "work"
        self.work.mkdir()
        os.chdir(self.work)

    def tearDown(self):
        # Before the temporary directory goes: a cwd pointing at a removed directory
        # breaks every later test in the run.
        os.chdir(self.previous)
        self.env.stop()
        self.tmp.cleanup()

    def stub(self, target: str, *args, **kwargs):
        """Replace one thing for the length of one test, unwound however the test ends."""
        patch = mock.patch(target, *args, **kwargs)
        patch.start()
        self.addCleanup(patch.stop)


class ProfileTest(OperationTest):
    """A readied machine declaring one profile, `alpha`, and a fake process table.

    The premise `up`, `down` and `rm` share: all three take a `ResolvedProfile` that
    something else declared, and all three touch the browser. `add` and `init` do
    neither, which is why they build on `OperationTest` directly — `init` needs no Chrome
    at all, and starting these stubs for it would hide the day it began to.

    `seed=SeedFresh()` rather than crom's default is what lets the real
    `seed.materialize_under_lock` run instead of a stub: `seed._plan` reads `fresh` as
    the empty copy plan, so seeding creates an empty directory rather than duplicating
    this machine's actual Chrome profile.
    """

    def setUp(self):
        super().setUp()
        # Not about the operations: resolving any profile reads `chrome_binary`, which
        # would otherwise need a real Chrome on whatever runs this.
        self.stub("crom.config.find_chrome", return_value=Path("/stub/chrome"))
        fake = FakeChrome()
        for name in ("find_pids", "launch", "kill"):
            self.stub(f"crom.chrome.{name}", getattr(fake, name))
        Session.begin(log=lambda _: None)
        operations.add(
            Session().scope, ProfileSpec(name="alpha", seed=SeedFresh()), log=lambda _: None
        )

    def profile(self, name: str):
        """Resolve through a fresh session, because `add` rewrote the file a scope is
        read from and a stale scope is the race `add` re-reads to survive."""
        return Session().profile(name)


class WithoutClickTest(unittest.TestCase):
    """The epic's claim — "callable without click" — which nothing else in the suite tests.

    No temporary machine, because neither test runs an operation: both ask what importing
    the module drags in behind it.
    """

    def test_importing_the_operations_reaches_neither_click_nor_the_command_layer(self):
        """In a fresh interpreter, because this one cannot answer.

        `tests/test_cli` imports click at module scope, so by the time any test runs
        click is in `sys.modules` and an in-process check would either fail on a full
        run or pass vacuously on a single-file one — an assertion whose answer depends on
        which other tests ran is not an assertion. A subprocess has imported exactly what
        this line names.

        The exit code is checked and not merely the output: a subprocess that died on the
        import prints nothing, and "nothing" is this test's success value. Unchecked, the
        strongest possible failure — the module not importing at all — would read as a
        pass. [LAW:parse-dont-validate] an answer-shaped void, caught at the one place it
        can be.
        """
        probe = (
            "import sys, crom.operations\n"
            "print(' '.join(m for m in ('click', 'crom.cli') if m in sys.modules))\n"
        )

        result = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, check=False
        )

        self.assertEqual(result.returncode, 0, f"the probe did not run: {result.stderr}")
        self.assertEqual(result.stdout.strip(), "")

    def test_the_command_layer_holds_no_way_to_author_a_refusal(self):
        """`cli` answers for refusals and never authors them, which is this epic's shape.

        Every refusal crom makes is now raised by the module that found the fault, and
        `cli` maps the `Reason` it catches onto an exit code. A refusal authored back in
        a command body would be indistinguishable from outside — same message, same exit
        code — so no test of crom's behavior can reach this property, and it is currently
        held by nothing but habit. What it costs to break is the thing ae2 was for: a
        rule that only fires for CLI users, invisible to every other caller of the
        operation it belongs to. [LAW:single-enforcer]
        """
        self.assertFalse(
            hasattr(cli, "Reason"),
            "cli imported Reason again — a refusal authored at the boundary is one the "
            "operations module cannot make, and no non-CLI caller would ever meet",
        )


class UpTest(ProfileTest):
    """`up`'s five endings, driven through the real `drift.of`.

    The verdict is most of what `up` decides, so a stubbed `drift.of` would leave four
    of these arms asserting that a fixture reaches the match statement it was written
    for. Each ending below is arrived at by putting the machine into the state that
    produces it and letting crom read it.
    """

    def drift_the_record(self, profile) -> None:
        """Make the running browser look launched by something else."""
        launched.record(profile.profile_dir, FOREIGN)

    def test_a_profile_with_nothing_running_is_started(self):
        result = operations.up(self.profile("alpha"), operations.OnDrift.REPLACE)

        self.assertIs(result.outcome, operations.Outcome.STARTED)
        self.assertIsInstance(result.found, drift.Stopped)
        self.assertEqual(result.pids, (PID,))
        self.assertEqual(result.stopped, ())

    def test_a_browser_already_running_this_config_is_reported_not_restarted(self):
        """Idempotence, which is `up`'s whole advertised contract.

        The first call is what makes the second one's state real: the record under
        comparison is the one that launch wrote, so `MATCHED` is `drift.of` agreeing with
        crom's own launch rather than a fixture agreeing with itself.

        `stopped` empty is the assertion that carries the contract. The outcome names the
        arm, but a `RELAUNCHED` bug that reported itself as `MATCHED` would still have
        killed the browser, and this is the evidence of the killing.
        """
        profile = self.profile("alpha")
        operations.up(profile, operations.OnDrift.REPLACE)

        result = operations.up(profile, operations.OnDrift.REPLACE)

        self.assertIs(result.outcome, operations.Outcome.MATCHED)
        self.assertIsInstance(result.found, drift.Matches)
        self.assertEqual(result.stopped, ())
        self.assertEqual(result.pids, (PID,))

    def test_a_browser_crom_has_no_record_for_is_left_running(self):
        """Unmeasured is not a quiet `Drifted`, and the difference is someone's tabs.

        Relaunching here would kill a browser that may already match, on the evidence of
        a missing file. `stopped` empty is the whole claim; the outcome only names it.
        """
        profile = self.profile("alpha")
        operations.up(profile, operations.OnDrift.REPLACE)
        launched.path(profile.profile_dir).unlink()

        result = operations.up(profile, operations.OnDrift.REPLACE)

        self.assertIs(result.outcome, operations.Outcome.UNMEASURED)
        self.assertIsInstance(result.found, drift.Unmeasured)
        self.assertEqual(result.stopped, ())

    def test_a_drifted_browser_is_left_alone_under_the_reporting_policy(self):
        """The policy withholds the stop, and this is the half that proves it withholds it."""
        profile = self.profile("alpha")
        operations.up(profile, operations.OnDrift.REPLACE)
        self.drift_the_record(profile)

        result = operations.up(profile, operations.OnDrift.REPORT)

        self.assertIs(result.outcome, operations.Outcome.REPORTED)
        self.assertIsInstance(result.found, drift.Drifted)
        self.assertEqual(result.stopped, ())
        self.assertEqual(result.pids, (PID,))

    def test_a_drifted_browser_is_replaced_and_the_record_is_rewritten(self):
        """The one arm that stops something, asserted on what it stopped.

        `found` is how the profile stood when `up` reached it and not how it stands now,
        so it stays `Drifted` after a relaunch that has left the browser matching — the
        distinction `crom up --json` publishes under `found`.

        The record's text is the claim measured against something `up` did not compute:
        the foreign binary was planted by this test, and a relaunch that stopped the
        browser without recording the new launch would leave it there, reporting
        `RELAUNCHED` while the next `crom up` still found drift. [LAW:verifiable-goals]
        """
        profile = self.profile("alpha")
        operations.up(profile, operations.OnDrift.REPLACE)
        self.drift_the_record(profile)

        result = operations.up(profile, operations.OnDrift.REPLACE)

        self.assertIs(result.outcome, operations.Outcome.RELAUNCHED)
        self.assertIsInstance(result.found, drift.Drifted)
        self.assertEqual(result.stopped, (PID,))
        self.assertEqual(result.pids, (PID,))
        self.assertNotIn("/other/chrome", launched.path(profile.profile_dir).read_text())

    def test_what_it_says_on_the_way_goes_to_the_given_log_and_not_to_stderr(self):
        """The seam a non-CLI caller needs, which is why every operation takes a `log=`.

        Seeding and the replacement both narrate, and a caller embedding crom needs them
        somewhere other than this process's stderr. Both halves are asserted: the
        sentences reach the log, and stderr stays empty — a `log` that were merely
        additional would leave the second half failing. [LAW:effects-at-boundaries]
        """
        profile = self.profile("alpha")
        said: list[str] = []

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            operations.up(profile, operations.OnDrift.REPLACE, log=said.append)

        self.assertTrue(
            any("from seed 'fresh'" in message for message in said),
            f"the seeding was not reported through the given log: {said}",
        )
        self.assertEqual(stderr.getvalue(), "")


class DownTest(ProfileTest):
    """`down` hands back what it stopped, which is the only thing separating its endings."""

    def test_a_running_profile_reports_the_pids_it_stopped(self):
        profile = self.profile("alpha")
        operations.up(profile, operations.OnDrift.REPLACE)

        self.assertEqual(operations.down(profile), (PID,))

    def test_a_profile_that_was_not_up_is_not_an_error(self):
        """Empty rather than a refusal: `crom down` on a stopped profile has got what it
        asked for, and the caller is the one with somewhere to say nothing happened."""
        self.assertEqual(operations.down(self.profile("alpha")), ())


class AddTest(OperationTest):
    """`add` takes a `Scope` and a `ProfileSpec`, because it is what creates the
    declaration the other operations resolve from.

    One stub, and it is not about `add`: resolving the profile it just declared reads
    `chrome_binary`, which would otherwise need a real Chrome on the runner.
    """

    def setUp(self):
        super().setUp()
        self.stub("crom.config.find_chrome", return_value=Path("/stub/chrome"))
        Session.begin(log=lambda _: None)

    def add(self, **spec) -> operations.Add:
        return operations.add(Session().scope, ProfileSpec(**spec), log=lambda _: None)

    def test_declaring_a_new_profile_writes_it_and_resolves_it(self):
        result = self.add(name="ci", seed=SeedFresh())

        self.assertIs(result.outcome, operations.Declaration.CREATED)
        self.assertEqual(result.profile.ref.name, "ci")
        self.assertEqual(result.target.read_text().count("[profiles.ci]"), 1)

    def test_declaring_a_profile_that_is_already_declared_converges(self):
        """Idempotent, and the ending is carried rather than derived: both paths leave the
        file declaring the same profile, so the write that did or did not happen is the
        only witness to which one this was."""
        self.add(name="ci", seed=SeedFresh())

        result = self.add(name="ci", seed=SeedFresh())

        self.assertIs(result.outcome, operations.Declaration.ALREADY_PRESENT)
        self.assertEqual(result.target.read_text().count("[profiles.ci]"), 1)

    def test_asking_the_file_for_something_it_does_not_say_is_refused_as_data(self):
        """Convergence stops where it would become a lie, and the refusal is readable
        without reading English.

        `fields` is the assertion worth having: a caller that is not a terminal needs to
        know *which* setting collided, and a refusal carrying only a sentence gives it
        nothing to act on. The file's own text is the second claim, measured against
        something `add` did not compute — a refusal that had already written would leave
        two `[profiles.ci]` stanzas or one holding the wrong port, and the reason alone
        would not notice. [LAW:no-silent-failure]
        """
        first = self.add(name="ci", seed=SeedFresh(), port=9500)

        with self.assertRaises(CromError) as caught:
            self.add(name="ci", seed=SeedFresh(), port=9600)

        self.assertIs(caught.exception.reason, Reason.DECLARATION_DIFFERS)
        self.assertEqual(caught.exception.fields, {"settings": ("port",)})
        self.assertEqual(first.target.read_text().count("[profiles.ci]"), 1)
        self.assertIn("9500", first.target.read_text())
        self.assertNotIn("9600", first.target.read_text())


class InitTest(OperationTest):
    """`init` needs no session, no scope and no Chrome — it is what creates the config a
    scope is read from.

    Deliberately no `find_chrome` stub. `crom init` has to work on a machine that has no
    Chrome installed yet, which is the case `Session`'s lazy scope exists for, and a stub
    started here would hide the day `init` grew a dependency on resolving a profile.

    Each ending gets a fresh directory: `init` is idempotent per directory, so a reused
    one silently tests `ALREADY_PRESENT` instead of whatever was meant.
    """

    def project(self, name: str) -> Path:
        directory = self.work / name
        directory.mkdir()
        return directory

    def test_a_bare_init_names_the_project_after_its_directory(self):
        """The artifact carries the claim, not the record.

        `Init.namespace` is read back out of the file, and the obvious way to spell the
        expectation is the same `slug_for(here.name)` that `init` used — an expectation
        and an actual computed by one function, which agree even when that function is
        wrong. The file's text is the independent witness, and `count` rather than `in`
        because a second write would append a second `namespace` line that `in` would
        happily accept.
        """
        directory = self.project("proj")

        result = operations.init(directory, None, None)

        self.assertIs(result.outcome, operations.Declaration.CREATED)
        self.assertEqual(result.target, directory / ".crom.toml")
        self.assertEqual(result.target.read_text().count('namespace = "proj"'), 1)

    def test_an_initialised_project_is_reported_rather_than_rewritten(self):
        """And reported as what the file says, not as what this call would have written —
        which is the same thing here only because the first call chose the same values."""
        directory = self.project("proj")
        operations.init(directory, "chosen", "fresh")

        result = operations.init(directory, None, None)

        self.assertIs(result.outcome, operations.Declaration.ALREADY_PRESENT)
        self.assertEqual(result.namespace, "chosen")
        self.assertEqual(result.seed, "fresh")

    def test_a_request_that_agrees_with_the_file_converges(self):
        """Restating what the project already declares asks for nothing, so it is not a
        contradiction — the boundary between converging and refusing runs between this
        test and the two below it."""
        directory = self.project("proj")
        operations.init(directory, "chosen", "fresh")

        result = operations.init(directory, "chosen", "fresh")

        self.assertIs(result.outcome, operations.Declaration.ALREADY_PRESENT)
        self.assertEqual(result.seed, "fresh")

    def test_asking_an_initialised_project_for_a_different_namespace_is_refused(self):
        directory = self.project("proj")
        operations.init(directory, "chosen", None)

        with self.assertRaises(CromError) as caught:
            operations.init(directory, "other", None)

        self.assertIs(caught.exception.reason, Reason.DECLARATION_DIFFERS)
        self.assertEqual(caught.exception.fields, {"settings": ("namespace",)})
        self.assertEqual((directory / ".crom.toml").read_text().count('namespace = "chosen"'), 1)

    def test_asking_an_initialised_project_for_a_different_seed_is_refused(self):
        """A separate test from the namespace refusal and not a subtest of it: the two
        read different keys out of the file by different routes — `value_at(target,
        "namespace")` against `value_at(target, "defaults", "seed")` — and a single test
        covering both would pass on a `fields` that named the wrong one."""
        directory = self.project("proj")
        operations.init(directory, None, "fresh")

        with self.assertRaises(CromError) as caught:
            operations.init(directory, None, "default")

        self.assertIs(caught.exception.reason, Reason.DECLARATION_DIFFERS)
        self.assertEqual(caught.exception.fields, {"settings": ("seed",)})

    def test_the_user_namespace_is_reserved(self):
        with self.assertRaises(CromError) as caught:
            operations.init(self.project("proj"), "user", None)

        self.assertIs(caught.exception.reason, Reason.NAMESPACE_RESERVED)

    def test_a_reserved_namespace_is_named_before_an_unrecognised_seed(self):
        """Which refusal fires when both arguments are bad, and it is an ordering claim.

        The two answer on different exit codes, so a script branching on them sees the
        order directly: `crom init user --seed chorme` names the reservation, a conflict,
        rather than the seed, a failure. Moving the seed parse above the reserved-namespace
        check regresses this and nothing about either refusal alone would notice — which is
        exactly what PR #52 did before review caught it. [LAW:verifiable-goals]
        """
        with self.assertRaises(CromError) as caught:
            operations.init(self.project("proj"), "user", "chorme")

        self.assertIs(caught.exception.reason, Reason.NAMESPACE_RESERVED)

    def test_an_unrecognised_seed_is_refused_before_anything_is_written(self):
        """The parse is a border and it runs ahead of the write, so a misspelt seed cannot
        reach a file the user is about to commit. The absent file is the assertion; the
        reason alone would hold on a version that wrote first and refused after."""
        directory = self.project("proj")

        with self.assertRaises(CromError) as caught:
            operations.init(directory, None, "chorme")

        self.assertIs(caught.exception.reason, Reason.CONFIG_INVALID)
        self.assertIn("chorme", str(caught.exception))
        self.assertFalse((directory / ".crom.toml").exists())

    def test_a_file_that_declares_no_usable_namespace_is_refused_not_reported(self):
        """A hand-written config holding a number configures nothing, and converging on it
        would report a namespace of `None` and send the user to a `crom up` that
        `config.parse` is about to refuse anyway. Said here instead, where the fix is
        still cheap."""
        directory = self.project("proj")
        (directory / ".crom.toml").write_text("namespace = 7\n")

        with self.assertRaises(CromError) as caught:
            operations.init(directory, None, None)

        self.assertIs(caught.exception.reason, Reason.CONFIG_INVALID)

    def test_an_existing_dot_crom_directory_is_adopted_rather_than_shadowed(self):
        """`config.init_target`'s existing-first rule, which nothing else in the suite
        reaches.

        Both halves matter and neither implies the other: `init` could report the right
        target while still creating the `.crom.toml` beside it, and a project would then
        hold two configs whose disagreement no command could resolve.
        [LAW:one-source-of-truth]
        """
        directory = self.project("proj")
        (directory / ".crom").mkdir()
        (directory / ".crom" / "config.toml").write_text('namespace = "adopted"\n')

        result = operations.init(directory, None, None)

        self.assertIs(result.outcome, operations.Declaration.ALREADY_PRESENT)
        self.assertEqual(result.target, directory / ".crom" / "config.toml")
        self.assertEqual(result.namespace, "adopted")
        self.assertFalse((directory / ".crom.toml").exists())


class RmTest(ProfileTest):
    """`rm` converges rather than refusing: it stops the browser itself, under the lock it
    already holds, instead of telling the user to run `crom down` first.

    The real `seed.profile_lock` and the real `shutil.rmtree` run — driving them is most
    of the value, since the ordering they sit in is what `rm` exists to get right.
    """

    def setUp(self):
        super().setUp()
        self.target = config.write_target(Session().scope)

    def test_removing_a_running_profile_stops_it_and_deletes_its_data(self):
        """The PIDs are the return value because they are the most surprising thing `rm`
        does, and the caller is the one with somewhere to say it."""
        profile = self.profile("alpha")
        operations.up(profile, operations.OnDrift.REPLACE)

        stopped = operations.rm(profile, config.load_ambient(), keep_data=False, log=lambda _: None)

        self.assertEqual(stopped, (PID,))
        self.assertFalse(profile.profile_dir.exists())
        self.assertNotIn("[profiles.alpha]", self.target.read_text())

    def test_keeping_the_data_leaves_the_directory_and_still_undeclares(self):
        """The two halves are independent, and this is the path where they disagree: the
        directory survives, the declaration does not."""
        profile = self.profile("alpha")
        operations.up(profile, operations.OnDrift.REPLACE)

        stopped = operations.rm(profile, config.load_ambient(), keep_data=True, log=lambda _: None)

        self.assertEqual(stopped, (PID,))
        self.assertTrue(profile.profile_dir.exists())
        self.assertNotIn("[profiles.alpha]", self.target.read_text())

    def test_a_profile_whose_directory_was_never_built_is_removed_without_complaint(self):
        """Declared and never brought up is a state crom reaches on its own — `crom add`
        writes the stanza and nothing creates the directory until the first `crom up` — so
        `rm` has to survive it rather than raise on a missing path."""
        profile = self.profile("alpha")
        self.assertFalse(profile.profile_dir.exists())

        stopped = operations.rm(profile, config.load_ambient(), keep_data=False, log=lambda _: None)

        self.assertEqual(stopped, ())
        self.assertNotIn("[profiles.alpha]", self.target.read_text())
