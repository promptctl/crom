"""Tests for reading the process table — which running Chrome belongs to which profile.

`ps` flattens argv into one string, so recovering the user-data-dir from it is a parsing
problem with real edge cases: directories containing spaces, and profile paths that are
prefixes of one another. Both decide whether `crom up` sees its own browser or launches
a second one on top of it.
"""

import signal
import socket
import unittest
from pathlib import Path
from unittest import mock

from crom import chrome
from crom.model import CromError, ProfileRef, ResolvedProfile, SeedFresh
from crom.resolve import build_argv


def ps_line(pid: int, argv) -> str:
    """Render argv the way `ps -Ao pid=,command=` does: space-joined, boundaries lost."""
    return f"{pid} {' '.join(argv)}"


class GroupByUserDataDirTest(unittest.TestCase):
    def test_a_directory_containing_spaces_survives_the_round_trip(self):
        # A project living at `~/My Projects/app` produces exactly this profile path.
        # Parsing must recover it whole, or crom never recognises its own browser and
        # launches a second one against the same profile.
        profile_dir = Path("/Users/bmf/My Projects/app/.crom/profiles/myapp/dev")
        argv = build_argv(Path("/Applications/Google Chrome"), profile_dir, 9300, ())

        found = chrome._group_by_user_data_dir(ps_line(4242, argv))

        self.assertEqual(found, {str(profile_dir): (4242,)})

    def test_a_profile_path_that_prefixes_another_is_a_separate_profile(self):
        short = Path("/state/profiles/myapp/dev")
        longer = Path("/state/profiles/myapp/dev2")
        output = "\n".join(
            (
                ps_line(1, build_argv(Path("/chrome"), short, 9300, ())),
                ps_line(2, build_argv(Path("/chrome"), longer, 9301, ())),
            )
        )

        found = chrome._group_by_user_data_dir(output)

        self.assertEqual(found, {str(short): (1,), str(longer): (2,)})

    def test_helper_processes_are_not_the_browser(self):
        profile_dir = Path("/state/profiles/myapp/dev")
        argv = build_argv(Path("/chrome"), profile_dir, 9300, ())
        output = "\n".join(
            (
                ps_line(10, argv),
                ps_line(11, (*argv, "--type=renderer")),
                ps_line(12, (*argv, "--type=gpu-process")),
            )
        )

        found = chrome._group_by_user_data_dir(output)

        self.assertEqual(found, {str(profile_dir): (10,)})

    def test_several_windows_on_one_profile_report_every_pid(self):
        profile_dir = Path("/state/profiles/myapp/dev")
        argv = build_argv(Path("/chrome"), profile_dir, 9300, ())
        output = "\n".join((ps_line(7, argv), ps_line(9, argv)))

        self.assertEqual(chrome._group_by_user_data_dir(output), {str(profile_dir): (7, 9)})

    def test_a_browser_crom_did_not_launch_is_not_a_crom_profile(self):
        # The user's own Chrome carries a --user-data-dir but no CDP port; it is not a
        # profile crom manages and must not be reported as one.
        output = ps_line(99, ("/chrome", "--user-data-dir=/Users/bmf/Library/Application Support/Google/Chrome"))

        self.assertEqual(chrome._group_by_user_data_dir(output), {})

    def test_ps_header_and_blank_lines_are_not_processes(self):
        self.assertEqual(chrome._group_by_user_data_dir("  PID COMMAND\n\n   \n"), {})

    def test_a_configured_flag_cannot_spoof_the_user_data_dir(self):
        """`parse_flags` only inspects the switch name before `=`, so a flag whose
        *value* contains the literal text is accepted — and it lands before crom's own
        switches in argv. Matching the first occurrence captured the decoy."""
        profile_dir = Path("/state/profiles/myapp/dev")
        spoof = "--fake=--user-data-dir=/evil --remote-debugging-port=1"
        argv = build_argv(Path("/chrome"), profile_dir, 9300, (spoof,))

        found = chrome._group_by_user_data_dir(ps_line(4242, argv))

        self.assertEqual(found, {str(profile_dir): (4242,)})

    def test_a_profile_path_containing_the_terminator_text_is_not_truncated(self):
        """`state_dir` is an unrestricted string, so a profile path can contain the very
        text used as the terminator. The non-greedy capture stopped at the embedded copy
        until the pattern was anchored to a real port at the end of the line."""
        profile_dir = Path("/state/x --remote-debugging-port=oops/myapp/dev")
        argv = build_argv(Path("/chrome"), profile_dir, 9300, ())

        found = chrome._group_by_user_data_dir(ps_line(4242, argv))

        self.assertEqual(found, {str(profile_dir): (4242,)})


