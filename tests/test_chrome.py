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
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from itertools import chain, repeat
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
        # The port half of the stop contract is stubbed free here so each test states the
        # one fact it is about. Left real, every test in this class would bind port 9300
        # on the machine running it and start failing the day something else wants it.
        # `PortIsPartOfBeingStoppedTest` covers the same wait against a genuine socket.
        port = mock.patch.object(chrome, "_port_is_free", lambda _port: True)
        port.start()
        self.addCleanup(port.stop)

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
            self.assertRaisesRegex(CromError, "SIGKILL"),
        ):
            chrome.kill(self.profile)

        self.assertEqual(self.signals, [(11, signal.SIGTERM), (11, signal.SIGKILL)])

    def test_stopping_a_profile_that_is_not_running_signals_nothing(self):
        with mock.patch.object(chrome, "find_pids", return_value=()):
            self.assertEqual(chrome.kill(self.profile), ())
        self.assertEqual(self.signals, [])

    def test_a_port_the_browser_has_not_let_go_of_yet_is_waited_out(self):
        """The wait covers the port, not just the process table.

        The scans model the ordinary shape of the race this exists for: the browser is
        gone from `ps` on the first look, and the CDP socket outlives it by a moment. If
        the wait ended at the process table it would return inside that moment, and the
        relaunch that follows would find the port taken. Waiting costs one more round and
        needs no SIGKILL — the browser is already dead, only its socket is not.
        """
        freed = chain([False, False], repeat(True))
        with (
            mock.patch.object(chrome, "find_pids", side_effect=lambda p: (11,) if not self.signals else ()),
            mock.patch.object(chrome, "_port_is_free", lambda _port: next(freed)),
        ):
            killed = chrome.kill(self.profile)

        self.assertEqual(killed, (11,))
        self.assertEqual(self.signals, [(11, signal.SIGTERM)])  # never escalated

    def test_a_port_still_held_once_the_processes_are_gone_is_not_a_stop(self):
        """[LAW:no-silent-failure] an empty process table is not the whole promise.

        Returning here would hand `up` a port it cannot bind and blame the next command
        for it. The message has to say which half failed, because a stop that left a
        process and a stop that left a socket are chased in completely different places.
        """
        with (
            mock.patch.object(chrome, "find_pids", return_value=()),
            mock.patch.object(chrome, "_port_is_free", lambda _port: False),
            self.assertRaises(CromError) as caught,
        ):
            chrome.kill(self.profile)

        message = str(caught.exception)
        self.assertIn("port 9300 is still held", message)
        self.assertIn("lsof", message)  # the message tells the user how to find the holder
        self.assertNotIn("still running", message)  # nothing was, and it must not say so


class PortCheckBoundaryTest(unittest.TestCase):
    """The socket the port check needs is an OS resource like any other in this module.

    A refused bind is an answer; a socket that cannot be made is not. Both arrive as
    `OSError`, and only the first one means anything about the port.
    """

    def test_a_probe_socket_that_cannot_be_made_is_reported_not_crashed_through(self):
        with (
            mock.patch.object(chrome.socket, "socket", side_effect=OSError("too many open files")),
            self.assertRaisesRegex(CromError, "could not check whether port 9300 is free"),
        ):
            chrome._port_is_free(9300)

    def test_a_stop_inherits_that_report_rather_than_a_traceback(self):
        """`down` and `rm` sit behind this now, and both live inside the exit-code contract."""
        with (
            mock.patch.object(chrome, "find_pids", return_value=()),
            mock.patch.object(chrome.socket, "socket", side_effect=OSError("too many open files")),
            self.assertRaises(CromError),
        ):
            chrome.kill(RequirePortAvailableTest._profile(9300))


