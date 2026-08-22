"""End-to-end tests for the CLI contract: what a script sees on stdout and in $?.

Chrome is never launched here — these cover everything up to the process, which is the
part apps integrate against. `chrome.scan` is stubbed because the process table is an
external system, not an implementation detail of crom.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from crom import cli
from crom.paths import state_home, user_config_file


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.project = self.root / "myproj"
        self.project.mkdir()
        self.env = mock.patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_STATE_HOME": str(self.root / "state"),
            },
        )
        self.env.start()
        self.scan = mock.patch("crom.chrome.scan", return_value={})
        self.scan.start()
        self.runner = CliRunner()

    def tearDown(self):
        self.scan.stop()
        self.env.stop()
        self.tmp.cleanup()

    def crom(self, *args, cwd: Path | None = None, expect: int = 0):
        previous = Path.cwd()
        os.chdir(cwd or self.project)
        try:
            result = self.runner.invoke(cli.main, list(args))
        finally:
            os.chdir(previous)
        self.assertEqual(
            result.exit_code, expect, f"crom {' '.join(args)} -> {result.exit_code}\n{result.output}"
        )
        return result.output

    # --- bootstrap ------------------------------------------------------------------

    def test_first_run_declares_a_user_default_profile(self):
        self.crom("list")
        self.assertIn('[profiles.default]', user_config_file().read_text())
        self.assertIn('seed = "chrome"', user_config_file().read_text())

    # --- init and namespaces --------------------------------------------------------

    def test_init_writes_a_config_named_after_the_directory(self):
        self.crom("init")
        self.assertIn('namespace = "myproj"', (self.project / ".crom.toml").read_text())

    def test_init_refuses_to_clobber_an_existing_config(self):
        self.crom("init")
        self.crom("init", expect=4)

    def test_init_refuses_the_reserved_namespace(self):
        self.crom("init", "user", expect=4)

    def test_a_project_config_shadows_the_user_namespace(self):
        self.crom("init")
        output = self.crom("config", "--json")
        self.assertEqual(json.loads(output)["namespace"], "myproj")

    def test_outside_a_project_the_ambient_namespace_is_user(self):
        output = self.crom("config", "--json", cwd=self.root)
        self.assertEqual(json.loads(output)["namespace"], "user")

    # --- profiles -------------------------------------------------------------------

    def test_add_declares_a_profile_in_the_ambient_config(self):
        self.crom("init")
        self.crom("add", "ci", "--flag", "--headless=new")
        text = (self.project / ".crom.toml").read_text()
        self.assertIn("[profiles.ci]", text)
        self.assertIn("--headless=new", text)

    def test_add_refuses_a_duplicate_name(self):
        self.crom("init")
        self.crom("add", "ci")
        self.crom("add", "ci", expect=4)

    def test_add_refuses_a_reserved_chrome_switch(self):
        self.crom("init")
        self.crom("add", "ci", "--flag", "--user-data-dir=/tmp/x", expect=1)

    def test_up_on_an_undeclared_profile_exits_not_found(self):
        self.crom("init")
        self.crom("up", "ghost", expect=3)

    def test_an_unknown_namespace_exits_not_found(self):
        self.crom("init")
        self.crom("up", "ghost/dev", expect=3)

    # --- the machine-readable seam --------------------------------------------------

    def test_port_prints_only_the_port(self):
        self.crom("init")
        self.assertTrue(self.crom("port").strip().isdigit())

    def test_port_is_stable_across_invocations(self):
        self.crom("init")
        self.assertEqual(self.crom("port"), self.crom("port"))

    def test_env_emits_shell_exports(self):
        self.crom("init")
        exports = dict(
            line.removeprefix("export ").split("=", 1)
            for line in self.crom("env").strip().splitlines()
        )
        self.assertEqual(exports["CROM_PROFILE"], "myproj/default")
        self.assertEqual(exports["CROM_CDP_URL"], f"http://127.0.0.1:{exports['CROM_PORT']}")

    def test_config_json_reports_the_resolved_command_line(self):
        self.crom("init")
        payload = json.loads(self.crom("config", "default", "--json"))
        argv = payload["resolved"]["argv"]
        self.assertEqual(argv[-1], f"--remote-debugging-port={payload['resolved']['port']}")
        self.assertIn("--no-first-run", argv)

    def test_list_json_describes_every_addressable_profile(self):
        self.crom("init")
        self.crom("add", "ci")
        records = json.loads(self.crom("list", "--json"))
        refs = {record["ref"] for record in records}
        self.assertIn("myproj/default", refs)
        self.assertIn("myproj/ci", refs)
        self.assertIn("user/default", refs)  # still addressable from inside a project

    def test_mcp_wires_the_profile_port(self):
        self.crom("init")
        port = self.crom("port").strip()
        self.crom("mcp")
        entry = json.loads((self.project / ".mcp.json").read_text())
        self.assertIn(f"http://127.0.0.1:{port}", entry["mcpServers"]["chrome-devtools-mcp"]["args"])

    # --- collision avoidance, the whole point ---------------------------------------

    def test_two_projects_get_different_ports_and_directories(self):
        other = self.root / "otherproj"
        other.mkdir()
        self.crom("init")
        self.crom("init", cwd=other)

        mine = json.loads(self.crom("config", "default", "--json"))["resolved"]
        theirs = json.loads(self.crom("config", "default", "--json", cwd=other))["resolved"]
        self.assertNotEqual(mine["port"], theirs["port"])
        self.assertNotEqual(mine["profile_dir"], theirs["profile_dir"])

    def test_a_namespace_is_addressable_from_another_project(self):
        other = self.root / "otherproj"
        other.mkdir()
        self.crom("init", cwd=other)
        self.crom("port", cwd=other)  # registers the namespace by reading its config

        self.crom("init")
        self.assertEqual(self.crom("port", "otherproj/default").strip(), self.crom("port", cwd=other).strip())

    def test_forget_releases_a_namespaces_ports(self):
        other = self.root / "otherproj"
        other.mkdir()
        self.crom("init", cwd=other)
        self.crom("port", cwd=other)
        self.crom("init")
        self.crom("forget", "otherproj")
        self.crom("up", "otherproj/default", expect=3)

    # --- removal --------------------------------------------------------------------

    def test_rm_undeclares_the_profile_and_deletes_its_data(self):
        self.crom("init")
        self.crom("add", "ci")
        profile_dir = Path(json.loads(self.crom("config", "ci", "--json"))["resolved"]["profile_dir"])
        profile_dir.mkdir(parents=True)
        self.crom("rm", "ci", "--yes")
        self.assertNotIn("[profiles.ci]", (self.project / ".crom.toml").read_text())
        self.assertFalse(profile_dir.exists())

    def test_rm_keep_data_leaves_the_directory(self):
        self.crom("init")
        self.crom("add", "ci")
        profile_dir = Path(json.loads(self.crom("config", "ci", "--json"))["resolved"]["profile_dir"])
        profile_dir.mkdir(parents=True)
        self.crom("rm", "ci", "--keep-data")
        self.assertTrue(profile_dir.exists())

    def test_rm_refuses_while_the_profile_is_running(self):
        self.crom("init")
        self.crom("add", "ci")
        profile_dir = json.loads(self.crom("config", "ci", "--json"))["resolved"]["profile_dir"]
        with mock.patch("crom.chrome.scan", return_value={profile_dir: (999,)}):
            self.crom("rm", "ci", "--yes", expect=4)
        self.assertIn("[profiles.ci]", (self.project / ".crom.toml").read_text())

    # --- seeding --------------------------------------------------------------------

    def test_project_profiles_default_to_a_fresh_seed(self):
        self.crom("init")
        self.assertIn('seed = "fresh"', (self.project / ".crom.toml").read_text())

    def test_state_dir_can_be_relocated_into_the_project(self):
        self.crom("init")
        path = self.project / ".crom.toml"
        # Top-level keys must precede the first table, per TOML.
        path.write_text('state_dir = ".crom/profiles"\n' + path.read_text())
        profile_dir = json.loads(self.crom("config", "default", "--json"))["resolved"]["profile_dir"]
        self.assertTrue(profile_dir.startswith(str(self.project / ".crom" / "profiles")))
        self.assertFalse(profile_dir.startswith(str(state_home())))


if __name__ == "__main__":
    unittest.main()
