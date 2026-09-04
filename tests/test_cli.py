"""End-to-end tests for the CLI contract: what a script sees on stdout and in $?.

Chrome is never launched here — these cover everything up to the process, which is the
part apps integrate against. `chrome.scan` is stubbed because the process table is an
external system, not an implementation detail of crom.
"""

import contextlib
import errno
import json
import os
import re
import shlex
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from crom import cli, config, configwrite, mcp
from crom.config import load_ambient
from crom.model import CromError, ProfileRef
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

    def tearDown(self):
        self.scan.stop()
        self.env.stop()
        self.tmp.cleanup()

    def invoke(self, *args, cwd: Path | None = None, expect: int = 0):
        """Run one command in the project directory, and hand back the whole result.

        The only way this suite runs a command, deliberately: which directory crom is
        standing in *is* an input — `load_ambient` discovers the governing config by
        walking up from the cwd — and it is ambient state no argument carries.
        [LAW:no-ambient-temporal-coupling] A test that reached past this and called a
        runner directly ran in whatever directory pytest was launched from, found no
        project config there, and quietly asserted against the bootstrapped `user` scope
        instead of the project it had just written. It passed, because the claims it made
        are true of `user/default` too.

        So the `CliRunner` is built here rather than kept as an attribute: with no runner
        to reach for, that mistake is unrepresentable rather than discouraged.
        [LAW:types-are-the-program] A runner carries no state between invocations, so
        building one per call costs nothing.
        """
        previous = Path.cwd()
        os.chdir(cwd or self.project)
        try:
            # `catch_exceptions=False` because the default one lies about this boundary:
            # it turns an escaping exception into a tidy `exit_code == 1` with nothing in
            # `output`, which is precisely how a real process behaves *only after*
            # `CromGroup.invoke` has caught it. Under the default, every `expect=1` in this
            # file passes just as happily against a command that printed a stack trace.
            # [FRAMING:representation] the runner is a map of the terminal; this keeps a
            # crash looking like a crash.
            result = CliRunner().invoke(cli.main, list(args), catch_exceptions=False)
        finally:
            os.chdir(previous)
        self.assertEqual(
            result.exit_code, expect, f"crom {' '.join(args)} -> {result.exit_code}\n{result.output}"
        )
        return result

    def crom(self, *args, cwd: Path | None = None, expect: int = 0):
        """What a script sees: stdout and stderr as one stream, which is what a terminal
        shows. `invoke` is for the few tests that need the two told apart."""
        return self.invoke(*args, cwd=cwd, expect=expect).output

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

    def _override_project(self) -> None:
        """A config whose `[defaults]` and profile answer the same two questions."""
        self.crom("init")
        (self.project / ".crom.toml").write_text(
            'namespace = "myproj"\n\n'
            "[defaults]\n"
            'flags = ["--window-size=800,600"]\n'
            "features = { PictureInPicture = false }\n\n"
            "[profiles.default]\n"
            'flags = ["--window-size=1280,800"]\n'
            "features = { PictureInPicture = true }\n"
        )

    def test_config_attributes_an_overridden_flag_and_names_what_it_replaced(self):
        """A flag a user wrote can legitimately not be in argv because a later layer
        replaced it. The listing that prints argv is where that has to be readable —
        otherwise the only way to find out is to know the layering rule already."""
        self._override_project()
        human = self.crom("config", "default")

        self.assertIn(
            "--window-size=1280,800",
            human,
        )
        self.assertIn(
            "from [profiles.default], over --window-size=800,600 from [defaults]", human
        )
        self.assertNotIn("--window-size=800,600 ", human.replace("over --window-size=800,600", ""))

    def test_config_attributes_a_feature_per_name_rather_than_per_switch(self):
        """The two feature switches are a union of every layer's table, so 'the profile
        overrode the policy's --disable-features' is a false sentence. The provenance for
        them is per feature name, and the output has to say it that way."""
        self._override_project()
        human = self.crom("config", "default")

        # crom's own policy feature and the project's, in one switch, each attributed to
        # the layer that decided it — and the project's own flip shown over `[defaults]`.
        self.assertIn("ChromeWhatsNewUI from crom's launch policy", human)
        self.assertIn("PictureInPicture from [profiles.default], over false from [defaults]", human)

    def test_config_json_carries_each_flags_layer_beside_the_command(self):
        self._override_project()
        payload = json.loads(self.crom("config", "default", "--json"))
        by_flag = {entry["flag"]: entry["why"] for entry in payload["resolved"]["flags"]}

        self.assertEqual(
            by_flag["--window-size=1280,800"],
            [
                {
                    "question": "--window-size",
                    "stands": {"layer": "[profiles.default]", "said": "--window-size=1280,800"},
                    "over": [{"layer": "[defaults]", "said": "--window-size=800,600"}],
                }
            ],
        )
        # Every flag in the report is a flag on the command line, and vice versa for the
        # ones crom composes — the two views describe one list.
        self.assertLessEqual(set(by_flag), set(payload["resolved"]["argv"]))

    def test_config_of_a_config_that_overrides_nothing_stays_a_clean_exit_on_stdout(self):
        """Attribution is information, not a warning: nothing here may change the exit
        status, move output to stderr, or prompt."""
        self.crom("init")
        result = self.invoke("config", "default")

        self.assertIn("Here, crom is in the 'myproj' namespace.", result.stdout)
        self.assertEqual(result.stderr, "")
        self.assertIn("from crom's launch policy", result.stdout)
        self.assertNotIn("over ", result.stdout)

    def test_config_reports_what_a_layer_said_beside_what_chrome_is_given(self):
        """The two are different facts and part company exactly when a value interpolates:
        `flag` is what launched, `stands.said` is what the stanza wrote. Keeping the file's
        spelling is what makes the report findable — a user searching their config for
        `${CROM_CONFIG_DIR}` must be able to see it here."""
        self.crom("init")
        (self.project / ".crom.toml").write_text(
            'namespace = "myproj"\n\n'
            "[defaults]\n"
            'flags = ["--load-extension=${CROM_CONFIG_DIR}/base"]\n\n'
            "[profiles.default]\n"
            'flags = ["--load-extension=${CROM_CONFIG_DIR}/over"]\n'
        )

        human = self.crom("config", "default")
        payload = json.loads(self.crom("config", "default", "--json"))
        (entry,) = [
            item
            for item in payload["resolved"]["flags"]
            if item["flag"].startswith("--load-extension=")
        ]
        (why,) = entry["why"]

        # What Chrome is given: expanded, and identical to the argv line.
        self.assertEqual(entry["flag"], f"--load-extension={self.project}/over")
        self.assertIn(entry["flag"], payload["resolved"]["argv"])
        # What the layers said: the file's own spelling, on both the standing answer and
        # the one it outranked.
        self.assertEqual(why["stands"]["said"], "--load-extension=${CROM_CONFIG_DIR}/over")
        self.assertEqual(
            why["over"], [{"layer": "[defaults]", "said": "--load-extension=${CROM_CONFIG_DIR}/base"}]
        )
        self.assertIn("over --load-extension=${CROM_CONFIG_DIR}/base from [defaults]", human)

    def test_config_shows_the_value_a_drop_took_away_not_just_the_switch(self):
        """A dropped flag is absent from argv, so this line is the only channel carrying
        what was lost. Naming the switch alone tells a user who wrote
        `--window-size=800,600` that something of theirs is gone without confirming it was
        theirs."""
        self.crom("init")
        (self.project / ".crom.toml").write_text(
            'namespace = "myproj"\n\n'
            "[defaults]\n"
            'flags = ["--window-size=800,600"]\n\n'
            "[profiles.default]\n"
            'drop_flags = ["--window-size"]\n'
        )

        human = self.crom("config", "default")
        payload = json.loads(self.crom("config", "default", "--json"))

        self.assertIn(
            "(dropped --window-size=800,600, from [defaults] — removed by [profiles.default])",
            human,
        )
        self.assertEqual(payload["resolved"]["dropped"][0]["stands"]["said"], "--window-size=800,600")

    def test_config_attributes_one_feature_switch_to_every_layer_that_filled_it(self):
        """The shape this ticket exists for: one `--disable-features` carrying names from
        two layers at once, where 'the profile overrode the policy's --disable-features'
        would be a false sentence about a union. Each name is attributed on its own."""
        self.crom("init")
        (self.project / ".crom.toml").write_text(
            'namespace = "myproj"\n\n'
            "[defaults]\n"
            "features = { PictureInPicture = false }\n\n"
            "[profiles.default]\n"
        )

        human = self.crom("config", "default")
        payload = json.loads(self.crom("config", "default", "--json"))
        (entry,) = [
            item
            for item in payload["resolved"]["flags"]
            if item["flag"].startswith("--disable-features=")
        ]

        # crom's own policy feature and the project's ride in one switch, each keeping its
        # own layer — and the human line joins the two clauses rather than picking one.
        self.assertEqual(
            [(why["question"], why["stands"]["layer"]) for why in entry["why"]],
            [("ChromeWhatsNewUI", "crom's launch policy"), ("PictureInPicture", "[defaults]")],
        )
        self.assertIn(
            "ChromeWhatsNewUI from crom's launch policy · PictureInPicture from [defaults]",
            human,
        )

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
        self.assertEqual(
            payload["resolved"]["dropped"],
            [
                {
                    "by": "[profiles.default]",
                    "question": "--disable-sync",
                    "stands": {"layer": "crom's launch policy", "said": "--disable-sync"},
                    "over": [],
                }
            ],
        )
        self.assertIn(
            "(dropped --disable-sync, from crom's launch policy — removed by [profiles.default])",
            human,
        )

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
        entry_key = mcp.entry_key(ProfileRef("myproj", "default"))
        self.assertIn(f"http://127.0.0.1:{port}", entry["mcpServers"][entry_key]["args"])

    def _legacy_file(self, port: int) -> None:
        """The file a crom older than 0f4b8a2 left here: one entry under the constant key."""
        (self.project / ".mcp.json").write_text(
            json.dumps({"mcpServers": {mcp.LEGACY_KEY: mcp.server_entry(port)}})
        )

    def test_mcp_names_the_entry_it_wrote(self):
        # The key stopped being a constant at 0f4b8a2, so it is no longer something the
        # user can predict — and it is what they need to find their profile in the file.
        self.crom("init")
        self.assertIn(
            mcp.entry_key(ProfileRef("myproj", "default")), self.invoke("mcp").stdout
        )

    def test_mcp_reports_the_legacy_entry_it_renamed(self):
        self.crom("init")
        self._legacy_file(int(self.crom("port").strip()))
        result = self.invoke("mcp")
        self.assertIn(mcp.LEGACY_KEY, result.stderr)
        self.assertIn(mcp.entry_key(ProfileRef("myproj", "default")), result.stderr)
        # Renaming an entry is convergence — work crom did that the user did not ask for
        # — so it goes where crom says such things, not into the answer a script parses.
        self.assertNotIn(mcp.LEGACY_KEY, result.stdout)

    def test_mcp_reports_a_legacy_entry_it_left_in_place(self):
        # The outcome nothing else makes visible: the file is left declaring two
        # chrome-devtools servers, and only this line says why crom did not merge them.
        self.crom("init")
        self._legacy_file(int(self.crom("port").strip()) + 100)
        result = self.invoke("mcp")
        self.assertIn(mcp.LEGACY_KEY, result.stderr)
        self.assertIn("two chrome-devtools servers", result.stderr)
        self.assertIn(mcp.LEGACY_KEY, json.loads((self.project / ".mcp.json").read_text())["mcpServers"])

    def test_mcp_says_nothing_about_a_legacy_entry_that_was_never_there(self):
        # The common case, and the reason the outcome is three-valued rather than a
        # message crom always prints: there is nothing here it did on the user's behalf.
        self.crom("init")
        self.assertEqual(self.invoke("mcp").stderr, "")

    def _assert_mcp_wires(self, *profiles: tuple[ProfileRef, int]) -> None:
        """Assert `.mcp.json` here declares exactly `profiles`, each on its own port.

        Counted as well as matched, because the expected mapping is keyed by `entry_key`
        itself: a derivation that collapsed two profiles onto one key would collapse this
        expectation along with the file and the comparison would hold, having asserted
        nothing. The count is the half of the claim measured against the file rather than
        against the code that wrote it. [LAW:one-source-of-truth]

        A ref and a port, rather than the `ResolvedProfile` `mcp.write` insists on. That
        signature exists so no caller can key an entry to one profile and point it at
        another's port; naming the pairing independently here is what lets a test notice
        if one ever does.
        """
        servers = json.loads((self.project / ".mcp.json").read_text())["mcpServers"]
        self.assertEqual(len(servers), len(profiles))
        self.assertEqual(
            servers, {mcp.entry_key(ref): mcp.server_entry(port) for ref, port in profiles}
        )

    def test_mcp_wires_a_second_profile_without_disturbing_the_first(self):
        """The collision the derived key exists to close, at the only level that shows it.

        The key was a constant, so wiring `ci` after `default` in one directory overwrote
        `default` and exited 0. No unit test reaches the case: it needs the ledger to hand
        out two real ports.

        The file is asserted whole rather than looked up by the second key, because the
        claim is that the write *added* to it — a membership check on `ci` alone passed
        against the constant-key code too, which also left a correct-looking `ci` entry
        behind. [LAW:behavior-not-structure]

        Silence on stderr is the same claim from the other side: crom recognises a legacy
        entry by a key it no longer writes, so the entry it wrote for `default` a moment
        ago is not something it offers to rename. Keys alone would miss a spurious rename,
        because renaming crom's own fresh entry still leaves two correct entries behind.
        """
        self.crom("init")
        self.crom("add", "ci")
        default_port = int(self.crom("port").strip())
        ci_port = int(self.crom("port", "ci").strip())

        self.crom("mcp")
        second = self.invoke("mcp", "ci")

        self._assert_mcp_wires(
            (ProfileRef("myproj", "default"), default_port),
            (ProfileRef("myproj", "ci"), ci_port),
        )
        self.assertEqual(second.stderr, "")

    def test_mcp_wires_two_namespaces_that_share_a_profile_name(self):
        """Both halves of the key carry weight, not the profile name alone.

        These two profiles are both called `default`, so only the namespace tells their
        entries apart. A key derived from the name alone passes the test above and still
        collapses this pair — and it is the pair a developer meets first, since `default`
        is the profile `crom init` declares in every project.
        """
        other = self.root / "otherproj"
        other.mkdir()
        self.crom("init", cwd=other)
        other_port = int(self.crom("port", cwd=other).strip())  # registers the namespace
        self.crom("init")
        mine_port = int(self.crom("port").strip())

        self.crom("mcp")
        self.crom("mcp", "otherproj/default")

        self._assert_mcp_wires(
            (ProfileRef("myproj", "default"), mine_port),
            (ProfileRef("otherproj", "default"), other_port),
        )

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

    # --- restart --------------------------------------------------------------------

    def test_restart_stops_and_starts_inside_one_critical_section(self):
        """The claim that makes `crom restart` more than `crom down && crom up`.

        Released between the halves, another crom process is free to land in the gap. A
        concurrent `up` sees nothing running and starts the browser, so this command's own
        start then finds a live Chrome and reports a restart it never performed — on the
        old command line, which is the one thing a restart exists to replace. An `rm` in
        the gap deletes the directory this is about to launch against.

        So the claim under test is the *span* of the lock, not that both halves happen.
        [LAW:no-ambient-temporal-coupling]
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

        with (
            mock.patch("crom.cli.seed.profile_lock", tracking_lock),
            mock.patch("crom.cli.seed.materialize_under_lock"),
            mock.patch("crom.cli.chrome.kill", lambda _p: events.append("kill") or (999,)),
            mock.patch("crom.cli.chrome.find_pids", return_value=()),
            mock.patch("crom.cli.chrome.launch", lambda _p: events.append("launch") or (1234,)),
        ):
            self.crom("restart", "ci")

        self.assertEqual(events, ["lock", "kill", "launch", "unlock"])

    def test_restart_names_the_process_it_replaced(self):
        """Both pids, because the point of the command is that the browser is a new one.
        A message saying only "Restarted" cannot be told from `up` reporting a browser it
        left alone, which is the failure this whole command exists to prevent."""
        self.crom("init")
        self.crom("add", "ci")

        with (
            mock.patch("crom.cli.seed.materialize_under_lock"),
            mock.patch("crom.cli.chrome.kill", return_value=(999,)),
            mock.patch("crom.cli.chrome.find_pids", return_value=()),
            mock.patch("crom.cli.chrome.launch", return_value=(1234,)),
        ):
            output = self.crom("restart", "ci")

        self.assertIn("Restarted myproj/ci", output)
        self.assertIn("was pid 999", output)
        self.assertIn("now pid 1234", output)

    def test_restart_of_a_stopped_profile_starts_it_rather_than_erroring(self):
        """`restart` converges toward running, so the state it was asked to reach is
        reached — it just has nothing to report stopping. Erroring here would be crom
        telling a user to run `crom up` instead, which is the thing README.md promises
        it never does."""
        self.crom("init")
        self.crom("add", "ci")

        with (
            mock.patch("crom.cli.seed.materialize_under_lock"),
            mock.patch("crom.cli.chrome.kill", return_value=()),
            mock.patch("crom.cli.chrome.find_pids", return_value=()),
            mock.patch("crom.cli.chrome.launch", return_value=(1234,)),
        ):
            output = self.crom("restart", "ci")

        self.assertIn("was not running", output)
        self.assertIn("started it", output)

    # --- show -----------------------------------------------------------------------

    def test_show_raises_the_running_browser_without_starting_one(self):
        self.crom("init")
        self.crom("add", "ci")

        with (
            mock.patch("crom.cli.seed.materialize_under_lock"),
            mock.patch("crom.cli.chrome.find_pids", return_value=(999,)),
            mock.patch("crom.cli.chrome.launch", side_effect=AssertionError("must not launch")),
            mock.patch("crom.cli.window.raise_profile", return_value=1) as raised,
        ):
            output = self.crom("show", "ci")

        self.assertEqual(raised.call_args.args[1], (999,))
        self.assertIn("Raised myproj/ci", output)
        self.assertNotIn("Started", output)

    def test_show_starts_a_stopped_profile_and_says_it_did(self):
        """The convergence README.md describes: if the only thing between a command and
        its job is another crom command, crom runs it and says so. `show` names the end
        state 'this profile's window is in front', and a profile that is not running is
        one `crom up` away from it."""
        self.crom("init")
        self.crom("add", "ci")

        with (
            mock.patch("crom.cli.seed.materialize_under_lock"),
            mock.patch("crom.cli.chrome.find_pids", return_value=()),
            mock.patch("crom.cli.chrome.launch", return_value=(1234,)),
            mock.patch("crom.cli.window.raise_profile", return_value=1),
        ):
            output = self.crom("show", "ci")

        self.assertIn("Started myproj/ci", output)
        self.assertIn("Raised myproj/ci", output)

    def test_show_raises_inside_the_lock_it_found_the_pid_under(self):
        """The pid raised must be the pid observed. A `down` landing between the two would
        have crom asking macOS for a process that no longer exists, and the error macOS
        answers with describes a race this command is in a position to prevent."""
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

        with (
            mock.patch("crom.cli.seed.profile_lock", tracking_lock),
            mock.patch("crom.cli.seed.materialize_under_lock"),
            mock.patch("crom.cli.chrome.find_pids", return_value=(999,)),
            mock.patch("crom.cli.window.raise_profile", lambda _p, _pids: events.append("raise") or 1),
        ):
            self.crom("show", "ci")

        self.assertEqual(events, ["lock", "raise", "unlock"])

    def test_restart_names_every_pid_on_both_sides_of_the_message(self):
        """The two halves of one message must agree about how many browsers exist.

        `find_pids` and `chrome.launch` are documented as returning the main browser
        process(es), plural, so reporting `pids[0]` for the new browser while joining every
        stopped one gives a reader a message that quietly disagrees with itself.
        """
        self.crom("init")
        self.crom("add", "ci")

        with (
            mock.patch("crom.cli.seed.materialize_under_lock"),
            mock.patch("crom.cli.chrome.kill", return_value=(998, 999)),
            mock.patch("crom.cli.chrome.find_pids", return_value=()),
            mock.patch("crom.cli.chrome.launch", return_value=(1234, 1235)),
        ):
            output = self.crom("restart", "ci")

        self.assertIn("was pid 998, 999", output)
        self.assertIn("now pid 1234, 1235", output)

    def test_restart_reports_the_stop_even_when_the_start_then_fails(self):
        """A restart whose launch half fails leaves the user with no browser at all.

        Told only that starting failed, they have no reason to think crom stopped the
        working browser they had — so the fact is said before the start is attempted,
        rather than assembled into a result line that a raise never reaches.
        """
        self.crom("init")
        self.crom("add", "ci")

        with (
            mock.patch("crom.cli.seed.materialize_under_lock"),
            mock.patch("crom.cli.chrome.kill", return_value=(999,)),
            mock.patch("crom.cli.chrome.find_pids", return_value=()),
            mock.patch("crom.cli.chrome.launch", side_effect=CromError("Chrome exited 1")),
        ):
            result = self.invoke("restart", "ci", expect=1)

        self.assertIn("Stopped myproj/ci (pid 999)", result.output)
        self.assertIn("Chrome exited 1", result.output)

    def test_restart_json_carries_what_it_stopped(self):
        """`--json` must be able to tell a browser that was replaced from one that was
        merely started — the single distinction this command exists to report, and one the
        profile record alone cannot carry because it describes the profile, not the act."""
        self.crom("init")
        self.crom("add", "ci")

        with (
            mock.patch("crom.cli.seed.materialize_under_lock"),
            mock.patch("crom.cli.chrome.kill", return_value=(999,)),
            mock.patch("crom.cli.chrome.find_pids", return_value=()),
            mock.patch("crom.cli.chrome.launch", return_value=(1234,)),
        ):
            record = json.loads(self.invoke("restart", "ci", "--json").stdout)

        self.assertEqual(record["stopped"], [999])
        self.assertEqual(record["pids"], [1234])

    def test_show_json_carries_whether_a_window_came_forward(self):
        """A script confirming the window actually appeared — the whole point of `show` —
        would otherwise have to parse the human sentence to learn it."""
        self.crom("init")
        self.crom("add", "ci")

        with (
            mock.patch("crom.cli.seed.materialize_under_lock"),
            mock.patch("crom.cli.chrome.find_pids", return_value=(999,)),
            mock.patch("crom.cli.window.raise_profile", return_value=0),
        ):
            record = json.loads(self.invoke("show", "ci", "--json").stdout)

        self.assertEqual(record["windows"], 0)
        self.assertFalse(record["started"])

    def test_show_reports_the_launch_even_when_the_raise_then_fails(self):
        """Withheld Automation access is likeliest on a first run — the same run likeliest
        to have started the browser. Told only that the raise failed, a user goes looking
        for a launch failure that never happened, on a machine now running a browser
        nobody mentioned. [LAW:no-silent-failure]"""
        self.crom("init")
        self.crom("add", "ci")

        with (
            mock.patch("crom.cli.seed.materialize_under_lock"),
            mock.patch("crom.cli.chrome.find_pids", return_value=()),
            mock.patch("crom.cli.chrome.launch", return_value=(1234,)),
            mock.patch("crom.cli.window.raise_profile", side_effect=CromError("no Automation access")),
        ):
            result = self.invoke("show", "ci", expect=1)

        self.assertIn("Started myproj/ci", result.output)
        self.assertIn("no Automation access", result.output)

    def test_show_says_so_when_there_is_no_window_to_bring_forward(self):
        """A browser running headless is raised successfully and shows the user nothing.
        Reporting 'Raised' alone would be crom claiming a window that is not there, and
        the user would be left looking for it. [LAW:no-silent-failure]"""
        self.crom("init")
        self.crom("add", "ci")

        with (
            mock.patch("crom.cli.seed.materialize_under_lock"),
            mock.patch("crom.cli.chrome.find_pids", return_value=(999,)),
            mock.patch("crom.cli.window.raise_profile", return_value=0),
        ):
            output = self.crom("show", "ci")

        self.assertIn("no open windows", output)
        self.assertIn("headless", output)

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
        not raise — this helper's only job is to be informative before a destructive
        act, and a failure to measure is not a reason to refuse the delete."""
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
        suggests was impossible.
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

    def test_every_config_key_the_parser_accepts_is_named_in_help(self):
        """crom's primary reader is an agent that gets one look at `--help` and then
        writes a `.crom.toml` from it, never the parser's source. A key the parser
        accepts but the help never names is invisible to that reader — it cannot be
        discovered at all, only stumbled into after a rejected file. The help was just
        updated to name every key `config.py` accepts; this is what keeps that true
        when someone adds key number seven and updates only the parser.
        [LAW:one-source-of-truth] the parser's frozensets are read here rather than
        restated, so this test agrees with the parser by construction and can only
        drift from the help text, which is the direction that matters.
        """
        help_text = self.crom("--help") + self.crom("config", "--help")
        for keys in (config._SCOPE_KEYS, config._DEFAULTS_KEYS, config._PROFILE_KEYS):
            for key in keys:
                # The key as a word, not as a substring. `env` occurs inside
                # "environment" and `port` inside "--remote-debugging-port", so a plain
                # `assertIn` would keep passing off prose that never names the key —
                # green for a reader who still cannot find it.
                named = re.search(rf"(?<![-\w]){re.escape(key)}(?![\w])", help_text)
                self.assertTrue(named, f"config key {key!r} is not named in --help")

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


    # --- the failure contract ---------------------------------------------------------

    def test_an_os_level_refusal_becomes_a_message_and_an_exit_code(self):
        """Three ways the filesystem can refuse one command, all through `mcp.write`.

        Three errno families across two call sites: `--path <a-directory>` fails in
        `read_text`, while a missing parent and a parent that is a regular file both get
        `False` from `path.exists()` — which swallows the `ENOTDIR` — and fail in
        `write_text`. All three used to reach the user as a stack trace out of `pathlib`
        — exit 1, and nothing else a script could read.

        `invoke` runs with `catch_exceptions=False`, which is what makes an escape
        visible here at all: the runner's default would have handed this test a tidy
        `exit_code == 1` and an empty stderr, and the assertions below would have passed
        against the exact bug they exist to catch.
        """
        self.crom("init")
        self.crom("add", "dev")
        (self.project / "adir").mkdir()
        (self.project / "afile").touch()

        # `os.strerror` rather than the literal text: that half of the message comes from
        # the C library and not from crom, so the C library is its oracle — and naming the
        # errno says which family each case exercises. Still an independent check: crom
        # reads `strerror` off the exception, so `str(e)` would not match.
        for path, code in (
            ("adir", errno.EISDIR),
            ("nosuch/dir/.mcp.json", errno.ENOENT),
            ("afile/x.json", errno.ENOTDIR),
        ):
            with self.subTest(path=path):
                result = self.invoke("mcp", "dev", "--path", path, expect=1)
                self.assertEqual(result.stderr.strip(), f"Error: {path}: {os.strerror(code)}")

    def test_the_contract_covers_a_failure_no_command_anticipated(self):
        """The rule lives at the boundary, so a call site nobody patched is covered too.

        `chrome.scan` guards `ps` being absent and `ps` exiting nonzero — and not a `ps`
        on PATH that cannot be executed. It is the single process-table reader, so that
        `PermissionError` escaped from `list`, `up`, `down`, `rm`, `config` and migration
        alike, and closing it took no edit to `chrome.py`.

        This is the claim the test above cannot make: a `try/except OSError` inside
        `mcp_cmd` closes the reported bug and leaves this one open, so it is the pair that
        distinguishes one boundary rule from a fourth pointwise patch.
        [LAW:single-enforcer]
        """
        self.crom("init")
        with mock.patch(
            "crom.chrome.scan", side_effect=PermissionError(13, "Permission denied", "/bin/ps")
        ):
            result = self.invoke("list", expect=1)
        self.assertEqual(result.stderr.strip(), "Error: /bin/ps: Permission denied")

    def test_a_reader_leaving_a_pipeline_is_not_a_failure_to_report(self):
        """`crom list | head` ends the conventional Unix way: no message, exit 1.

        Click installs a `PacifyFlushWrapper` and exits for an `errno.EPIPE` write, and
        broadening the boundary to `OSError` took that over. Measured against the real
        binary with stdout on a reader-less pipe, `crom list` went from exit 1 and an
        empty stderr to exit 120 and `Error: Broken pipe` — 120 rather than 1 because
        the wrapper never got installed, so interpreter shutdown then failed to flush
        stdout as well. Two regressions from one widened `except`.
        """
        self.crom("init")
        with mock.patch(
            "crom.chrome.scan", side_effect=BrokenPipeError(errno.EPIPE, "Broken pipe")
        ):
            result = self.invoke("list", expect=1)
        self.assertEqual(result.stderr, "")

        # And the other half of the carve-out: `BrokenPipeError` is raised for ESHUTDOWN
        # too, which is a socket crom really did fail to write and no reader leaving.
        # Click declines it (its own test is `errno.EPIPE`), so handing that class back
        # wholesale would return the traceback this PR closed.
        with mock.patch(
            "crom.chrome.scan", side_effect=BrokenPipeError(errno.ESHUTDOWN, "Cannot send")
        ):
            result = self.invoke("list", expect=1)
        self.assertEqual(result.stderr.strip(), "Error: Cannot send")



if __name__ == "__main__":
    unittest.main()
