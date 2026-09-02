"""Tests for reading the process table — which running Chrome belongs to which profile.

`ps` flattens argv into one string, so recovering the user-data-dir from it is a parsing
problem with real edge cases: directories containing spaces, and profile paths that are
prefixes of one another. Both decide whether `crom up` sees its own browser or launches
a second one on top of it.
"""

import dataclasses
import os
import shutil
import signal
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from crom import chrome
from crom.model import CromError, ProfileRef, ResolvedProfile, SeedFresh
from crom.resolve import build_argv


def ps_line(pid: int, argv) -> str:
    """Render argv the way `ps -Ao pid=,command=` does: space-joined, boundaries lost."""
    return f"{pid} {' '.join(argv)}"


def temp_profile(test: unittest.TestCase, port: int = 9300) -> ResolvedProfile:
    """A profile whose directory really exists, removed when `test` finishes.

    Real rather than notional because `launch` creates the profile dir before it spawns
    anything, so a read-only path fails these tests before they reach what they cover.
    The cleanup is registered against the `mkdtemp` root, not the nested profile dir, so
    the whole tree goes — and `mkdtemp`, unlike `TemporaryDirectory`, has no owner whose
    lifetime could end first.
    """
    import tempfile

    root = Path(tempfile.mkdtemp())
    test.addCleanup(shutil.rmtree, root, ignore_errors=True)
    profile_dir = root / "myapp" / "dev"
    return ResolvedProfile(
        ref=ProfileRef("myapp", "dev"),
        port=port,
        profile_dir=profile_dir,
        chrome_binary=Path("/chrome"),
        argv=("/chrome", f"--user-data-dir={profile_dir}"),
        env={},
        seed=SeedFresh(),
        source=None,
    )


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

    def test_a_browser_crom_did_not_launch_is_not_mistaken_for_a_profile(self):
        """The claim that matters is that a foreign Chrome is never reported *as a crom
        profile* — which is a fact about the lookup, not about the parse.

        This used to assert that the user's own Chrome produced no entry at all, but that
        was an artifact of a pattern that only matched crom's own argv ordering, and
        keeping it would mean keeping the bug where a browser that restarted itself
        vanished from the scan. `scan()` says it reports every running main Chrome, so
        listing one is correct; what must never happen is a crom profile resolving to a
        PID that is not its browser. The directory is the discriminator, and no crom
        profile lives under the real Chrome's user-data-dir.
        """
        foreign = "/Users/bmf/Library/Application Support/Google/Chrome"
        output = ps_line(99, ("/chrome", f"--user-data-dir={foreign}"))

        found = chrome._group_by_user_data_dir(output)

        self.assertEqual(found, {foreign: (99,)})
        # The part crom acts on: this profile is not running, foreign browser or not.
        self.assertEqual(found.get("/state/profiles/myapp/dev", ()), ())

    def test_ps_header_and_blank_lines_are_not_processes(self):
        self.assertEqual(chrome._group_by_user_data_dir("  PID COMMAND\n\n   \n"), {})

    def test_a_configured_flag_cannot_spoof_the_user_data_dir(self):
        """`parse_layer` only inspects the switch name before `=`, so a flag whose
        *value* contains the literal text is accepted — and it lands before crom's own
        switches in argv. Matching the first occurrence captured the decoy."""
        profile_dir = Path("/state/profiles/myapp/dev")
        spoof = "--fake=--user-data-dir=/evil --remote-debugging-port=1"
        argv = build_argv(Path("/chrome"), profile_dir, 9300, (spoof,))

        found = chrome._group_by_user_data_dir(ps_line(4242, argv))

        self.assertEqual(found, {str(profile_dir): (4242,)})

    def test_a_browser_that_restarted_itself_is_still_found(self):
        """Chrome re-execs itself and rewrites its argv when it does.

        This is the real `ps` line from a browser that displayed "relaunch the browser to
        load your profile data" and restarted — note `--user-data-dir` is no longer last,
        and no longer adjacent to `--remote-debugging-port`. A pattern anchored to the
        order `build_argv` emits stops matching here, and then every command that asks
        "is this running" is told no about a browser that is: `up` starts a second Chrome
        on the same directory, `down` cannot find it, `list` says stopped, and `rm`
        deletes a live browser's profile. Observed, not hypothesised.
        """
        directory = "/state/profiles/smoketest/dev"
        restarted = (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
            "--disable-background-networking --disable-sync --no-first-run "
            f"--remote-debugging-port=9223 --restart --user-data-dir={directory} --restart"
        )

        found = chrome._group_by_user_data_dir(f"74546 {restarted}")

        self.assertEqual(found, {directory: (74546,)})

    def test_a_restarted_browser_under_a_path_with_spaces_is_still_found(self):
        """The two hard cases together: argv reordered *and* a directory with spaces."""
        directory = "/Users/you/My Projects/app/.crom/profiles/myapp/dev"
        restarted = (
            f"/chrome --remote-debugging-port=9300 --restart --user-data-dir={directory} --restart"
        )

        found = chrome._group_by_user_data_dir(f"4242 {restarted}")

        self.assertEqual(found, {directory: (4242,)})

    def test_a_directory_containing_switch_like_text_is_not_recognised(self):
        """The documented limit of parsing flattened `ps` output, pinned deliberately.

        `state_dir` is an unrestricted string, so a profile path *can* contain something
        shaped like a switch — and there is no way to tell it from a real one once argv
        has been joined with spaces. The previous pattern supported this by requiring
        `--user-data-dir` to sit last and adjacent to `--remote-debugging-port`, which
        cost it every browser that restarted itself. That trade was backwards: it
        defended a directory nobody has against a restart everybody gets.

        Names cannot introduce this — `validate_name` allows only `[a-z0-9._-]` — so it
        takes a deliberately hostile `state_dir` to reach. This test exists so the
        narrowing is a recorded decision rather than an undiscovered regression.
        """
        profile_dir = Path("/state/x --remote-debugging-port=oops/myapp/dev")
        argv = build_argv(Path("/chrome"), profile_dir, 9300, ())

        found = chrome._group_by_user_data_dir(ps_line(4242, argv))

        self.assertEqual(found, {"/state/x": (4242,)})  # truncated, and knowably so


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
        """Escalates to SIGKILL, and returns once the process is actually gone.

        The scans model a browser that ignores SIGTERM through the grace period and dies
        on SIGKILL. Returning is gated on it having *gone*, not on the signal having been
        sent: `rm` deletes the user-data-dir the moment this returns.
        """
        alive = [True]

        def signal_(pid, sig):
            self.signals.append((pid, sig))
            if sig == signal.SIGKILL:
                alive[0] = False

        with (
            mock.patch.object(chrome, "_signal", signal_),
            mock.patch.object(chrome, "find_pids", side_effect=lambda p: (11,) if alive[0] else ()),
        ):
            killed = chrome.kill(self.profile)

        self.assertEqual(killed, (11,))
        self.assertEqual(self.signals, [(11, signal.SIGTERM), (11, signal.SIGKILL)])

    def test_a_browser_that_survives_sigkill_is_never_reported_as_stopped(self):
        """[LAW:no-silent-failure] returning would tell `rm` it is safe to delete.

        Nothing crom can do will stop this process, so the honest outcome is to say so
        and leave the profile directory alone — the alternative is `shutil.rmtree` under
        a live browser, which is the failure the return-gate exists to prevent.
        """
        with (
            mock.patch.object(chrome, "find_pids", return_value=(11,)),
            self.assertRaisesRegex(CromError, "survived SIGKILL"),
        ):
            chrome.kill(self.profile)

        self.assertEqual(self.signals, [(11, signal.SIGTERM), (11, signal.SIGKILL)])

    def test_stopping_a_profile_that_is_not_running_signals_nothing(self):
        with mock.patch.object(chrome, "find_pids", return_value=()):
            self.assertEqual(chrome.kill(self.profile), ())
        self.assertEqual(self.signals, [])