class RequirePortAvailableTest(unittest.TestCase):
    """The check that turns "Chrome timed out after 30s" into the actual diagnosis."""

    @staticmethod
    def _profile(port: int) -> ResolvedProfile:
        return ResolvedProfile(
            ref=ProfileRef("myapp", "dev"),
            port=port,
            profile_dir=Path("/state/profiles/myapp/dev"),
            chrome_binary=Path("/chrome"),
            argv=(),
            env={},
            seed=SeedFresh(),
            source=None,
        )

    def test_a_free_port_passes_without_complaint(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]
        # The socket is closed again, so the port is genuinely free by the time we ask.
        chrome._require_port_available(self._profile(free_port))

    def test_a_held_port_is_named_along_with_the_profile_that_wanted_it(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
            holder.bind(("127.0.0.1", 0))
            holder.listen(1)
            taken = holder.getsockname()[1]

            with self.assertRaises(CromError) as caught:
                chrome._require_port_available(self._profile(taken))

        message = str(caught.exception)
        self.assertIn(str(taken), message)
        self.assertIn("myapp/dev", message)
        self.assertIn("lsof", message)  # the message tells the user how to find it


class KillTest(unittest.TestCase):
    """SIGTERM first, SIGKILL only for what is still there after the grace period.

    Driven against a stubbed process table rather than a real Chrome: the logic under
    test is the escalation, and getting it wrong in either direction — never force-killing
    a hung browser, or SIGKILLing one that was shutting down cleanly — is the risk.
    """

    def setUp(self):
        self.profile = RequirePortAvailableTest._profile(9300)
        self.signals: list[tuple[int, int]] = []
        patcher = mock.patch.object(chrome, "_signal", lambda pid, sig: self.signals.append((pid, sig)))
        patcher.start()
        self.addCleanup(patcher.stop)
        # A real 5s wait would make this test the slowest in the suite for no coverage.
        timeout = mock.patch.object(chrome, "SHUTDOWN_TIMEOUT_SECONDS", 0.2)
        timeout.start()
        self.addCleanup(timeout.stop)

    def test_a_process_that_exits_during_the_grace_period_is_never_force_killed(self):
        scans = [(11, 22), ()]  # alive when we look, gone on the next scan
        with mock.patch.object(chrome, "find_pids", side_effect=lambda p: scans.pop(0) if scans else ()):
            killed = chrome.kill(self.profile)

        self.assertEqual(killed, (11, 22))
        self.assertEqual(self.signals, [(11, signal.SIGTERM), (22, signal.SIGTERM)])

    def test_a_straggler_still_running_after_the_timeout_is_force_killed(self):
        with mock.patch.object(chrome, "find_pids", return_value=(11,)):
            killed = chrome.kill(self.profile)

        self.assertEqual(killed, (11,))
        self.assertEqual(self.signals[0], (11, signal.SIGTERM))
        self.assertIn((11, signal.SIGKILL), self.signals)

    def test_stopping_a_profile_that_is_not_running_signals_nothing(self):
        with mock.patch.object(chrome, "find_pids", return_value=()):
            self.assertEqual(chrome.kill(self.profile), ())
        self.assertEqual(self.signals, [])


if __name__ == "__main__":
    unittest.main()
