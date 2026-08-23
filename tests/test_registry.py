"""Tests for the port ledger and the XDG paths it lives under.

The ledger is machine-wide shared state, so most of what matters here is what it refuses:
a second project claiming a namespace, a pin on the port `user/default` is promised, and
a file it cannot read being reported rather than crashing every command.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crom import paths, registry
from crom.model import Conflict, CromError, ProfileRef


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        self.env.start()
        self.addCleanup(self.env.stop)

    # --- namespace ownership --------------------------------------------------------

    def test_a_namespace_belongs_to_one_config_file(self):
        """Two projects picking the same namespace would share ports and profile dirs.

        That is the cross-project bleed namespaces exist to prevent — same ledger key,
        same on-disk profile, so one project's cookies in the other's browser.
        """
        registry.remember_namespace("app", Path("/one/.crom.toml"))
        with self.assertRaisesRegex(Conflict, "already claimed by /one/.crom.toml"):
            registry.remember_namespace("app", Path("/two/.crom.toml"))

    def test_remembering_the_same_config_again_is_not_a_conflict(self):
        registry.remember_namespace("app", Path("/one/.crom.toml"))
        registry.remember_namespace("app", Path("/one/.crom.toml"))
        self.assertEqual(registry.namespaces()["app"], Path("/one/.crom.toml"))

    # --- the reserved default port --------------------------------------------------

    def test_base_port_is_refused_to_a_pin_from_another_profile(self):
        """`_allocate` held 9222 back from auto-assignment but not from an explicit pin,
        after which a bare `crom` quietly landed on 9223."""
        with self.assertRaisesRegex(Conflict, "reserved for 'user/default'"):
            registry.port_for(ProfileRef("myapp", "ci"), pinned=9222, source=None)

    def test_user_default_may_still_pin_its_own_port(self):
        port = registry.port_for(ProfileRef("user", "default"), pinned=9222, source=None)
        self.assertEqual(port, 9222)

    # --- the reserved user namespace ------------------------------------------------

    def test_the_user_namespace_cannot_be_forgotten(self):
        """`crom forget user` would release the ports personal profiles are using, and
        they would silently come back on different numbers."""
        with self.assertRaisesRegex(Conflict, "reserved"):
            registry.forget_namespace("user")

    def test_an_ordinary_namespace_can_still_be_forgotten(self):
        registry.remember_namespace("app", Path("/one/.crom.toml"))
        registry.port_for(ProfileRef("app", "dev"), pinned=None, source=None)
        self.assertEqual(registry.forget_namespace("app"), 1)

    # --- an unreadable ledger -------------------------------------------------------

    def test_a_corrupt_ledger_is_reported_not_crashed_through(self):
        """Every command touches the ledger, so a raw JSONDecodeError took the whole CLI
        down outside its documented exit-code contract."""
        path = paths.registry_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ truncated")
        with self.assertRaisesRegex(CromError, "not valid JSON"):
            registry.reservations()

    def test_a_future_schema_is_a_plain_failure_not_a_conflict(self):
        """`Conflict` means two declarations claim one resource and maps to exit 4. A
        ledger written by a newer crom is neither, and reporting it as 4 misleads a
        script that branches on the code."""
        path = paths.registry_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 99, "ports": {}, "namespaces": {}}))
        with self.assertRaises(CromError) as caught:
            registry.reservations()
        self.assertNotIsInstance(caught.exception, Conflict)


class XdgTest(unittest.TestCase):
    """A relative XDG value must be ignored, per the spec and for crom's own sake.

    crom is run from many directories by design, so honoring a relative value would put
    the ledger, the user config, and every profile somewhere different depending on the
    cwd at invocation.
    """

    def test_a_relative_value_is_ignored_in_favour_of_the_default(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "relative/state"}):
            self.assertTrue(paths.state_home().is_absolute())
            self.assertNotIn("relative", str(paths.state_home()))

    def test_an_absolute_value_is_honoured(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/tmp/xdg-abs"}):
            self.assertEqual(paths.state_home(), Path("/tmp/xdg-abs/crom"))




class AdoptTest(unittest.TestCase):
    """`adopt` is the ledger's second write path, and must enforce the ledger's rules.

    It exists so migration can preserve a port the world already points at, which means
    the number it writes comes from a file the user can hand-edit. An invariant enforced
    on `port_for` alone is not enforced.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_adopting_the_base_port_for_anything_but_the_default_is_refused(self):
        """`port_for` refuses this; `adopt` used to let it through. A legacy registry
        naming 9222 for some other profile would then carry that violation through
        migration and keep it permanently, in a ledger that rejects it everywhere else —
        and a bare `crom` would quietly stop finding its profile on the port the README
        promises it without a lookup."""
        with self.assertRaisesRegex(Conflict, "reserved for 'user/default'"):
            registry.adopt(ProfileRef("myapp", "dev"), registry.BASE_PORT, None)

        self.assertEqual(registry.reservations(), {})

    def test_the_default_profile_may_still_adopt_the_base_port(self):
        """The rule is about who holds 9222, not about the number being untouchable."""
        registry.adopt(ProfileRef("user", "default"), registry.BASE_PORT, None)
        self.assertEqual(registry.reservations()["user/default"].port, registry.BASE_PORT)

    def test_adopting_a_port_another_profile_already_holds_is_refused(self):
        registry.adopt(ProfileRef("myapp", "dev"), 9301, None)
        with self.assertRaises(Conflict):
            registry.adopt(ProfileRef("other", "dev"), 9301, None)


if __name__ == "__main__":
    unittest.main()