class ProcessBoundaryTest(unittest.TestCase):
    """The places crom hands work to the operating system.

    All of them translate OS failures into CromError, and all are easy to undo in a
    refactor without any test noticing, because the happy path is unaffected either way.
    """

    def test_a_missing_ps_is_reported_not_crashed_through(self):
        """`scan` is the single process-table reader, so every command that asks about
        process state arrives here — a raw error is a raw error from all of them."""
        with mock.patch("crom.chrome.subprocess.run", side_effect=FileNotFoundError("ps")):
            with self.assertRaisesRegex(CromError, "`ps` was not found"):
                chrome.scan()

    def test_a_failing_ps_is_reported_not_crashed_through(self):
        import subprocess

        failure = subprocess.CalledProcessError(1, ["ps"], stderr="ps: bad option")
        with mock.patch("crom.chrome.subprocess.run", side_effect=failure):
            with self.assertRaisesRegex(CromError, "`ps` exited 1"):
                chrome.scan()

    def test_a_browser_that_cannot_be_started_is_reported_with_its_profile(self):
        """Boundary error translation around `Popen`. Its sibling `_require_port_available`
        was covered when it was written; this was not."""
        with mock.patch("crom.chrome.subprocess.Popen", side_effect=OSError("no such file")):
            with mock.patch("crom.chrome._require_port_available"):
                with self.assertRaises(CromError) as caught:
                    chrome.launch(temp_profile(self))

        self.assertIn("myapp/dev", str(caught.exception))

    def test_a_temp_file_that_cannot_be_opened_is_reported_not_crashed_through(self):
        """The stderr sink is a filesystem boundary, and a launch can reach it on a host
        with a full disk or no descriptors left. Asserting on `launch` rather than on the
        constructor pins the contract — the CLI never sees a raw OSError — so it survives
        a change of which temp-file call the sink is built from."""
        with mock.patch(
            "crom.chrome.tempfile.NamedTemporaryFile", side_effect=OSError("No space left")
        ):
            with mock.patch("crom.chrome._require_port_available"):
                with self.assertRaisesRegex(CromError, "could not open a temporary file"):
                    chrome.launch(temp_profile(self))