class PortIsPartOfBeingStoppedTest(unittest.TestCase):
    """The same wait, against a real socket rather than a stubbed predicate.

    `KillTest` stubs `_port_is_free` so its scenarios stay about the escalation. That
    stub is only worth anything if the real predicate is wired to a real kernel, and the
    wiring is exactly what a refactor can quietly cut without any mocked test noticing.
    """

    def test_a_profile_whose_port_will_not_bind_is_reported_by_that_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
            holder.bind(("127.0.0.1", 0))
            holder.listen(1)
            taken = holder.getsockname()[1]
            # No mocked process table: no Chrome anywhere holds this fabricated
            # user-data-dir, so the process half is genuinely satisfied and only the
            # socket stands between this profile and a clean stop.
            profile = RequirePortAvailableTest._profile(taken)

            with (
                mock.patch.object(chrome, "SHUTDOWN_TIMEOUT_SECONDS", 0.2),
                self.assertRaises(CromError) as caught,
            ):
                chrome.kill(profile)

        self.assertIn(f"port {taken} is still held", str(caught.exception))


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

    def test_a_sink_that_cannot_be_opened_is_reported_not_crashed_through(self):
        """The stderr sink is a filesystem boundary, and a launch can reach it on a host
        with a full disk or no descriptors left. Asserting on `launch` rather than on the
        constructor pins the contract — the CLI never sees a raw OSError — so it survives
        a change of which call the sink is opened by."""
        with mock.patch.object(Path, "open", side_effect=OSError("No space left")):
            with mock.patch("crom.chrome._require_port_available"):
                with self.assertRaisesRegex(CromError, "could not open .*crom-stderr.log"):
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
        with mock.patch.object(
            chrome, "_probe_port", side_effect=[chrome._Silent(), chrome._Answered()]
        ):
            self.assertEqual(chrome.launch(self.profile), (4242,))

    def test_a_chrome_that_exits_during_startup_is_reported_with_its_exit_code(self):
        self.proc.poll.return_value = 3
        started = time.monotonic()
        with mock.patch.object(chrome, "_probe_port", return_value=chrome._Silent()):
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
        with mock.patch.object(chrome, "_probe_port", return_value=chrome._Answered()):
            self.assertEqual(chrome.launch(self.profile), (4242,))

    def test_a_live_chrome_that_never_answers_names_the_timeout_and_says_it_is_alive(self):
        """Being alive is what separates this ending from a death, so the message says so.

        Without it the two readings differ only by absence — the reader has to notice that
        no exit code was named and infer the rest, which is exactly the inspection this
        message exists to save them.
        """
        with (
            mock.patch.object(chrome, "LAUNCH_TIMEOUT_SECONDS", 0.3),
            mock.patch.object(chrome, "_probe_port", return_value=chrome._Silent()),
            self.assertRaisesRegex(CromError, "still running but did not open CDP port"),
        ):
            chrome.launch(self.profile)

    def test_a_stranger_on_the_port_is_not_reported_as_a_launched_browser(self):
        """The silent failure this outcome exists to end.

        A bool probe counted any HTTP 200 as readiness, so `launch` returned pids for a
        profile whose port belonged to something else and the trouble surfaced later, at
        whichever CDP client could not connect. The mock cannot show that: `find_pids` is
        stubbed to succeed here, so a `launch` that still trusted a bare answer would
        return `(4242,)` and this assertion is the whole of what stops it.
        """
        with mock.patch.object(
            chrome, "_probe_port", return_value=chrome._AnsweredByStranger("HTTP 404 Not Found")
        ):
            with self.assertRaises(CromError) as caught:
                chrome.launch(self.profile)

        message = str(caught.exception)
        self.assertIn("not as a Chrome DevTools endpoint", message)
        self.assertIn("HTTP 404 Not Found", message)  # what answered, quoted back
        self.assertIn(f"lsof -nP -iTCP:{self.profile.port}", message)  # and how to find it

    def test_a_stranger_ends_the_wait_without_serving_out_the_timeout(self):
        """A listener holding the port is why Chrome cannot have it; waiting changes that
        for nobody. Distinct from the timeout ending, which is worth its full 30s."""
        started = time.monotonic()
        with (
            mock.patch.object(chrome, "LAUNCH_TIMEOUT_SECONDS", 30.0),
            mock.patch.object(chrome, "_probe_port", return_value=chrome._AnsweredByStranger("x")),
            self.assertRaises(CromError),
        ):
            chrome.launch(self.profile)
        self.assertLess(time.monotonic() - started, 5.0)


# The reply a real Chrome/152 serves on /json/version, kept to the field crom keys on.
# `webSocketDebuggerUrl` is what makes an endpoint drivable, so a stub that omits it is a
# stranger by the same rule that would reject a dev server — which is why this fixture
# cannot be shortened to the `{}` it used to be.
_CDP_VERSION_DOCUMENT = (
    '{"Browser": "Chrome/152.0.7977.66", "Protocol-Version": "1.3", '
    '"webSocketDebuggerUrl": "ws://127.0.0.1/devtools/browser/stub-uuid"}'
)

# Fills any pipe buffer before the port is ever bound, so a sink that can block the writer
# is a sink that never lets the stub become ready.
_FLOOD_STDERR = 'sys.stderr.write("x" * 200000); sys.stderr.flush()'


# Trickles one byte at a time, slower than any real server and faster than the socket
# timeout — the gap a per-`recv` timeout cannot close, since every single read is legal.
_TRICKLE_FOREVER = '''
import socket, sys, time
port = int([a for a in sys.argv if a.startswith("--remote-debugging-port=")][0].split("=")[1])
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", port))
server.listen(5)
while True:
    conn, _ = server.accept()
    conn.recv(4096)
    try:
        conn.sendall(b"HTTP/1.1 200 OK\\r\\n\\r\\n")
        while True:
            conn.sendall(b"z")
            time.sleep(0.5)
    except OSError:
        pass
    conn.close()
'''

