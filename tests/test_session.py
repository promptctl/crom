"""Readying crom the way a caller that is not the CLI would: one call, no click.

`crom.session` is the first of crom's mutating half to have a home outside `cli.py`, and
this file is the only place that reaches it as such a caller does — no `CliRunner`, no
click context, no command line. That is the whole point of the file. `test_cli` already
proves `crom list` works and would go on proving it if the sequence were inlined back
into `CromCommand.invoke` and reachable from nowhere else, which is the state this ticket
exists to leave behind.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crom import config, migrate
from crom.paths import user_config_file
from crom.session import Session

LEGACY = {"profiles": {"worker1": {"port": 4223}}}


def setUpModule():
    """Keep the session's tests about the session.

    A profile resolved here falls through to a live `find_chrome()`, which makes the
    assertions below depend on Chrome being installed on the machine running them and
    fail with "no Chrome executable found" on a CI runner that has none. `test_migrate`
    and `test_config` stub it for the same reason; the condition is theirs, not this
    file's subject.
    """
    global _chrome_stub
    _chrome_stub = mock.patch.object(config, "find_chrome", return_value=Path("/stub/chrome"))
    _chrome_stub.start()


def tearDownModule():
    _chrome_stub.stop()


class BeginTest(unittest.TestCase):
    """`Session.begin()` — the one call that readies crom and hands back a usable session."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        # `HOME` as well as the XDG variables, for the reason `test_migrate` states at
        # length: migration locates a pre-namespace installation through `Path.home()`,
        # which deliberately ignores XDG. `Session.begin` runs that migration, so this
        # file needs the guard the CLI's suite needs.
        self.env = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.root / "home"),
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_STATE_HOME": str(self.root / "state"),
            },
        )
        self.env.start()
        # Which directory the caller stands in *is* an input — `load_ambient` discovers
        # the governing config by walking up from the cwd — and it is ambient state no
        # argument carries. Left alone, these tests would walk up out of a checkout and
        # find the repository's own `.crom.toml`, asserting against whatever namespace
        # that declares. [LAW:no-ambient-temporal-coupling]
        self.previous = Path.cwd()
        os.chdir(self.root)
        # The process table and the port table are external systems, stubbed here for the
        # reasons `test_cli` gives: unstubbed, a developer running this suite with their
        # own Chrome up would see state this file never wrote.
        self.scan = mock.patch("crom.chrome.scan", return_value={})
        self.scan.start()
        self.ports = mock.patch("crom.chrome.port_is_free", return_value=True)
        self.ports.start()

    def tearDown(self):
        self.ports.stop()
        self.scan.stop()
        # Before the temporary directory goes: a cwd pointing at a removed directory
        # breaks every later test in the run.
        os.chdir(self.previous)
        self.env.stop()
        self.tmp.cleanup()

    def _legacy_installation(self) -> None:
        """A pre-namespace crom on this machine, which is the only thing migration acts on."""
        migrate.legacy_registry_file().parent.mkdir(parents=True)
        migrate.legacy_registry_file().write_text(json.dumps(LEGACY))
        for name in LEGACY["profiles"]:
            (migrate._legacy_state_dir() / name / "Default").mkdir(parents=True)

    def test_one_call_leaves_a_session_that_can_resolve_the_default_profile(self):
        """The ticket's claim, stated as the only thing a caller has to do.

        Resolving is the test rather than reading the file back, because "usable" is
        about what the session can answer, not about what got written on the way. The
        config's absence beforehand is the premise and not decoration: on a machine
        already holding a user config this would pass without `begin` having done
        anything at all. [LAW:verifiable-goals]
        """
        self.assertFalse(user_config_file().exists())

        session = Session.begin(log=lambda _: None)

        profile = session.profile("default")
        self.assertEqual((profile.ref.namespace, profile.ref.name), ("user", "default"))

    def test_a_repair_made_on_the_way_through_is_reported_to_the_caller_not_to_stderr(self):
        """The seam a non-CLI caller needs, and the reason `begin` takes a `log` at all.

        Readying converges what it finds unmet — here a user config that will not
        tokenize, which crom resets rather than leaving in the way of every command.
        [LAW:no-silent-failure] that may not happen quietly, and a caller embedding crom
        needs it somewhere other than the process's stderr. Both halves are asserted: the
        sentence reaches the log, and stderr stays empty — a `log` that is merely
        additional would leave the second half failing.

        The session still resolving afterwards is what separates a repair from a report:
        crom converges, so saying so is not the whole of the job.
        """
        user_config_file().parent.mkdir(parents=True)
        user_config_file().write_text("this file [[[ is not toml")
        said: list[str] = []

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            session = Session.begin(log=said.append)

        self.assertTrue(
            any("could not be read as TOML" in message for message in said),
            f"the repair was not reported through the given log: {said}",
        )
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(session.profile("default").ref.name, "default")

    def test_a_legacy_installation_is_migrated_by_the_same_call_and_through_the_same_log(self):
        """Readying is two writes, and this is the other one.

        Migration is what makes the order matter — it declares profiles into the very
        file the bootstrap writes into, and the session reads that file afterwards — so a
        `begin` that skipped it would hand back a session that cannot see the profiles
        this machine already had. Asserting the migrated profile resolves is asserting
        the whole sequence ran, in an order that left the read last.

        Through the same log for the same reason as the repair above: a caller gets one
        channel for everything the readying did, not one channel and one stderr.
        """
        self._legacy_installation()
        said: list[str] = []

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            session = Session.begin(log=said.append)

        self.assertTrue(
            any("migrating" in message for message in said),
            f"the migration was not reported through the given log: {said}",
        )
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(session.profile("worker1").ref.namespace, "user")
        # The bootstrap's own write survived the migration that ran ahead of it, which is
        # the one thing the two writes sharing a file could have cost.
        self.assertEqual(session.profile("default").ref.name, "default")
