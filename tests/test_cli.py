"""End-to-end tests for the CLI contract: what a script sees on stdout and in $?.

Chrome is never launched here — these cover everything up to the process, which is the
part apps integrate against. `chrome.scan` is stubbed because the process table is an
external system, not an implementation detail of crom.
"""

import contextlib
import json
import os
import shlex
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from crom import cli, config, configwrite
from crom.config import load_ambient
from crom.model import CromError
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
                # HOME as well as the XDG variables, and not optional: `main` runs
                # `migrate.run_if_needed()` before every command, and migration locates
                # the legacy installation through `Path.home()` — the one lookup that
                # deliberately ignores XDG, because that is where the pre-namespace crom
                # actually wrote. Without this the suite would find a developer's real
                # `~/.config/crom/profiles.json` and migrate their actual profiles.
                "HOME": str(self.root / "home"),
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

    def test_the_suite_cannot_reach_the_real_home(self):
        """A guard rail rather than a feature test.

        `main` runs `migrate.run_if_needed()` before every command, and migration
        locates the legacy installation through `Path.home()` — the one lookup that
        deliberately ignores XDG, because that is where the pre-namespace crom actually
        wrote. A harness redirecting only the XDG variables would let this suite find a
        developer's real `~/.config/crom/profiles.json` and migrate their profiles
        mid-run. If this fails, stop and fix the harness before trusting any result.
        """
        from crom import migrate

        self.assertTrue(str(Path.home()).startswith(str(self.root)))
        self.assertTrue(str(migrate.legacy_registry_file()).startswith(str(self.root)))
        self.assertFalse(migrate.needed())

    def test_first_run_declares_a_user_default_profile(self):
        self.crom("list")
        self.assertIn('[profiles.default]', user_config_file().read_text())
        self.assertIn('seed = "default"', user_config_file().read_text())

    def test_a_corrupt_user_config_is_reset_rather_than_left_for_the_user(self):
        """A config crom cannot parse takes out every command, repairs included.

        `main` runs `_bootstrap_user_config()` before anything else, so the failure used
        to arrive from `crom config` and `crom list` too — the two commands someone
        reaches for to find out what is wrong. There is no command crom could have named
        as the fix, which is what makes resetting the file the only useful answer.
        """
        user_config_file().parent.mkdir(parents=True, exist_ok=True)
        user_config_file().write_text("not = = valid toml [[[")

        for command in (["list"], ["config"], ["port"]):
            with self.subTest(command=command):
                self.crom(*command)

        self.assertIn("[profiles.default]", user_config_file().read_text())

    def test_a_reset_config_keeps_the_file_it_replaced(self):
        """The broken file is the only record of what the user meant, so it is renamed
        rather than removed — and a second reset must not overwrite the first's copy."""
        user_config_file().parent.mkdir(parents=True, exist_ok=True)
        user_config_file().write_text("not = = valid toml [[[")
        self.crom("list")

        kept = user_config_file().with_name(user_config_file().name + ".broken")
        self.assertEqual(kept.read_text(), "not = = valid toml [[[")

        user_config_file().write_text("also = = broken")
        self.crom("list")

        self.assertEqual(kept.read_text(), "not = = valid toml [[[")
        self.assertEqual(
            user_config_file().with_name(user_config_file().name + ".broken-2").read_text(),
            "also = = broken",
        )

    def test_a_user_config_with_a_non_table_profiles_key_is_reported(self):
        """Valid TOML with a wrong value is not a reset case: crom can read the file, so
        it names the key instead of replacing everything around it."""
        user_config_file().parent.mkdir(parents=True, exist_ok=True)
        user_config_file().write_text('profiles = "typo"\n')

        output = self.crom("list", expect=1)

        self.assertIn("`profiles` must be a table", output)

    def test_a_broken_project_config_is_reset_keeping_its_namespace(self):
        """The namespace is what the project's ports and profile directories are keyed
        on, so a reset that renamed it would silently hand the project a fresh set of
        both. crom's own registry still remembers the name after the file stops saying it.
        """
        self.crom("init")
        port_before = self.crom("port").strip()
        (self.project / ".crom.toml").write_text("namespace = [[[ broken")

        # `assertIn`, not equality: the reset narrates itself on stderr, which the
        # runner folds into the same captured buffer as the answer on stdout.
        self.assertIn(port_before, self.crom("port"))
        self.assertIn('namespace = "myproj"', (self.project / ".crom.toml").read_text())

    def test_a_config_crom_can_still_read_is_reported_never_reset(self):
        """The reset fires only when the file will not tokenize, because that is the only
        state holding nothing crom can act on. Every other way a config is wrong keeps its
        precise diagnostic — resetting over one bad line would destroy the good
        declarations beside it to punish the bad one.
        """
        self.crom("init")
        self.crom("add", "dev", "--port", "9401")
        path = self.project / ".crom.toml"
        path.write_text(path.read_text() + "\n[profiles.ci]\nport = 9401\n")

        output = self.crom("list", expect=4)

        self.assertIn("both pin port 9401", output)
        self.assertIn("[profiles.dev]", path.read_text())

    def test_repairing_a_config_does_not_require_finding_chrome(self):
        """Whether a file tokenizes as TOML is a question about bytes. Running the repair
        through a full load would have made `find_chrome()` a precondition of every
        command — including `crom init`, which `_Session` exists to keep working on a
        machine that has no Chrome yet.
        """
        with mock.patch("crom.config.find_chrome", side_effect=CromError("no Chrome here")):
            self.crom("init", "myapp")

        self.assertIn('namespace = "myapp"', (self.project / ".crom.toml").read_text())

    def test_a_foreign_projects_config_is_never_reset_from_here(self):
        """`crom list --all` reaches every registered project's config through the
        registry. Repairing from there meant one listing could rewrite every `.crom.toml`
        on the machine, dropping declarations belonging to work the user is not doing.
        """
        other = self.root / "other"
        other.mkdir()
        self.crom("init", cwd=other)
        self.crom("add", "dev", cwd=other)
        self.crom("init")
        (other / ".crom.toml").write_text("not = = valid toml [[[")

        self.crom("list", "--all")

        self.assertEqual((other / ".crom.toml").read_text(), "not = = valid toml [[[")

    def test_a_namespace_whose_config_is_gone_keeps_its_ports(self):
        """An absent config file is not proof the project is gone — an unmounted volume
        looks identical, and released ports are irreversible: they get handed to other
        profiles and every checked-in `.mcp.json` pointing at the old number breaks. Only
        crom's record of where the project lives is dropped.
        """
        other = self.root / "other"
        other.mkdir()
        self.crom("init", cwd=other)
        port_before = self.crom("port", cwd=other).strip()
        self.crom("init")
        (other / ".crom.toml").rename(self.root / "stashed.toml")

        self.crom("up", "other/default", expect=3)

        (self.root / "stashed.toml").rename(other / ".crom.toml")
        self.assertEqual(self.crom("port", cwd=other).strip(), port_before)

    # --- init and namespaces --------------------------------------------------------

    def test_init_writes_a_config_named_after_the_directory(self):
        self.crom("init")
        self.assertIn('namespace = "myproj"', (self.project / ".crom.toml").read_text())

    def test_init_in_an_initialised_project_reports_it_and_succeeds(self):
        """`crom init` asks for a state, not a change. Refusing the second call made the
        user's own project a reason for a non-zero exit, and made every setup script that
        runs `crom init` unconditionally need a guard around it."""
        self.crom("init")
        before = (self.project / ".crom.toml").read_text()

        output = self.crom("init")

        self.assertIn("already configures this project", output)
        self.assertIn("myproj", output)
        self.assertEqual((self.project / ".crom.toml").read_text(), before)

    def test_init_reports_the_namespace_the_file_declares_not_the_one_it_would_guess(self):
        """The second `crom init` derives `myproj` from the directory name, which is a
        guess. Echoing that back as the project's namespace would tell someone whose
        project is called something else that it is called `myproj`."""
        self.crom("init", "chosen")

        output = self.crom("init")

        self.assertIn("(namespace 'chosen')", output)
        self.assertIn("chosen/default", output)

    def test_init_refuses_to_rename_a_project_that_already_has_a_namespace(self):
        """Converging reports a request that is already met; it must not report one that
        cannot be. Accepting this would exit 0 having done nothing, and the user would
        find out when `crom up other/default` said the profile was not declared."""
        self.crom("init", "chosen")

        output = self.crom("init", "other", expect=4)

        self.assertIn("declared chosen", output)
        self.assertIn("you asked for other", output)
        self.assertIn("chosen", (self.project / ".crom.toml").read_text())

    def test_init_refuses_to_restate_a_different_seed(self):
        self.crom("init", "--seed", "fresh")

        output = self.crom("init", "--seed", "default", expect=4)

        self.assertIn("[defaults].seed", output)
        self.assertIn('seed = "fresh"', (self.project / ".crom.toml").read_text())

    def test_init_over_a_config_that_declares_no_namespace_says_so(self):
        """A file that exists without configuring anything is not an initialised project.
        Converging on it would print "namespace 'None'" and point at a `crom up` that the
        parser is about to refuse."""
        (self.project / ".crom.toml").write_text("[defaults]\n")

        output = self.crom("init", expect=1)

        self.assertIn("declares no usable `namespace`", output)

    def test_init_refuses_the_reserved_namespace(self):
        self.crom("init", "user", expect=4)

    def test_init_refuses_the_reserved_namespace_guessed_from_the_directory(self):
        """The guess can still reach the file on the path that creates it, so it is still
        refused there. Falling back to another name instead would hand the project a
        namespace nobody chose."""
        here = self.root / "user"
        here.mkdir()

        self.crom("init", cwd=here, expect=4)

    def test_init_converges_in_a_directory_named_after_the_reserved_namespace(self):
        """The reserved name is refused for a namespace the command claims, not for one it
        is about to throw away. A project that chose `myproj` is named `myproj` whatever
        its directory is called, and the second `crom init` states no namespace at all —
        so crom's guess has nothing to contradict, and once had it exiting 4 over a value
        the user never typed and the file never held."""
        here = self.root / "user"
        here.mkdir()
        self.crom("init", "myproj", cwd=here)

        output = self.crom("init", cwd=here)

        self.assertIn("(namespace 'myproj')", output)
        self.assertIn('namespace = "myproj"', (here / ".crom.toml").read_text())

    def test_add_converges_in_a_directory_named_after_the_reserved_namespace(self):
        """The same directory, reached through the command that has to keep working in it
        after `crom init` did."""
        here = self.root / "user"
        here.mkdir()
        self.crom("init", "myproj", cwd=here)

        output = self.crom("add", "ci", cwd=here)

        self.assertIn("Declared myproj/ci", output)

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

    def test_adding_a_profile_that_already_exists_reports_it_and_succeeds(self):
        """`crom add ci` asks that `ci` exist. Twice is the same request, so the second
        call reports the profile — port and directory included, the same summary the first
        printed — rather than making a script that provisions profiles idempotently need a
        guard crom is perfectly able to apply itself."""
        self.crom("init")
        first = self.crom("add", "ci")
        before = (self.project / ".crom.toml").read_text()

        second = self.crom("add", "ci")

        self.assertIn("Already declared myproj/ci", second)
        self.assertIn(f"port {self.crom('port', 'ci').strip()}", second)
        self.assertIn("Declared myproj/ci", first)
        self.assertEqual((self.project / ".crom.toml").read_text(), before)

    def test_adding_a_profile_again_with_the_same_options_is_still_the_same_request(self):
        """Statedness is what decides, not silence: restating exactly what the config
        already says asks for nothing new."""
        self.crom("init")
        self.crom("add", "ci", "--seed", "fresh", "--port", "9500")

        self.assertIn(
            "Already declared", self.crom("add", "ci", "--seed", "fresh", "--port", "9500")
        )

    def test_adding_a_profile_again_with_a_different_seed_is_refused(self):
        """The other half of converging. Reporting success here would have crom claim it
        had given `ci` a fresh profile while the config still says otherwise, and the user
        would find out at launch — with their real Chrome logins in a browser they asked
        to be empty."""
        self.crom("init")
        self.crom("add", "ci")

        output = self.crom("add", "ci", "--seed", "fresh", expect=4)

        self.assertIn("declared default", output)
        self.assertIn("you asked for fresh", output)
        self.assertNotIn('seed = "fresh"', (self.project / ".crom.toml").read_text())

    def test_pinning_the_port_a_profile_was_merely_assigned_is_refused(self):
        """The exception to comparing effective values. A seed or flag inherited from
        `[defaults]` reaches the profile on every checkout of the file, so inheriting it
        satisfies a request for it. An assigned port lives in a machine-local ledger and
        nowhere in the config, so asking to pin the number crom happens to have handed out
        is asking for something the file does not yet promise — and reporting that as
        already-done would leave a fresh clone free to land somewhere else."""
        self.crom("init")
        self.crom("add", "ci")
        assigned = self.crom("port", "ci").strip()

        output = self.crom("add", "ci", "--port", assigned, expect=4)

        self.assertIn(f"unpinned — crom assigned {assigned}", output)
        # Read back through the parser rather than grepped: the template's own comments
        # mention `port = 9401`, so a substring test would answer about the prose.
        self.assertIsNone(load_ambient(self.project).profiles["ci"].port)

    def test_a_seed_reached_through_defaults_satisfies_a_request_for_it(self):
        """`[defaults].seed` is committed alongside the profile, so a profile that
        inherits `fresh` *is* the profile `--seed fresh` asked for. Comparing the key the
        file happens to spell it in rather than the value the profile resolves to would
        refuse a request the project already meets."""
        self.crom("init", "--seed", "fresh")
        self.crom("add", "ci")

        self.assertIn("Already declared", self.crom("add", "ci", "--seed", "fresh"))

    def test_adding_a_profile_again_with_a_flag_it_already_inherits_is_the_same_request(self):
        """Flags are judged on effective values, exactly as the seed is.

        A flag reaching the profile from `[defaults]` reaches it on every machine that
        checks the file out, so a profile already running `--headless` *is* the profile
        `--flag --headless` asked for. Concatenating the stated flag onto the defaults
        compared `--headless` against `--headless --headless` and exited 4 — refusing over
        a difference the comparison had invented, and reporting the doubled list back as
        what the user had asked for.
        """
        self.crom("init")
        config_path = self.project / ".crom.toml"
        config_path.write_text(config_path.read_text().replace("flags = []", 'flags = ["--headless"]', 1))
        self.crom("add", "ci")

        output = self.crom("add", "ci", "--flag", "--headless")

        self.assertIn("Already declared myproj/ci", output)

    def test_restating_the_flags_of_a_profile_that_drops_one_is_the_same_request(self):
        """The request cannot express `drop_flags`, so it is silent about drops rather
        than asserting there are none. Resolved beside the declaration instead of on top
        of it, the two sides were composed under different drop policies: a restatement
        identical to the file exited 4 reporting a `[defaults]` flag the profile drops and
        the user never typed — a difference the comparison invented, in a value the user
        never spoke."""
        self.crom("init")
        (self.project / ".crom.toml").write_text(
            'namespace = "myproj"\n'
            '[defaults]\nflags = ["--b=2"]\n'
            '[profiles.ci]\nflags = ["--a=1"]\ndrop_flags = ["--b"]\n'
        )

        output = self.crom("add", "ci", "--flag", "--a=1")

        self.assertIn("Already declared myproj/ci", output)

    def test_asking_for_a_switch_the_profile_drops_is_still_refused(self):
        """The fix must not launder a real disagreement into convergence: the file says
        remove this switch and the command says set it, which only the author can settle.
        Refused with what each side actually says, rather than a fabricated value."""
        self.crom("init")
        (self.project / ".crom.toml").write_text(
            'namespace = "myproj"\n'
            '[defaults]\nflags = ["--b=2"]\n'
            '[profiles.ci]\nflags = ["--a=1"]\ndrop_flags = ["--b"]\n'
        )

        output = self.crom("add", "ci", "--flag", "--b=9", expect=4)

        self.assertIn("--b=9", output)

    def test_adding_a_profile_again_with_its_flags_reordered_is_the_same_request(self):
        """Order is not a fact about the profile, so restating the same flags in another
        order is not a change to it."""
        self.crom("init")
        self.crom("add", "ci", "--flag", "--headless", "--flag", "--no-sandbox")

        output = self.crom("add", "ci", "--flag", "--no-sandbox", "--flag", "--headless")

        self.assertIn("Already declared myproj/ci", output)

    def test_asking_for_a_launch_policy_flag_the_config_does_not_state_is_refused(self):
        """The launch policy is a layer at launch but not a fact about this config.

        What makes an inherited flag *already* what the user asked for is that it lives in
        the file every checkout shares. crom's policy does not — it is crom's own
        behavior, which an upgrade can change — so asking the file to state `--no-pings`
        is asking for something it does not yet say, exactly as `--port` is judged on the
        pin rather than on the port crom happened to assign.
        """
        self.crom("init")
        self.crom("add", "ci")

        output = self.crom("add", "ci", "--flag", "--no-pings", expect=4)

        self.assertIn("--no-pings", output)

    def test_a_refusal_names_the_profiles_whole_effective_flag_list(self):
        """Both sides are full values, in the vocabulary `_reject_restatement` promises.

        Reporting only what the two sides differ on read `declared (unset)` for a profile
        that had flags — the wording that means "nothing is set" for seed and port —
        denying a declaration sitting in the file the message names.
        """
        self.crom("init")
        self.crom("add", "ci", "--flag", "--a=1")

        output = self.crom("add", "ci", "--flag", "--a=1", "--flag", "--b=2", expect=4)

        self.assertIn("declared --a=1,", output)
        self.assertNotIn("(unset)", output)

    def test_dropping_a_declared_flag_is_refused_without_trailing_off(self):
        """The asked side has no `(unset)` fallback, so it must never render empty."""
        self.crom("init")
        self.crom("add", "ci", "--flag", "--a=1", "--flag", "--b=1")

        output = self.crom("add", "ci", "--flag", "--a=1", expect=4)

        self.assertIn("you asked for --a=1", output)

    def test_restating_one_switch_with_a_different_value_is_refused(self):
        """The override case the old set-of-strings comparison could not see: both sides
        name `--disable-blink-features`, so a set union found nothing in common to
        compare."""
        self.crom("init")
        self.crom("add", "ci", "--flag", "--disable-blink-features=A")

        output = self.crom("add", "ci", "--flag", "--disable-blink-features=B", expect=4)

        self.assertIn("flags", output)
        self.assertIn("--disable-blink-features=B", output)

    def test_adding_a_profile_that_states_one_switch_twice_is_refused(self):
        self.crom("init")

        output = self.crom(
            "add",
            "ci",
            "--flag",
            "--disable-blink-features=A",
            "--flag",
            "--disable-blink-features=B",
            expect=1,
        )

        self.assertIn("--disable-blink-features=A,B", output)

    def test_adding_a_profile_again_with_different_flags_is_refused(self):
        self.crom("init")
        self.crom("add", "ci")

        output = self.crom("add", "ci", "--flag", "--headless", expect=4)

        self.assertIn("flags", output)
        self.assertIn("--headless", output)
        self.assertNotIn("--headless", (self.project / ".crom.toml").read_text())

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

        # Scoped to the profile under test: `_bootstrap_user_config` reaches
        # `ensure_profile` before every command, so an unconditional stub fails the
        # bootstrap instead of the declaration this is about.
        real_ensure = configwrite.ensure_profile

        def fail_for_ci(path, spec, **kwargs):
            if spec.name == "ci":
                raise OSError("disk full")
            return real_ensure(path, spec, **kwargs)

        with mock.patch("crom.configwrite.ensure_profile", fail_for_ci):
            self.crom("add", "ci", expect=1)

        self.assertNotIn("ci", target.read_text())
        reserved = json.loads((state_home() / "registry.json").read_text())["ports"]
        self.assertNotIn("myproj/ci", reserved)

    def test_losing_a_concurrent_add_converges_and_leaves_the_winners_port_alone(self):
        """`profile.ref` is the profile's shared identity, not one attempt's.

        Two `crom add ci` calls resolve the same ref and reserve the same port. The loser
        read the config before the winner wrote to it, so it believes the name is free and
        goes all the way to the write — where the declaration it finds is the *winner's*,
        and owns that reservation. Releasing it there silently moved a live profile to a
        new port on its next resolve; refusing with exit 4 told the user their profile
        could not be created when it plainly exists. Converging does neither.

        The race is reproduced deterministically by staling the *discovery* read — the
        scope is parsed without `ci`, exactly the picture a process that read the file a
        moment too early would hold — over a file that genuinely declares it.
        """
        self.crom("init")
        self.crom("add", "ci")
        winners_port = self.crom("port", "ci")

        with self.losing_the_race_for("ci"):
            output = self.crom("add", "ci")

        self.assertIn("Already declared myproj/ci", output)
        self.assertEqual(self.crom("port", "ci"), winners_port)

    @contextlib.contextmanager
    def losing_the_race_for(self, name: str):
        """One `crom add`'s view of a config another `crom add` is writing to.

        Only the *first* read that would have seen `name` is staled. A loser reads the
        file at discovery, a moment before the winner writes, and reads the true file
        every time after — so staling every read would model a process that can never see
        the file at all, which is not this race. It would also pin `add` to reading the
        config exactly once, which is plumbing, not the contract.
        [LAW:behavior-not-structure]
        """
        real_parse = config.parse
        before_the_winner_wrote = [True]

        def parse(text, source, **kwargs):
            scope = real_parse(text, source, **kwargs)
            if name in scope.profiles and before_the_winner_wrote[0]:
                before_the_winner_wrote[0] = False
                return replace(
                    scope, profiles={n: s for n, s in scope.profiles.items() if n != name}
                )
            return scope

        with mock.patch("crom.config.parse", parse):
            yield

    def test_losing_a_concurrent_add_is_judged_against_the_winners_declaration(self):
        """The loser states a fact; the winner's declaration is what it must be judged on.

        `add` proposes its own spec for a name its scope does not show, which on this path
        is the loser's own request — so comparing that proposal against itself could never
        refuse anything, and the report stated it as the project's fact. `crom add ci
        --seed fresh` exited 0 announcing `seed fresh` over a file that gives `ci` the
        user's real Chrome profile: the find-out-at-launch failure
        `test_adding_a_profile_again_with_a_different_seed_is_refused` exists to prevent,
        reached by the one path that skipped the check.
        """
        self.crom("init")
        self.crom("add", "ci")  # winner states no seed, so `ci` inherits `default`

        with self.losing_the_race_for("ci"):
            output = self.crom("add", "ci", "--seed", "fresh", expect=4)

        self.assertIn("seed", output)
        self.assertIn("fresh", output)
        # The winner's declaration is untouched: no `seed` key, still inheriting.
        target = self.project / ".crom.toml"
        self.assertIsNone(config.parse(target.read_text(), target).profiles["ci"].seed)

    def test_losing_a_concurrent_add_reports_the_winners_values_not_its_own(self):
        """Converging still converges — on the file's facts. The loser asks for nothing
        the winner's declaration does not already satisfy, so this is a met request; every
        fact printed has to come from the file rather than from the loser's proposal."""
        self.crom("init")
        self.crom("add", "ci", "--seed", "fresh")
        winners_port = self.crom("port", "ci").strip()

        with self.losing_the_race_for("ci"):
            output = self.crom("add", "ci")

        self.assertIn("Already declared myproj/ci", output)
        self.assertIn("seed fresh", output)
        self.assertIn(f"port {winners_port}", output)

    def test_add_recreates_a_project_config_that_vanished_after_discovery(self):
        """`_declare` creates a missing file from a header that carries no `namespace`
        key, so recreating a deleted project config that way yields a file the parser
        rejects wholesale. `crom add` used to refuse and name `crom init` — a command it
        is holding every argument for, since the scope it needs is still in hand.

        The window is within one invocation: the scope is read at discovery and the file
        removed before the write (a `git clean`, another agent resetting the workspace).
        A fresh `crom` would simply re-discover and fall back to the user scope, so the
        scope is stubbed to hold a source that no longer exists — which is exactly the
        state `_Session.scope` would be caching.
        """
        self.crom("init")
        scope = load_ambient(self.project)
        (self.project / ".crom.toml").unlink()

        with mock.patch("crom.cli.load_ambient", return_value=scope):
            self.crom("add", "two")

        recreated = (self.project / ".crom.toml").read_text()
        self.assertIn('namespace = "myproj"', recreated)
        self.assertIn("[profiles.two]", recreated)

    def test_init_shortens_a_directory_name_too_long_to_be_a_namespace(self):
        long_name = "a" * 200
        here = self.root / long_name
        here.mkdir()
        self.crom("init", cwd=here)
        self.assertIn(f'namespace = "{"a" * 64}"', (here / ".crom.toml").read_text())

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

    def test_referring_to_an_undeclared_profile_declares_it(self):
        """"Run `crom add ghost` first" was crom making the user the courier for a step
        it holds every argument for. The declaration written is the bare one `crom add`
        writes — no seed key, so the project's `[defaults]` still governs it."""
        self.crom("init")

        self.crom("port", "ghost")

        config_text = (self.project / ".crom.toml").read_text()
        self.assertIn("[profiles.ghost]", config_text)
        self.assertNotIn("seed", config_text.split("[profiles.ghost]")[1])

    def test_a_bare_up_declares_default_in_a_namespace_that_has_no_profiles(self):
        """The state a `crom rm` of a project's last profile leaves behind. Every command
        that takes a ref defaults to `default`, so a namespace without one is a namespace
        in which crom's own documented default does not resolve."""
        self.crom("init")
        self.crom("rm", "default", "--yes")

        with mock.patch("crom.chrome.launch", return_value=(4321,)):
            with mock.patch("crom.seed.materialize_under_lock"):
                output = self.crom("up")

        self.assertIn("myproj/default", output)
        self.assertIn("[profiles.default]", (self.project / ".crom.toml").read_text())

    def test_removing_an_undeclared_profile_does_not_declare_it_first(self):
        """`rm` converges a profile toward not existing, so creating one on the way would
        be crom bringing into being the thing it was asked to take away."""
        self.crom("init")

        self.crom("rm", "ghost", "--yes", expect=3)

        self.assertNotIn("[profiles.ghost]", (self.project / ".crom.toml").read_text())

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
        self.assertEqual(exports["CROM_CDP_URL"], f"http://127.0.0.1:{exports['CROM_PORT']}")

    def test_env_gives_each_name_the_meaning_it_has_in_a_config(self):
        """`CROM_PROFILE` named two different things depending on where it was read: the
        full "namespace/name" here, and the bare name inside a config's
        `${CROM_PROFILE}`. The README presents both as one vocabulary, so a user moving
        a value between them silently got something else. One name, one meaning — and
        the joined form keeps a name that means only that."""
        self.crom("init")
        exports = dict(
            line.removeprefix("export ").split("=", 1)
            for line in self.crom("env").strip().splitlines()
        )
        self.assertEqual(exports["CROM_NAMESPACE"], "myproj")
        self.assertEqual(exports["CROM_PROFILE"], "default")
        self.assertEqual(exports["CROM_REF"], "myproj/default")

    def test_the_two_vocabularies_agree_on_every_name_they_share(self):
        """The guarantee is the agreement itself, not either spelling on its own."""
        from crom import resolve as resolver

        self.crom("init")
        exports = dict(
            line.removeprefix("export ").split("=", 1)
            for line in self.crom("env").strip().splitlines()
        )
        # Restore the *original* working directory, not self.root — tearDown deletes
        # that, and a cwd pointing at a removed directory breaks every later test.
        previous = Path.cwd()
        os.chdir(self.project)
        try:
            profile = resolver.resolve(cli.parse_ref("default", "myproj"), load_ambient())
        finally:
            os.chdir(previous)
        interpolation = resolver._variables(
            profile.ref, profile.profile_dir, profile.config_dir, profile.port
        )
        for name in set(exports) & set(interpolation):
            with self.subTest(variable=name):
                self.assertEqual(exports[name], interpolation[name])

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

    def test_config_names_a_dropped_switch_rather_than_leaving_it_missing(self):
        """A dropped switch is absent from argv and indistinguishable there from one
        nobody ever set. `crom config` is where a reader goes to find out what crom is
        doing, so the removal has to be something the listing shows."""
        self.crom("init")
        # The template ends inside `[profiles.default]`, so this lands in that stanza.
        path = self.project / ".crom.toml"
        path.write_text(path.read_text() + 'drop_flags = ["--disable-sync"]\n')

        human = self.crom("config", "default")
        payload = json.loads(self.crom("config", "default", "--json"))

        self.assertNotIn("--disable-sync", payload["resolved"]["argv"])
        self.assertEqual(payload["resolved"]["dropped"], ["--disable-sync"])
        self.assertIn("(dropped --disable-sync)", human)

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

    def test_rm_stops_a_running_profile_rather_than_refusing(self):
        """`rm` owns the stop; it does not send the user away to run `crom down` first.

        The old contract exported a state transition `rm` needs — and can reach itself,
        under the lock it already takes — as a two-command ritual the caller had to
        perform. [LAW:no-ambient-temporal-coupling] The stop is reported, because `--yes`
        skips the prompt that would otherwise be its only mention.
        """
        self.crom("init")
        self.crom("add", "ci")
        profile_dir = json.loads(self.crom("config", "ci", "--json"))["resolved"]["profile_dir"]

        with (
            mock.patch("crom.chrome.scan", return_value={profile_dir: (999,)}),
            mock.patch("crom.cli.chrome.kill", return_value=(999,)),
        ):
            output = self.crom("rm", "ci", "--yes")

        self.assertIn("stopped pid 999", output)
        self.assertNotIn("[profiles.ci]", (self.project / ".crom.toml").read_text())

    def test_down_stops_the_browser_under_the_profile_lock(self):
        """`up` and `rm` hold `profile_lock` across their check-then-act; `down` did not.

        A launched Chrome is visible to `scan` as soon as `Popen` returns, well before
        CDP answers — and `up` is still inside its locked section then, possibly having
        just materialised the profile from a `chrome` seed. An unlocked `down` could kill
        a browser mid-first-run against a freshly-copied user-data-dir, and leave `up`
        reporting a readiness timeout for a process someone else terminated.

        A lock one participant ignores serialises nothing, so the claim under test is
        that the kill happens *inside* the critical section, not merely that it happens.
        """
        self.crom("init")
        self.crom("add", "ci")

        events: list[str] = []
        real_lock = cli.seed.profile_lock

        @contextlib.contextmanager
        def tracking_lock(profile):
            events.append("lock")
            with real_lock(profile):
                yield
            events.append("unlock")

        def kill(_profile):
            events.append("kill")
            return ()

        with mock.patch("crom.cli.seed.profile_lock", tracking_lock):
            with mock.patch("crom.cli.chrome.kill", kill):
                self.crom("down", "ci")

        self.assertEqual(events, ["lock", "kill", "unlock"])

    def test_rm_stops_and_deletes_inside_one_critical_section(self):
        """A `crom up` racing this command must land wholly before it or wholly after.

        `up_cmd` holds `profile_lock` across its own seed-check-and-launch, so the two
        commands interleave only if `rm` performs any of its destruction outside the
        lock. Deleting a user-data-dir out from under a browser that is mid-first-run
        leaves a process crom can no longer find or stop, writing into a directory that
        no longer exists.

        The liveness read that composes the confirmation prompt is deliberately *not*
        part of that guarantee: the prompt is held open for a human, so that read is
        stale by construction. Which is why the kill inside the lock is unconditional
        rather than guarded by it — a browser started while the question was on screen
        must not survive the answer. The claim under test is therefore ordering, not
        merely that the steps happen.
        """
        self.crom("init")
        self.crom("add", "ci")
        profile_dir = Path(json.loads(self.crom("config", "ci", "--json"))["resolved"]["profile_dir"])
        profile_dir.mkdir(parents=True)

        events: list[str] = []
        real_lock = cli.seed.profile_lock

        @contextlib.contextmanager
        def tracking_lock(profile):
            events.append("lock")
            with real_lock(profile):
                yield
            events.append("unlock")

        with (
            mock.patch("crom.cli.seed.profile_lock", tracking_lock),
            mock.patch("crom.cli.chrome.kill", lambda _p: events.append("kill") or ()),
            mock.patch("crom.cli.shutil.rmtree", lambda _p: events.append("rmtree")),
        ):
            self.crom("rm", "ci", "--yes")

        self.assertEqual(events, ["lock", "kill", "rmtree", "unlock"])

    def test_the_size_prompt_counts_only_what_the_delete_reclaims(self):
        """`_human_size` measures what removing the directory frees, so a symlink's
        target — which is not deleted — must not be counted, and a dangling link must
        not raise. A raw OSError from a mid-walk `stat` is not a CromError either, so it
        would escape the exit-code contract as a traceback from a helper whose only job
        is to be informative before a destructive act."""
        directory = self.root / "profile"
        (directory / "sub").mkdir(parents=True)
        (directory / "sub" / "real.bin").write_bytes(b"x" * 2048)

        outside = self.root / "huge.bin"
        outside.write_bytes(b"y" * 100_000)
        (directory / "link-to-huge").symlink_to(outside)
        (directory / "dangling").symlink_to(self.root / "gone")

        size = cli._human_size(directory)
        self.assertEqual(size, "2KB")  # 2048 bytes, and neither link counted

    def test_a_failed_delete_leaves_the_profile_declared_and_retryable(self):
        """`rm` deletes data *before* it undeclares, so a failure is recoverable.

        `rm` stops the browser itself now, and a Chrome helper can outlive
        `chrome.kill` — so `rmtree` can raise on a directory still being written. When
        the delete ran last, that failure left a half-removed directory belonging to a
        profile no command could name: `rm` resolves by name first, so the retry it
        suggests was impossible. It also escaped as a traceback, since a bare `OSError`
        is not a `CromError`.
        """
        self.crom("init")
        self.crom("add", "ci")
        before = json.loads(self.crom("config", "ci", "--json"))["resolved"]
        Path(before["profile_dir"]).mkdir(parents=True)

        with mock.patch("crom.cli.shutil.rmtree", side_effect=OSError(66, "Directory not empty")):
            output = self.crom("rm", "ci", "--yes", expect=1)

        self.assertIn("still declared", output)
        self.assertIn("[profiles.ci]", (self.project / ".crom.toml").read_text())
        # Still nameable, on its original port — which is what makes the retry real.
        self.assertEqual(json.loads(self.crom("config", "ci", "--json"))["resolved"], before)

    def test_help_sections_cover_every_command(self):
        """Every command appears in exactly one curated `crom --help` section.

        `format_commands` renders anything ungrouped under an "Other" heading so a new
        command can never silently vanish from the only place users look for it. That
        fallback is the safety net; this is the plan, and a failure here means a command
        was added without deciding which of crom's three jobs it belongs to.
        """
        listed = [name for _, names in cli._COMMAND_SECTIONS for name in names]
        self.assertEqual(sorted(listed), sorted(set(listed)), "a command is listed twice")
        self.assertEqual(set(cli.main.commands) - set(listed), set(), "ungrouped command(s)")
        self.assertEqual(set(listed) - set(cli.main.commands), set(), "section names a dead command")

    # --- seeding --------------------------------------------------------------------

    def test_a_project_profile_starts_from_the_same_seed_as_a_personal_one(self):
        """The regression a user actually hit: `crom init` then `crom up` opened a Chrome
        with none of their extensions or logins, while a bare `crom up` anywhere else
        opened one that had them — because the project template wrote `fresh` and
        `_bootstrap_user_config` wrote `SeedChrome()`. Asserting the two agree is what
        keeps `default` meaning one thing. [LAW:one-source-of-truth]
        """
        self.crom("init")
        project = json.loads(self.crom("config", "default", "--json"))["resolved"]["seed"]
        personal = json.loads(self.crom("config", "user/default", "--json"))["resolved"]["seed"]
        self.assertEqual(project, "default")
        self.assertEqual(project, personal)

    def test_an_added_profile_inherits_the_projects_seed(self):
        """`crom add` with no `--seed` writes no `seed` key, so `[defaults].seed`
        governs. The old `--seed` default of `"fresh"` stamped an explicit seed nobody
        asked for into every added profile, which left `[defaults].seed` applying to the
        profile `crom init` wrote and to no profile added after it."""
        self.crom("init", "--seed", "chrome:Work")
        self.crom("add", "ci")

        self.assertNotIn("seed", (self.project / ".crom.toml").read_text().split("[profiles.ci]")[1])
        resolved = json.loads(self.crom("config", "ci", "--json"))["resolved"]
        self.assertEqual(resolved["seed"], "chrome:Work")

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