# Answers on the CDP port without speaking HTTP at all — the shape of any other service
# that happens to bind the port, and once the shape that escaped `launch` as a traceback.
_NOT_HTTP = '''
import socket, sys
port = int([a for a in sys.argv if a.startswith("--remote-debugging-port=")][0].split("=")[1])
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", port))
server.listen(5)
while True:
    conn, _ = server.accept()
    conn.recv(4096)
    conn.sendall(b"SSH-2.0-OpenSSH_9.0\\r\\n")
    conn.close()
'''

# Puts terminal escapes in the status line's reason phrase, which the stranger chooses
# just as freely as the body — and which reaches the message down a different arm.
_ESCAPES_IN_THE_REASON = r'''
import socket, sys
port = int([a for a in sys.argv if a.startswith("--remote-debugging-port=")][0].split("=")[1])
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", port))
server.listen(5)
while True:
    conn, _ = server.accept()
    conn.recv(4096)
    conn.sendall(b"HTTP/1.1 404 \x1b[2J\x1b[1;31mLAUNCH OK\x1b[0m\x07 evil-reason\r\n"
                 b"Content-Length: 0\r\n\r\n")
    conn.close()
'''

# Paces bytes just *under* the per-recv timeout rather than well under it, so a read is
# reliably in flight when the overall deadline passes. `_TRICKLE_FOREVER` at 0.5s never
# straddles that boundary, so it cannot show what one read costs after the clock runs out.
_TRICKLE_PAST_THE_DEADLINE = '''
import socket, sys, time
port = int([a for a in sys.argv if a.startswith("--remote-debugging-port=")][0].split("=")[1])
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", port))
server.listen(5)
while True:
    conn, _ = server.accept()
    conn.recv(4096)
    try:
        conn.sendall(b"HTTP/1.1 200 OK\\r\\n\\r\\n")
        while True:
            conn.sendall(b"z")
            time.sleep(0.9)
    except OSError:
        pass
    conn.close()
'''

# Sends the headers, then pauses longer than one recv slice but well inside the overall
# deadline, then sends a perfectly good CDP document. A browser on a loaded machine, in
# other words — and the case where treating a per-recv timeout as "the peer is finished"
# condemns a healthy launch on the strength of a prefix.
_HEADERS_THEN_A_PAUSE = '''
import socket, sys, time
port = int([a for a in sys.argv if a.startswith("--remote-debugging-port=")][0].split("=")[1])
body = (b'{"Browser": "Chrome/152.0.7977.66", '
        b'"webSocketDebuggerUrl": "ws://127.0.0.1/devtools/browser/stub-uuid"}')
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", port))
server.listen(5)
while True:
    conn, _ = server.accept()
    conn.recv(4096)
    try:
        conn.sendall(b"HTTP/1.1 200 OK\\r\\nContent-Length: %d\\r\\n\\r\\n" % len(body))
        time.sleep(1.4)
        conn.sendall(body)
    except OSError:
        pass
    conn.close()
'''

# Trickles the *status line* one byte at a time and never completes it. The phase an
# HTTP client parses before it hands anything back, and so the phase a deadline that
# starts at the response body cannot reach — measured, this held a probe past 12s.
_TRICKLE_THE_STATUS_LINE = '''
import socket, sys, time
port = int([a for a in sys.argv if a.startswith("--remote-debugging-port=")][0].split("=")[1])
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", port))
server.listen(5)
while True:
    conn, _ = server.accept()
    conn.recv(4096)
    try:
        for ch in b"HTTP/1.1 200 OK":
            conn.sendall(bytes([ch]))
            time.sleep(0.9)
    except OSError:
        pass
    conn.close()
'''

# Accepts the connection and closes it having written nothing at all. The one case that
# has to stay retryable rather than terminal: something answered the knock, but nothing on
# this port has spoken, which is indistinguishable from a browser that is not up yet.
_ACCEPT_AND_CLOSE = '''
import socket, sys
port = int([a for a in sys.argv if a.startswith("--remote-debugging-port=")][0].split("=")[1])
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", port))
server.listen(5)
while True:
    conn, _ = server.accept()
    conn.close()
'''

# Announces no length and never closes, so a client that reads to the end of the body
# reads until its own socket timeout instead. Not a `_serving_stub`, because what it does
# is exactly the thing that stub cannot do: refuse to finish.
_STREAM_FOREVER = '''
import socket, sys
port = int([a for a in sys.argv if a.startswith("--remote-debugging-port=")][0].split("=")[1])
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", port))
server.listen(5)
while True:
    conn, _ = server.accept()
    conn.recv(4096)
    try:
        conn.sendall(b"HTTP/1.1 200 OK\\r\\n\\r\\n")
        while True:
            conn.sendall(b"z" * 4096)
    except OSError:
        pass
    conn.close()
'''