class LaunchReadinessTest(unittest.TestCase):
    """What `launch` concludes about a browser that has been started but is not up yet.

    A dead Chrome and a slow Chrome used to be indistinguishable here: the wait loop
    watched only the CDP port, so the only ending it could reach was "timed out". That
    cost 30 seconds of wall clock and then named the wrong cause — the port, when the
    process had been gone since millisecond twenty.
    """

    def setUp(self):
        self.profile = temp_profile(self)
        self.proc = mock.Mock()
        self.proc.poll.return_value = None  # alive, unless a test says otherwise
        for patch in (
            mock.patch("crom.chrome.subprocess.Popen", return_value=self.proc),
            mock.patch("crom.chrome._require_port_available"),
            mock.patch.object(chrome, "find_pids", return_value=(4242,)),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_browser_that_answers_is_returned_with_its_pids(self):
        """The port is rarely up on the first probe, so the loop has to keep waiting."""
        with mock.patch.object(chrome, "_cdp_ready", side_effect=[False, True]):
            self.assertEqual(chrome.launch(self.profile), (4242,))

    def test_a_chrome_that_exits_during_startup_is_reported_with_its_exit_code(self):
        self.proc.poll.return_value = 3
        started = time.monotonic()
        with mock.patch.object(chrome, "_cdp_ready", return_value=False):
            with self.assertRaisesRegex(CromError, "exited 3 during startup"):
                chrome.launch(self.profile)
        elapsed = time.monotonic() - started

        # The point of the whole change: the answer arrives at the speed of the poll
        # interval, not the launch timeout. A generous bound — the regression it guards
        # against is 30s, so anything near it fails here.
        self.assertLess(elapsed, 5.0)

    def test_a_chrome_that_exits_after_opening_the_port_is_a_successful_launch(self):
        """A dead child and a reachable port is a success, and the port is what decides.

        macOS forwards a second launch to the already-running instance and exits 0, so
        "the process we spawned is gone" does not imply "no browser is listening". An
        implementation that fails on a dead child without consulting the port breaks a
        launch that worked. What this does *not* pin down is the order of the two
        observations inside a round — that race is microseconds wide and a static mock
        cannot express it; the ordering rationale lives in `_await_startup`'s docstring.
        """
        self.proc.poll.return_value = 0
        with mock.patch.object(chrome, "_cdp_ready", return_value=True):
            self.assertEqual(chrome.launch(self.profile), (4242,))

    def test_a_live_chrome_that_never_answers_still_reports_the_timeout(self):
        with (
            mock.patch.object(chrome, "LAUNCH_TIMEOUT_SECONDS", 0.3),
            mock.patch.object(chrome, "_cdp_ready", return_value=False),
            self.assertRaisesRegex(CromError, "did not open CDP port"),
        ):
            chrome.launch(self.profile)


# Floods stderr past any pipe buffer, and only then answers on the CDP port — so a sink
# that can block the writer is a sink that never lets this stub become ready.
_FLOOD_THEN_SERVE = """
import socket, sys
sys.stderr.write("x" * 200000)
sys.stderr.flush()
port = int([a for a in sys.argv if a.startswith("--remote-debugging-port=")][0].split("=")[1])
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", port))
server.listen(5)
while True:
    conn, _ = server.accept()
    conn.recv(4096)
    conn.sendall(b"HTTP/1.1 200 OK\\r\\nContent-Length: 2\\r\\n\\r\\n{}")
    conn.close()
"""


class StderrCaptureTest(unittest.TestCase):
    """What a failing launch is able to quote back from the browser that failed.

    These run a real child process. `LaunchReadinessTest` mocks `Popen`, so nothing there
    ever writes to the sink — every assertion about capture would pass against a `launch`
    that still sent stderr to `DEVNULL`. What Chrome says can only be tested by letting
    something actually say it.
    """

    def stub_profile(self, script: str, port: int = 9301) -> ResolvedProfile:
        """A profile whose 'Chrome' is a Python script we control.

        The script goes in a file and only its *path* goes in argv. Passing source with
        `-c` puts its newlines into the process's command line, and `scan()` reads `ps`
        output a line at a time keyed on a leading PID — macOS `ps` folds those newlines
        away, Linux procps prints them, and there the one process spans several lines of
        which only the first carries a PID. `find_pids` would then find nothing for a
        stub that is running perfectly well.

        Cleanup is registered here rather than from whatever `launch` returns, so a stub
        that outlives its test is killed even when the assertion about it fails.
        [LAW:dataflow-not-control-flow] the kill is not conditional on the result it is
        supposed to be independent of — an earlier version stranded a live server on the
        test port precisely when the test failed.
        """
        profile = temp_profile(self, port=port)
        fd, name = tempfile.mkstemp(suffix=".py", prefix="crom-stub-")
        os.write(fd, script.encode())
        os.close(fd)
        self.addCleanup(os.unlink, name)
        self.addCleanup(self.kill_stubs, profile.profile_dir)
        return dataclasses.replace(
            profile,
            chrome_binary=Path(sys.executable),
            argv=(
                sys.executable,
                name,
                f"--user-data-dir={profile.profile_dir}",
                f"--remote-debugging-port={port}",
            ),
        )

    def kill_stubs(self, profile_dir: Path) -> None:
        """Kill whatever is still running against this profile dir, if anything is."""
        for pid in chrome.find_pids_for_dir(profile_dir):
            os.kill(pid, signal.SIGKILL)

    def quoted(self, message: str) -> str:
        """What the error attributes to Chrome, isolated from the command echo.

        Every launch error ends with `Command was: <argv>`, and a stub's argv *is* the
        source that prints the strings it prints — so `assertIn(text, message)` passes on
        the echo alone. Measured: with `stderr=DEVNULL` restored, every assertion in this
        class written against the whole message still passed. Only this block is evidence
        that anything was captured.
        """
        self.assertIn("Chrome said:\n", message)
        return message.split("Chrome said:\n", 1)[1].split("\nCommand was:", 1)[0]

    def test_what_chrome_printed_before_dying_is_quoted_in_the_error(self):
        """The whole point: the actionable line survives the launch that failed."""
        profile = self.stub_profile(
            "import sys; sys.stderr.write('FATAL: unknown switch --nope\\n'); sys.exit(3)"
        )
        started = time.monotonic()
        with self.assertRaises(CromError) as caught:
            chrome.launch(profile)

        self.assertIn("exited 3 during startup", str(caught.exception))
        self.assertIn("FATAL: unknown switch --nope", self.quoted(str(caught.exception)))
        self.assertLess(time.monotonic() - started, 5.0)

    def test_a_silent_death_is_reported_without_an_empty_quotation(self):
        """Nothing to quote reads as nothing, not as a `Chrome said:` heading over blank."""
        profile = self.stub_profile("import sys; sys.exit(1)")
        with self.assertRaises(CromError) as caught:
            chrome.launch(profile)

        self.assertIn("exited 1 during startup", str(caught.exception))
        self.assertNotIn("Chrome said", str(caught.exception))

    def test_a_chatty_browser_that_stays_alive_still_reaches_readiness(self):
        """The hazard capture introduces, and the reason the sink is a file.

        A pipe nobody drains stops the child at the OS buffer — ~64KB on macOS — so it
        blocks on write and never opens CDP. Nothing about that is visible to a mocked
        `Popen`, and it appears only against a browser chatty enough to fill the buffer,
        which Chrome is.

        Neither the port probe nor the process lookup is mocked here, and that is the
        whole design of the test: the stub floods stderr *before* it binds the port, so
        readiness is downstream of surviving the write. Stubbing `_cdp_ready` would cut
        exactly the causal link under test — an earlier version of this test did, and it
        passed against a `stderr=PIPE` that deadlocks a real browser.
        """
        profile = self.stub_profile(_FLOOD_THEN_SERVE, port=9302)
        with mock.patch.object(chrome, "LAUNCH_TIMEOUT_SECONDS", 5.0):
            self.assertTrue(chrome.launch(profile))

    def test_the_quotation_is_bounded_by_the_tail_of_what_was_printed(self):
        """A browser that logged all day is quoted by its ending, not its whole history."""
        profile = self.stub_profile(
            "import sys; sys.stderr.write('n' * 50000 + 'THE-LAST-WORD'); sys.exit(2)"
        )
        with self.assertRaises(CromError) as caught:
            chrome.launch(profile)

        section = self.quoted(str(caught.exception))
        self.assertIn("THE-LAST-WORD", section)
        # 50KB printed, and the error stays the size of the tail plus its indent.
        self.assertLess(len(section), chrome.STDERR_TAIL_BYTES + 200)

    def test_terminal_escapes_in_what_chrome_printed_do_not_reach_the_message(self):
        """Quoting Chrome hands its bytes to a terminal, which `DEVNULL` never did.

        Chrome's log lines carry page-derived text, so the bytes are not all Chrome's
        own. Left raw, an escape sequence could clear the screen or hide the error it is
        printed inside — the message would be attacker-shaped rather than Chrome-shaped.
        """
        profile = self.stub_profile(
            r"import sys; sys.stderr.write('\x1b[2J\x1b[1;31mFAKE PROMPT\x1b[0m\x07 real"
            r" detail\n'); sys.exit(4)"
        )
        with self.assertRaises(CromError) as caught:
            chrome.launch(profile)

        section = self.quoted(str(caught.exception))
        self.assertIn("real detail", section)  # the diagnostic survives
        self.assertNotIn("\x1b", section)  # the escapes do not
        self.assertNotIn("\x07", section)

    def test_capture_leaves_no_file_behind(self):
        """Capture costs a successful launch what `DEVNULL` did: nothing anyone can find."""
        before = set(Path(tempfile.gettempdir()).glob("crom-chrome-stderr-*"))
        profile = self.stub_profile("import sys; sys.stderr.write('noise'); sys.exit(1)")
        with self.assertRaises(CromError):
            chrome.launch(profile)

        after = set(Path(tempfile.gettempdir()).glob("crom-chrome-stderr-*"))
        self.assertEqual(after - before, set())


if __name__ == "__main__":
    unittest.main()
