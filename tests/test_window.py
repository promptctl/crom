"""What crom does with each answer macOS can give when asked to raise a window.

`osascript` is stubbed throughout, and the answers it is stubbed with are transcripts,
not inventions: every one was measured against System Events on macOS 25.3 before these
tests were written. A live raise prints the window count on stdout and exits 0; a pid with
no window-server process fails with `Invalid index. (-1719)`; a real browser window counts
as 1 through an inlined `whose` specifier and 0 through one bound to a variable.

The stub is the boundary, not the mechanism. These assert what a user is told —
[LAW:behavior-not-structure] — except where the argv crom builds *is* the contract, which
is the one place the exact call matters, because the script's shape is what the
measurements above were taken of.
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

from crom import window
from crom.model import CromError, ProfileRef, Reason, ResolvedProfile, SeedFresh


def temp_profile(test: unittest.TestCase) -> ResolvedProfile:
    root = Path(tempfile.mkdtemp())
    test.addCleanup(shutil.rmtree, root, ignore_errors=True)
    profile_dir = root / "myapp" / "dev"
    return ResolvedProfile(
        ref=ProfileRef("myapp", "dev"),
        port=9300,
        profile_dir=profile_dir,
        chrome_binary=Path("/chrome"),
        argv=("/chrome", f"--user-data-dir={profile_dir}"),
        env={},
        seed=SeedFresh(),
        source=None,
    )


def answered(stdout: str = "1", stderr: str = "", returncode: int = 0):
    """osascript, stubbed to give one answer to every call."""
    return mock.patch(
        "crom.window.subprocess.run",
        return_value=CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr),
    )


class RaiseTest(unittest.TestCase):
    def setUp(self):
        self.profile = temp_profile(self)

    def test_a_raised_window_is_counted(self):
        with answered(stdout="1\n"):
            self.assertEqual(window.raise_profile(self.profile, (4242,)), 1)

    def test_a_browser_with_no_window_raises_successfully_and_counts_none(self):
        """The case `show` exists to be honest about.

        A headless Chrome is a real System Events process and accepts the raise at exit 0
        — measured — it simply has no window to bring forward. Reporting zero rather than
        failing is what lets `show` say "raised, but there is nothing to see" instead of
        either claiming a window or inventing an error for a browser that is working
        exactly as launched. [LAW:no-silent-failure]
        """
        with answered(stdout="0\n"):
            self.assertEqual(window.raise_profile(self.profile, (4242,)), 0)

    def test_every_pid_of_the_profile_is_raised(self):
        """`find_pids` returns a tuple, so two main browsers on one directory is a state
        the type admits and this must not silently raise only the first."""
        with answered(stdout="1\n") as run:
            self.assertEqual(window.raise_profile(self.profile, (11, 22)), 2)
        self.assertEqual([call.args[0][-1] for call in run.call_args_list], ["11", "22"])

    def test_a_pid_that_cannot_be_raised_fails_the_whole_command(self):
        """Fail-fast across several pids, pinned deliberately rather than left to `sum`.

        `show` names the end state "this profile's window is in front". If one of the
        profile's browsers refused to come forward, crom cannot say that happened, so the
        command fails and names the pid that refused. The alternative — aggregating per-pid
        outcomes — invents a partial-success mode for a state that is itself transient, and
        hands every caller a result it has to interpret. [LAW:no-mode-explosion]

        Pinned because the behaviour is currently a property of summing a generator, which
        a later refactor to a list comprehension would silently reverse.
        """
        answers = [
            CompletedProcess(args=[], returncode=0, stdout="1", stderr=""),
            CompletedProcess(args=[], returncode=1, stderr="Invalid index. (-1719)", stdout=""),
        ]
        with mock.patch("crom.window.subprocess.run", side_effect=answers):
            with self.assertRaises(CromError) as caught:
                window.raise_profile(self.profile, (11, 22))

        self.assertIn("22", str(caught.exception))

    def test_a_profile_with_no_running_browser_asks_macos_nothing(self):
        with answered() as run:
            self.assertEqual(window.raise_profile(self.profile, ()), 0)
        run.assert_not_called()

    def test_the_pid_crosses_as_an_argument_rather_than_as_script_text(self):
        """The script crom runs is a fixed string no value can extend.

        Interpolating the pid into the source would make the text crom executes a
        function of data, and AppleScript has no quoting discipline to lean on. Passed as
        an `argv` item it is coerced to an integer by the script itself.
        """
        with answered() as run:
            window.raise_profile(self.profile, (4242,))
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "osascript")
        self.assertEqual(argv[-1], "4242")
        self.assertNotIn("4242", argv[2])

    def test_the_whose_specifier_is_inlined_at_both_uses(self):
        """A guard on the one tidy-up that breaks this script silently.

        Binding the specifier once (`set target to first process whose unix id is pid`)
        and reusing the variable returns a reference that answers 0 to `count of windows`
        for a browser that demonstrably has one — measured, at exit 0, so nothing reports
        a failure and `show` simply tells every user their window is not there. The
        duplication is load-bearing; this fails if someone deduplicates it.
        """
        self.assertEqual(window._RAISE_SCRIPT.count("first process whose unix id is pid"), 2)


class RefusalTest(unittest.TestCase):
    def setUp(self):
        self.profile = temp_profile(self)

    def test_withheld_automation_access_names_the_setting_that_fixes_it(self):
        """The everyday failure, and the one a user can only fix if told.

        macOS grants Automation to the *program that ran crom*, not to crom, so the
        message has to send the reader to System Settings rather than to anything crom
        can do. Without this the command fails with an Apple error number and no next
        step. [LAW:no-silent-failure]
        """
        said = "execution error: Not authorized to send Apple events to System Events. (-1743)"
        with answered(returncode=1, stderr=said):
            with self.assertRaises(CromError) as caught:
                window.raise_profile(self.profile, (4242,))

        message = str(caught.exception)
        self.assertIn("myapp/dev", message)
        self.assertIn("System Settings", message)
        self.assertIn("Automation", message)
        # osascript's own words survive alongside crom's advice: the error number is what
        # a search engine answers, and crom's remedy is a guess about which app to grant.
        self.assertIn("(-1743)", message)
        # The refusal macOS states outright must not answer with a vaguer slug than
        # the timeout arm infers from an unanswered dialog.
        self.assertIs(caught.exception.reason, Reason.AUTOMATION_DENIED)

    def test_a_vanished_process_is_reported_as_one(self):
        said = (
            "execution error: System Events got an error: Can’t get process 1 whose "
            "unix id = 4242. Invalid index. (-1719)"
        )
        with answered(returncode=1, stderr=said):
            with self.assertRaises(CromError) as caught:
                window.raise_profile(self.profile, (4242,))

        message = str(caught.exception)
        self.assertIn("4242", message)
        self.assertIn("exited", message)
        self.assertIs(caught.exception.reason, Reason.WINDOW_RAISE_FAILED)

    def test_an_unrecognised_refusal_still_carries_what_osascript_said(self):
        """crom knows two error numbers; macOS has many. An ending crom has no remedy for
        must still hand over the whole complaint rather than swallowing it."""
        with answered(returncode=1, stderr="execution error: something new. (-42)"):
            with self.assertRaises(CromError) as caught:
                window.raise_profile(self.profile, (4242,))

        self.assertIn("something new. (-42)", str(caught.exception))
        # An ending with no row keeps the general slug rather than borrowing the
        # remedy of whichever row it nearly matched.
        self.assertIs(caught.exception.reason, Reason.WINDOW_RAISE_FAILED)

    def test_an_osascript_killed_before_it_could_complain_still_names_its_status(self):
        """The one ending that could degrade to no information at all.

        A signal ends osascript before it writes anything, so there is no error number for
        a remedy to match and nothing to quote — which used to leave a sentence that named
        a failure and then stopped at the colon. [LAW:no-silent-failure]
        """
        with answered(returncode=-9, stdout="", stderr=""):
            with self.assertRaises(CromError) as caught:
                window.raise_profile(self.profile, (4242,))

        message = str(caught.exception)
        self.assertIn("4242", message)
        self.assertIn("exit -9", message)

    def test_a_machine_without_osascript_is_told_the_command_is_macos_only(self):
        """`crom show` is the one command with no portable spelling. On a machine without
        `osascript` it must say so, not fail with a bare FileNotFoundError traceback that
        reads as a crom bug."""
        with mock.patch("crom.window.subprocess.run", side_effect=FileNotFoundError("osascript")):
            with self.assertRaises(CromError) as caught:
                window.raise_profile(self.profile, (4242,))

        self.assertIn("macOS", str(caught.exception))
        self.assertIs(caught.exception.reason, Reason.PLATFORM_UNSUPPORTED)

    def test_a_prompt_nobody_answers_does_not_hold_the_lock_forever(self):
        """`show` calls this while holding `seed.profile_lock`, so an unbounded wait here
        gates every other command on the profile. The first automation call from a new
        program can sit on macOS's consent dialog; the bound is what keeps a dialog nobody
        is looking at from blocking `crom down`. [LAW:no-ambient-temporal-coupling]"""
        expired = subprocess.TimeoutExpired(cmd="osascript", timeout=window.RAISE_TIMEOUT_SECONDS)
        with mock.patch("crom.window.subprocess.run", side_effect=expired):
            with self.assertRaises(CromError) as caught:
                window.raise_profile(self.profile, (4242,))

        message = str(caught.exception)
        self.assertIn("Automation", message)
        self.assertIn("System Settings", message)
        self.assertIs(caught.exception.reason, Reason.AUTOMATION_DENIED)

    def test_the_call_is_bounded_rather_than_left_to_wait_forever(self):
        with answered() as run:
            window.raise_profile(self.profile, (4242,))
        self.assertEqual(run.call_args.kwargs["timeout"], window.RAISE_TIMEOUT_SECONDS)

    def test_an_osascript_that_cannot_be_executed_is_reported_not_crashed_through(self):
        """`FileNotFoundError` was never the only way running it can fail: a present but
        unexecutable `osascript` raises `PermissionError`, which is no less a reason the
        command cannot work and no more deserving of a traceback."""
        with mock.patch("crom.window.subprocess.run", side_effect=PermissionError("denied")):
            with self.assertRaises(CromError) as caught:
                window.raise_profile(self.profile, (4242,))

        self.assertIn("denied", str(caught.exception))
        # The other half of the same split, and the reason the message assertions
        # above cannot stand alone: both arms print one sentence naming macOS, so a
        # split that sent an unexecutable osascript to PLATFORM_UNSUPPORTED would
        # read exactly as correct here.
        self.assertIs(caught.exception.reason, Reason.WINDOW_RAISE_FAILED)

    def test_an_unreadable_window_count_is_reported_not_crashed_through(self):
        """osascript exited 0, so the number it printed is the only evidence of what
        happened. A build that printed something else would otherwise escape the CLI's
        exit-code contract as a ValueError traceback about arithmetic.
        [LAW:parse-dont-validate]"""
        with answered(stdout="lots\n"):
            with self.assertRaises(CromError) as caught:
                window.raise_profile(self.profile, (4242,))

        self.assertIn("'lots'", str(caught.exception))
        self.assertIs(caught.exception.reason, Reason.WINDOW_RAISE_FAILED)


if __name__ == "__main__":
    unittest.main()
