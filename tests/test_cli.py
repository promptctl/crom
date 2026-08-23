"""End-to-end tests for the CLI contract: what a script sees on stdout and in $?.

Chrome is never launched here — these cover everything up to the process, which is the
part apps integrate against. `chrome.scan` is stubbed because the process table is an
external system, not an implementation detail of crom.
"""

import json
import os
import shlex
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

    def test_a_refused_add_writes_nothing_and_leaves_the_project_usable(self):
        # A rejected profile that still reached the file would be rejected again on the
        # next load — and the parser rejects the file as a whole, so every command in
        # the project, `crom rm` included, would fail on the file the user needs crom to
        # repair. The refusal has to happen before the write, not after.
        self.crom("init")
        self.crom("add", "alpha", "--port", "9500")
        before = (self.project / ".crom.toml").read_text()

        self.crom("add", "beta", "--port", "9500", expect=4)

        self.assertEqual((self.project / ".crom.toml").read_text(), before)
        self.assertNotIn("beta", before)
        self.assertIn("alpha", self.crom("list"))

    def test_a_refused_duplicate_name_does_not_move_the_existing_profile(self):
        # Resolving reserves a port, so resolving before noticing the name is taken would
        # repoint the real `ci` at 9500 — while the config still declares no pin, and
        # whatever already points at its old port (a checked-in .mcp.json) breaks.
        self.crom("init")
        self.crom("add", "ci")
        before = self.crom("port", "ci")

        self.crom("add", "ci", "--port", "9500", expect=4)

        self.assertEqual(self.crom("port", "ci"), before)

    def test_a_pinned_port_already_taken_is_a_conflict_not_a_bare_failure(self):
        self.crom("init")
        self.crom("add", "alpha", "--port", "9500")
        self.crom("add", "beta", "--port", "9500", expect=4)

    def test_add_refuses_a_reserved_chrome_switch(self):
        self.crom("init")
        self.crom("add", "ci", "--flag", "--user-data-dir=/tmp/x", expect=1)

    def test_add_refuses_a_port_outside_the_legal_range(self):
        """click only proves `--port` is an int. An out-of-range value used to be written
        to the file and then rejected by the parser on the next load — bricking every
        command in the project, the exact failure `add` is otherwise careful to avoid."""
        self.crom("init")
        before = (self.project / ".crom.toml").read_text()

        for bad in ("0", "-5", "99999"):
            with self.subTest(port=bad):
                self.crom("add", "ci", "--port", bad, expect=1)

        self.assertEqual((self.project / ".crom.toml").read_text(), before)
        self.crom("list")  # the project is still usable

    def test_add_refuses_the_port_reserved_for_the_default_profile(self):
        self.crom("init")
        self.crom("add", "ci", "--port", "9222", expect=4)

    def test_a_failed_declaration_leaves_no_port_reserved(self):
        """Resolution persists a reservation before the declaration is written. A write
        that fails would strand it: no config declares the profile, so `crom rm` cannot
        reach it, and it can refuse a later profile that port."""
        self.crom("init")
        target = self.project / ".crom.toml"

        with mock.patch("crom.configwrite.add_profile", side_effect=OSError("disk full")):
            self.crom("add", "ci", expect=1)

        self.assertNotIn("ci", target.read_text())
        reserved = json.loads((state_home() / "registry.json").read_text())["ports"]
        self.assertNotIn("myproj/ci", reserved)

    def test_forget_refuses_the_reserved_user_namespace(self):
        """`crom forget user` would release the ports personal profiles are using; they
        would then silently come back on different numbers."""
        self.crom("list")  # bootstraps user/default and reserves its port
        before = self.crom("port", "user/default")

        self.crom("forget", "user", expect=4)

        self.assertEqual(self.crom("port", "user/default"), before)

    def test_list_all_survives_a_namespace_whose_config_is_gone(self):
        """`crom forget` is the documented cleanup for a stale namespace — but listing is
        how a user discovers there is one, so it must not be the command that dies."""
        other = self.root / "other"
        other.mkdir()
        self.crom("init", cwd=other)
        self.crom("add", "dev", cwd=other)
        (other / ".crom.toml").unlink()

        self.crom("init")
        self.crom("add", "mine")

        output = self.crom("list", "--all")

        self.assertIn("myproj/mine", output)   # the healthy profile is still listed
        self.assertIn("other", output)         # and the broken namespace is named
        self.assertIn("unavailable", output)

    def test_list_reports_an_unresolvable_profile_without_hiding_the_others(self):
        self.crom("init")
        self.crom("add", "good")
        config_path = self.project / ".crom.toml"
        config_path.write_text(
            config_path.read_text() + '\n[profiles.broken]\nflags = ["--x=${CROM_NOPE}"]\n'
        )

        output = self.crom("list")

        self.assertIn("myproj/good", output)
        self.assertIn("myproj/broken", output)
        self.assertIn("unresolved", output)

    def test_init_names_a_namespace_after_a_dotted_or_underscored_directory(self):
        """`_slug` stripped only `-`, so `.dotfiles` slugified unchanged and then failed
        name validation — a confusing error from a command that promises to work in any
        directory."""
        for raw, expected in ((".dotfiles", "dotfiles"), ("_internal", "internal"), ("...", "project")):
            with self.subTest(directory=raw):
                here = self.root / raw
                here.mkdir()
                self.crom("init", cwd=here)
                self.assertIn(f'namespace = "{expected}"', (here / ".crom.toml").read_text())

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

    def test_env_output_survives_the_eval_the_docs_prescribe(self):
        # README tells the user to run `eval "$(crom env dev)"`, so this output is shell
        # source. A profile directory under a path with a space — ordinary on macOS —
        # would end the assignment early and leave the rest to be read as a command.
        self.crom("init")
        path = self.project / ".crom.toml"
        path.write_text('state_dir = "./My Browsers"\n' + path.read_text())

        # shlex.split is the shell's own parse: `export K='/a b'` -> ['export', 'K=/a b'].
        exports = dict(
            shlex.split(line)[1].split("=", 1)
            for line in self.crom("env").strip().splitlines()
        )

        self.assertIn("My Browsers", exports["CROM_PROFILE_DIR"])
        self.assertTrue(Path(exports["CROM_PROFILE_DIR"]).is_absolute())

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