# Chrome's own launcher shape: the process crom starts hands the browser off and exits,
# so the pid crom holds dies while a live browser goes on serving the port. Timed to land
# the parent's death *inside* the first probe — the first connection is accepted and never
# answered, so that probe spends `PORT_REPLY_SECONDS` and the 0.5s exit falls squarely in
# the middle of it. That is the whole fixture: everything else here exists to make those
# two clocks overlap. Not a `_serving_stub`, which has no way to die halfway through
# serving. The stub's own timing is what makes the case reachable, so it is written down
# here rather than left to the scheduler. [LAW:no-ambient-temporal-coupling]
_HANDS_OFF_AND_DIES = f'''
import os, socket, sys, time
port = int([a for a in sys.argv if a.startswith("--remote-debugging-port=")][0].split("=")[1])
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", port))
server.listen(5)
if os.fork():
    time.sleep(0.5)
    os._exit(3)
body = {_CDP_VERSION_DOCUMENT!r}.encode()
held, _ = server.accept()
while True:
    conn, _ = server.accept()
    conn.recv(4096)
    conn.sendall(b"HTTP/1.1 200 OK\\r\\nContent-Length: %d\\r\\n\\r\\n" % len(body) + body)
    conn.close()
'''


def _serving_stub(body: str, preamble: str = "") -> str:
    """A stub 'Chrome' that binds the CDP port and answers every request with `body`.

    What it serves is a parameter because readiness now turns on the *content* of the
    reply rather than on the fact of one: crom asks for a DevTools version document and
    reads anything else as a stranger holding the port. One stub covers both sides of that
    line, and `preamble` carries whatever the stub must do before it binds — as a value,
    so there is no second stub shape and no flag deciding which one you get.
    """
    return f'''
import socket, sys
{preamble}
port = int([a for a in sys.argv if a.startswith("--remote-debugging-port=")][0].split("=")[1])
body = {body!r}.encode()
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", port))
server.listen(5)
while True:
    conn, _ = server.accept()
    conn.recv(4096)
    conn.sendall(b"HTTP/1.1 200 OK\\r\\nContent-Length: %d\\r\\n\\r\\n" % len(body) + body)
    conn.close()
'''


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

    def bounded(self, call: Callable[[], object], seconds: float = 30.0) -> object:
        """Run `call` under a wall clock, returning what it returned or raising what it
        raised — but failing the test outright if it never came back.

        Every regression around reading the port shares a failure mode: it does not fail,
        it hangs. A read that never returns takes the whole suite with it and reports
        nothing, which is the one outcome that cannot describe itself. On a joinable
        daemon thread the bound is enforceable and the failure names the test that hit it.

        Which call is a parameter rather than a helper per call site, so `launch` and
        `_probe_port` are bounded by the same code. [LAW:composability]
        """
        outcome: list[tuple[str, object]] = []

        def attempt() -> None:
            try:
                outcome.append(("returned", call()))
            except BaseException as e:  # noqa: BLE001 — re-raised below, on the test's thread
                outcome.append(("raised", e))

        worker = threading.Thread(target=attempt, daemon=True)
        worker.start()
        worker.join(seconds)
        self.assertFalse(worker.is_alive(), f"the call did not return within {seconds:g}s")
        kind, result = outcome[0]
        if kind == "raised":
            # Back on the calling thread, where `assertRaises` can see it and a genuine
            # error is a test failure rather than a message on stderr nobody reads.
            raise result  # type: ignore[misc]
        return result

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
        readiness is downstream of surviving the write. Stubbing `_probe_port` would cut
        exactly the causal link under test — an earlier version of this test did, and it
        passed against a `stderr=PIPE` that deadlocks a real browser.
        """
        profile = self.stub_profile(
            _serving_stub(_CDP_VERSION_DOCUMENT, _FLOOD_STDERR), port=9302
        )
        with mock.patch.object(chrome, "LAUNCH_TIMEOUT_SECONDS", 5.0):
            self.assertTrue(chrome.launch(profile))

    def test_a_real_listener_that_is_not_chrome_is_diagnosed_as_the_stranger_it_is(self):
        """The fourth ending, against a process actually holding the port.

        `LaunchReadinessTest` reaches this arm through a stubbed `_probe_port`, so nothing
        there exercises the parse that decides a reply is not a DevTools document — the
        assertions would pass against a probe that still answered on HTTP status alone.
        Only a listener that really answers 200 with something else can show that.

        It also has stderr to its name, because a stranger on the port does not stop
        Chrome from having said something worth reading — the quotation belongs in this
        arm exactly as it does in the other two.

        The full 30s timeout is left standing so the clock is part of the assertion: a
        stranger ends the wait when it is seen, and the listener holding the port will not
        yield inside the deadline, so serving it out buys nobody anything. The mocked
        sibling test pins that against a stubbed `_probe_port`; this one pins it for a
        diagnosis that had to be parsed out of real bytes first. It holds only because
        this stranger finishes its reply — one that trickles without ever finishing is a
        prefix, and a prefix ends on the launch timeout by design.
        """
        profile = self.stub_profile(
            _serving_stub(
                "<!DOCTYPE html>\n<title>some other dev server</title>",
                'sys.stderr.write("chrome had something to say too\\n")',
            ),
            port=9303,
        )
        started = time.monotonic()
        with self.assertRaises(CromError) as caught:
            self.bounded(lambda: chrome.launch(profile), seconds=60.0)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 10.0)  # against the 30s a launch is otherwise given
        message = str(caught.exception)
        self.assertIn("not as a Chrome DevTools endpoint", message)
        self.assertIn("<!DOCTYPE html>", message)  # what answered, quoted back
        self.assertIn("chrome had something to say too", self.quoted(message))

    def test_a_stranger_cannot_write_the_error_message_it_appears_in(self):
        """Quoting the stranger hands a terminal bytes chosen by whatever holds the port.

        This is the less obvious half of that hazard: Chrome's stderr is at least Chrome's,
        but a listener crom did not launch is unvetted by definition, and it gets to pick
        every byte of what the message repeats. It is sanitised by the same code as
        Chrome's stderr, and this is the test that says so out loud.
        """
        profile = self.stub_profile(
            _serving_stub("\x1b[2J\x1b[1;31mLAUNCH OK\x1b[0m\x07 evil-server"), port=9304
        )
        with mock.patch.object(chrome, "LAUNCH_TIMEOUT_SECONDS", 5.0):
            with self.assertRaises(CromError) as caught:
                chrome.launch(profile)

        message = str(caught.exception)
        self.assertIn("evil-server", message)  # the diagnostic survives
        self.assertNotIn("\x1b", message)  # the escapes do not
        self.assertNotIn("\x07", message)

    def test_a_stranger_cannot_smuggle_escapes_through_the_status_line_either(self):
        """The body is not the only text the stranger writes.

        A non-200 reply reaches the message down its own arm, carrying a reason phrase the
        stranger chose. Sanitising the body and not the reason leaves the same hole in a
        second doorway — which is why every arm goes through `_summarise` rather than each
        remembering. This is the arm that was already forgotten once.
        """
        profile = self.stub_profile(_ESCAPES_IN_THE_REASON, port=9311)
        with mock.patch.object(chrome, "LAUNCH_TIMEOUT_SECONDS", 5.0):
            with self.assertRaises(CromError) as caught:
                chrome.launch(profile)

        message = str(caught.exception)
        self.assertIn("404", message)  # the diagnosis survives
        self.assertIn("evil-reason", message)
        self.assertNotIn("\x1b", message)  # the escapes do not
        self.assertNotIn("\x07", message)

    def test_a_stranger_serving_a_flood_cannot_become_the_error_message(self):
        """A server holding the port answers with whatever size it likes, and the message
        is headed for a terminal — so what is shown is one bounded line of it."""
        profile = self.stub_profile(_serving_stub("z" * 400000), port=9305)
        with mock.patch.object(chrome, "LAUNCH_TIMEOUT_SECONDS", 5.0):
            with self.assertRaises(CromError) as caught:
                chrome.launch(profile)

        served = str(caught.exception).split("it served: ", 1)[1].split(". Something", 1)[0]
        self.assertLessEqual(len(served), chrome.PORT_REPLY_SUMMARY_CHARS)

    def test_a_stranger_that_never_stops_talking_is_diagnosed_anyway(self):
        """The other bound, and the one the message cannot show: how much crom reads.

        This server announces no length and never closes, so a read that asks for
        everything never comes back at all: the socket timeout is per-recv and data keeps
        arriving, so nothing ever times out and `up` hangs for as long as the stranger
        cares to talk. Measured — restoring the unbounded read hangs this suite outright
        rather than failing it. Reading a bounded slice is what lets crom answer a server
        that has no intention of finishing, and truncation costs nothing, because a
        truncated reply is not a DevTools document and neither was the whole.
        """
        profile = self.stub_profile(_STREAM_FOREVER, port=9306)
        with mock.patch.object(chrome, "LAUNCH_TIMEOUT_SECONDS", 5.0):
            with self.assertRaises(CromError) as caught:
                self.bounded(lambda: chrome.launch(profile))

        self.assertIn("not as a Chrome DevTools endpoint", str(caught.exception))

    def test_a_launch_against_a_trickling_peer_ends_on_its_own_timeout(self):
        """The bound a size cap cannot supply, and the reason `_read_reply` holds a clock.

        This server sends one byte every 0.5s, so every individual `recv` completes well
        inside the 1s socket timeout and nothing ever trips it. Filling the size cap that
        way would take hours, and `_await_startup` cannot intervene because it checks its
        deadline *between* probes and this is inside one — so `up` blocks far past the 30s
        it documents. Only a wall clock inside the read closes that gap.

        It ends on the launch timeout rather than by naming a stranger, and that is the
        deliberate trade: a reply that never finishes is a prefix, and a prefix cannot be
        told from a slow browser's. Every stranger anyone actually meets completes its
        reply or closes the socket, and keeps the fast, specific diagnosis.
        """
        profile = self.stub_profile(_TRICKLE_FOREVER, port=9307)
        with mock.patch.object(chrome, "LAUNCH_TIMEOUT_SECONDS", 3.0):
            # Bounded well under the hours the regression takes, and well over the seconds
            # the fix takes, so the gap between them is what the assertion reads.
            with self.assertRaises(CromError) as caught:
                self.bounded(lambda: chrome.launch(profile), seconds=60.0)

        self.assertIn("did not open CDP port", str(caught.exception))

    def test_one_probe_costs_no_more_than_the_ceiling_the_constants_name(self):
        """The bound is a sum, and this is the test that holds it to the sum.

        `_read_reply` checks its deadline between reads, so a read in flight when the
        deadline passes still runs to its own timeout — the ceiling is
        `PORT_REPLY_SECONDS + PORT_RECV_SECONDS`, not the first alone. A fixture pacing
        well under the recv timeout never puts a read in that position and so would pass
        against any ceiling at all; this one paces just under it, and the lower assertion
        is what proves the straddle actually happened rather than the test flattering
        itself.
        """
        profile = self.stub_profile(_TRICKLE_PAST_THE_DEADLINE, port=9312)
        server = subprocess.Popen(
            profile.argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self.addCleanup(server.kill)
        listening = time.monotonic() + 10
        while time.monotonic() < listening:
            with socket.socket() as knock:
                if knock.connect_ex(("127.0.0.1", profile.port)) == 0:
                    break
            time.sleep(0.05)

        started = time.monotonic()
        answer = self.bounded(lambda: chrome._probe_port(profile.port))
        elapsed = time.monotonic() - started

        # Out of time mid-sentence, so the prefix is not evidence and stays retryable.
        self.assertIsInstance(answer, chrome._Silent)
        # Straddled: the read outlived the deadline, which is the case under test.
        self.assertGreater(elapsed, chrome.PORT_REPLY_SECONDS)
        # And was still capped by the ceiling the constants document, slack for scheduling.
        ceiling = chrome.PORT_REPLY_SECONDS + chrome.PORT_RECV_SECONDS
        self.assertLess(elapsed, ceiling + 1.0)

    def test_a_status_line_that_never_finishes_cannot_outlast_the_ceiling(self):
        """The hang the body-only deadline left open, one HTTP phase earlier.

        Everything before the body — connect, request, status line, headers — was parsed
        by a borrowed HTTP client whose only bound was per-recv, so a status line paced
        under that timeout kept every read legal while the probe ran for hours. Measured
        at 12s and still going, against a ceiling documented as 3s. The fixtures that
        trickle a body cannot reach this: they all send a complete status line first.
        """
        profile = self.stub_profile(_TRICKLE_THE_STATUS_LINE, port=9314)
        server = subprocess.Popen(
            profile.argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self.addCleanup(server.kill)
        listening = time.monotonic() + 10
        while time.monotonic() < listening:
            with socket.socket() as knock:
                if knock.connect_ex(("127.0.0.1", profile.port)) == 0:
                    break
            time.sleep(0.05)

        started = time.monotonic()
        answer = self.bounded(lambda: chrome._probe_port(profile.port))
        elapsed = time.monotonic() - started

        # Never a complete reply, so never evidence — and crucially never a hang either,
        # which is the whole point: bounded, and retryable rather than terminal.
        self.assertIsInstance(answer, chrome._Silent)
        ceiling = chrome.PORT_REPLY_SECONDS + chrome.PORT_RECV_SECONDS
        self.assertLess(elapsed, ceiling + 1.0)

    def test_a_browser_that_pauses_mid_reply_is_not_condemned_as_a_stranger(self):
        """A per-recv timeout says nothing about the peer being finished.

        This server sends its headers, waits longer than one recv slice but well inside
        the overall deadline, and then sends a perfectly good CDP document — a browser on
        a loaded machine. Treating that timeout as the end of the reply hands `_classify`
        a headers-only prefix, which is not a DevTools document, so a healthy launch fails
        permanently as "answering, but not as a Chrome DevTools endpoint". The three
        trickle fixtures all pace *under* the slice on purpose, so none of them can reach
        this; it needs a pause that steps over it.
        """
        self.assertGreater(1.4, chrome.PORT_RECV_SECONDS)  # long enough to time a recv out
        self.assertLess(1.4, chrome.PORT_REPLY_SECONDS)  # short enough to be in budget
        profile = self.stub_profile(_HEADERS_THEN_A_PAUSE, port=9315)
        with mock.patch.object(chrome, "LAUNCH_TIMEOUT_SECONDS", 10.0):
            self.assertTrue(chrome.launch(profile))

    def test_a_peer_that_accepts_and_says_nothing_stays_retryable(self):
        """Silence and a stranger are the retryable and the terminal answer, and the line
        between them has to be drawn by what was said, not by what was raised.

        A peer that accepts and closes without writing used to reach `_Silent` only
        because `RemoteDisconnected` inherits from `ConnectionResetError` and so was
        caught by the `OSError` arm sitting above the `HTTPException` one — an ordering
        called load-bearing in a comment and pinned by nothing. Reading the socket
        directly makes it a read of zero bytes, but the boundary is worth a test either
        way, since getting it wrong aborts launches that only needed another 100ms.
        """
        profile = self.stub_profile(_ACCEPT_AND_CLOSE, port=9313)
        server = subprocess.Popen(
            profile.argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self.addCleanup(server.kill)
        listening = time.monotonic() + 10
        while time.monotonic() < listening:
            with socket.socket() as knock:
                if knock.connect_ex(("127.0.0.1", profile.port)) == 0:
                    break
            time.sleep(0.05)

        self.assertIsInstance(chrome._probe_port(profile.port), chrome._Silent)

    def test_a_browser_that_outlives_the_process_crom_started_is_still_a_launch(self):
        """A launcher that hands off and exits is a browser, not a dead Chrome.

        This is the one thing a mocked `Popen` cannot say. `_await_startup` samples
        liveness *before* the port, so a child that exits while a probe is in flight is
        still reported as `None` for that round and the port gets asked once more before
        the launch is called dead. A static mock answers identically at both observation
        points, so swapping the two lines leaves every other test in this file green —
        only a process that really dies between them can tell the orders apart.

        The stub makes that window wide instead of racing it: its first connection is
        accepted and never answered, so probe one spends `PORT_REPLY_SECONDS`, and the
        parent exits half a second into those two. Probe two then meets the forked child
        serving CDP, and the launch succeeds against a pid that has been dead for a
        second and a half. Sampling the port first fails it with `exited 3` instead.
        """
        profile = self.stub_profile(_HANDS_OFF_AND_DIES, port=9316)
        with mock.patch.object(chrome, "LAUNCH_TIMEOUT_SECONDS", 10.0):
            pids = self.bounded(lambda: chrome.launch(profile), seconds=30.0)

        self.assertTrue(pids)

    def test_a_listener_that_does_not_speak_http_is_named_by_what_it_said(self):
        """A service holding the port without speaking HTTP is the plainest form of the
        thing this outcome was added to name, so it gets the plainest diagnosis: the
        banner it actually sent. Through an HTTP client this raised `BadStatusLine`, which
        is neither an `OSError` nor a `URLError` and so escaped `launch` as a traceback."""
        profile = self.stub_profile(_NOT_HTTP, port=9308)
        with mock.patch.object(chrome, "LAUNCH_TIMEOUT_SECONDS", 5.0):
            with self.assertRaises(CromError) as caught:
                chrome.launch(profile)

        message = str(caught.exception)
        self.assertIn("not as a Chrome DevTools endpoint", message)
        self.assertIn("SSH-2.0-OpenSSH", message)  # who is actually on the port

    def test_a_chrome_whose_own_flags_inflate_its_reply_is_still_recognised(self):
        """A configured `--user-agent` lands in `/json/version`, and the document grows.

        Measured against Chrome/152: a 12,000-character agent — an ordinary thing to put
        in `[defaults]` — makes the document 12,315 bytes. Read through a smaller cap it
        stops parsing as JSON, and crom fails a launch that worked by calling its own
        browser a stranger. The size cap has to clear a document the user can inflate.
        """
        document = (
            '{"Browser": "Chrome/152.0.7977.66", "User-Agent": "' + "x" * 12000 + '", '
            '"webSocketDebuggerUrl": "ws://127.0.0.1/devtools/browser/stub-uuid"}'
        )
        self.assertGreater(len(document), 8192)  # the bound this used to be read through
        profile = self.stub_profile(_serving_stub(document), port=9309)
        with mock.patch.object(chrome, "LAUNCH_TIMEOUT_SECONDS", 5.0):
            self.assertTrue(chrome.launch(profile))

    def test_a_stranger_cannot_answer_a_launch_with_a_traceback(self):
        """Nesting deeper than the JSON decoder will follow raises `RecursionError`, which
        is not a `ValueError` and so is not caught by the parse's other arms. It fits
        inside the size cap now that the cap clears an inflated real document."""
        profile = self.stub_profile(_serving_stub("[" * 20000 + "]" * 20000), port=9310)
        with mock.patch.object(chrome, "LAUNCH_TIMEOUT_SECONDS", 5.0):
            with self.assertRaises(CromError) as caught:
                chrome.launch(profile)

        self.assertIn("not as a Chrome DevTools endpoint", str(caught.exception))

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

    def test_a_sink_that_vanished_is_said_out_loud_and_crashes_nothing(self):
        """`tail` runs on every launch, so an unreadable sink must not take the launch
        with it — and must not pass itself off as a silent Chrome either. Those are two
        facts, and `""` is already spoken for by the second."""
        profile = self.stub_profile("import sys; sys.stderr.write('lost'); sys.exit(6)")
        with mock.patch("crom.chrome.open", side_effect=OSError("gone"), create=True):
            with self.assertRaises(CromError) as caught:
                chrome.launch(profile)

        message = str(caught.exception)
        self.assertIn("exited 6 during startup", message)  # the real outcome survives
        self.assertIn("could not read what Chrome printed", self.quoted(message))

    def test_the_sink_is_left_in_the_profile_under_a_name_the_error_gives_out(self):
        """The sink used to be an unlinked temp file, so everything Chrome said beyond the
        quoted tail died with the browser. Naming it is the whole ticket: `du` can see it,
        and a reader who needs more than the tail is told where the rest is."""
        profile = self.stub_profile(
            "import sys; sys.stderr.write('noise\\n'); sys.exit(1)", port=9317
        )
        with self.assertRaises(CromError) as caught:
            chrome.launch(profile)

        log = profile.profile_dir / chrome.STDERR_FILENAME
        self.assertEqual(log.read_text(), "noise\n")
        self.assertIn(str(log), str(caught.exception))

    def test_a_launch_does_not_inherit_the_previous_launchs_words(self):
        """Truncation is what keeps a named file honest: it holds what *this* browser
        said. Appending would make the quoted tail a mixture of two runs — the one thing
        worse than no output is output from a launch that is not the one being diagnosed.

        The marker travels in `env` so that both launches are the same stub against the
        same directory and port, which is the state a second `crom up` actually meets.
        """
        stub = self.stub_profile(
            "import os, sys; sys.stderr.write(os.environ['MARK'] + '\\n'); sys.exit(1)",
            port=9318,
        )
        for mark in ("FIRST", "SECOND"):
            with self.assertRaises(CromError):
                chrome.launch(dataclasses.replace(stub, env={"MARK": mark}))

        self.assertEqual((stub.profile_dir / chrome.STDERR_FILENAME).read_text(), "SECOND\n")


class SingletonHolderTest(unittest.TestCase):
    """Every state Chrome's `SingletonLock` can be found in, and what each one means.

    The lock is how crom answers "is anything writing this user-data-dir" for a browser
    it did not launch — the user's own Chrome carries no `--user-data-dir` in its argv,
    so `scan` cannot see it at all. Held or free is the whole answer, and the cases that
    cannot be resolved locally count as held: refusing a still directory costs a command,
    copying a moving one costs a profile.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def lock(self, target: str) -> None:
        os.symlink(target, self.dir / chrome.SINGLETON_LOCK)

    def dead_pid(self) -> int:
        """A pid that has certainly exited — spawned, waited on, and reaped."""
        proc = subprocess.Popen([sys.executable, "-c", ""])
        proc.wait()
        return proc.pid

    def test_no_lock_means_nothing_holds_the_directory(self):
        """The steady state after a clean quit: Chrome removes the lock on its way out."""
        self.assertIsNone(chrome.singleton_holder(self.dir))

    def test_a_lock_naming_a_live_pid_on_this_host_is_held(self):
        self.lock(f"{socket.gethostname()}-{os.getpid()}")

        held = chrome.singleton_holder(self.dir)
        self.assertIsNotNone(held)
        self.assertIn(str(os.getpid()), held)

    def test_a_lock_naming_a_dead_pid_is_crash_residue_and_holds_nothing(self):
        """Chrome killed or crashed leaves the lock behind; the directory is still idle.

        Treating this as held would strand the seed permanently — nothing ever cleans
        the file up but the next Chrome to start.
        """
        self.lock(f"{socket.gethostname()}-{self.dead_pid()}")

        self.assertIsNone(chrome.singleton_holder(self.dir))

    def test_a_lock_from_another_host_is_held_and_quotes_both_names(self):
        """A synced home directory, or a machine that renamed itself.

        Both strings are in the message because those are the same situation from here,
        and only seeing them side by side tells the operator which one they are in.
        """
        self.lock("some-other-box.local-4321")

        held = chrome.singleton_holder(self.dir)
        self.assertIn("some-other-box.local", held)
        self.assertIn(socket.gethostname(), held)

    def test_a_lock_that_is_not_a_symlink_is_held(self):
        (self.dir / chrome.SINGLETON_LOCK).write_text("not a link")

        self.assertIsNotNone(chrome.singleton_holder(self.dir))

    def test_a_lock_whose_target_is_not_host_and_pid_is_held(self):
        self.lock("gibberish")

        self.assertIsNotNone(chrome.singleton_holder(self.dir))

    def test_a_hyphenated_hostname_is_split_after_the_pid_not_before_it(self):
        """`rpartition`, not `partition` — hostnames carry hyphens of their own.

        Splitting at the first hyphen reads `my-mac.local-999` as host `my` and pid
        `mac.local-999`, which parses as nothing and reports every such machine held.
        """
        self.lock("my-mac.local-999")

        held = chrome.singleton_holder(self.dir)
        self.assertIn("names host 'my-mac.local'", held)


if __name__ == "__main__":
    unittest.main()
