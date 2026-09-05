"""End-to-end tests for the CLI contract: what a script sees on stdout and in $?.

Chrome is never launched here — these cover everything up to the process, which is the
part apps integrate against. `chrome.scan` is stubbed because the process table is an
external system, not an implementation detail of crom.
"""

import ast
import contextlib
import errno
import importlib.metadata
import json
import os
import re
import shlex
import shutil
import tempfile
import unittest
from dataclasses import replace
from itertools import takewhile
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from crom import cli, config, configwrite, doctor, launched, mcp, migrate, registry
from crom.config import load_ambient
from crom.model import USER_NAMESPACE, Conflict, CromError, ProfileRef, Reason
from crom.paths import registry_file, state_home, user_config_file

# README.md is the only place a reader learns the failure envelope's vocabulary, so it
# holds two enumerations the code owns. Located from this file rather than from the
# working directory, so the suite reads the same README wherever it is run from.
_README = Path(__file__).resolve().parent.parent / "README.md"


def _readme_names(fragment: str) -> tuple[str, ...]:
    """The names in one README fragment — a table cell, or one side of a sentence.

    Backticks are required rather than merely stripped, which is what makes this a parse
    and not a scrape: the prose around these fragments is full of backticked words that
    are not names — `null`, `CROM_CONFIG`, `crom init`, `user/default` — so a fragment
    that has drifted into prose fails here instead of yielding a name with punctuation
    stuck to it. [LAW:parse-dont-validate]
    """
    ticked = tuple(re.split(r",\s*|\s+and\s+", fragment.strip()))
    names = tuple(name.strip("`") for name in ticked)
    assert ticked == tuple(f"`{name}`" for name in names), f"not a list of names: {fragment!r}"
    return names


def _commands_offering_json() -> set[str]:
    """Both callers are drift guards: a change to how `--json` is declared has to fail
    them for the right reason, rather than being patched into agreement in two places.
    """
    return {
        name
        for name in cli.main.list_commands(None)
        if any("--json" in option.opts for option in cli.main.get_command(None, name).params)
    }


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.project = self.root / "myproj"
        self.project.mkdir()
        self.env = mock.patch.dict(
            os.environ,
            {
                # HOME as well as the XDG variables, and not optional:
                # `migrate.run_if_needed()` runs before every command, and migration
                # locates the legacy installation through `Path.home()` — the lookup that
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
        # The port table is an external system for the same reason the process table is,
        # and `crom doctor` now asks it about every reservation. Unstubbed, a developer
        # running this suite with their own Chrome on 9222 would see rows this file never
        # wrote. A test that is *about* a held port names the port it holds, inline, the
        # way the tests about a running browser name the directory.
        self.ports = mock.patch("crom.chrome.port_is_free", return_value=True)
        self.ports.start()

    def tearDown(self):
        self.ports.stop()
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
        with self._standing_in(cwd):
            # `catch_exceptions=False` because the default one lies about this boundary:
            # it turns an escaping exception into a tidy `exit_code == 1` with nothing in
            # `output`, which is precisely how a real process behaves *only after*
            # `CromGroup.invoke` has caught it. Under the default, every `expect=1` in this
            # file passes just as happily against a command that printed a stack trace.
            # [FRAMING:representation] the runner is a map of the terminal; this keeps a
            # crash looking like a crash.
            result = CliRunner().invoke(cli.main, list(args), catch_exceptions=False)
        self.assertEqual(
            result.exit_code, expect, f"crom {' '.join(args)} -> {result.exit_code}\n{result.output}"
        )
        return result

    @contextlib.contextmanager
    def _standing_in(self, cwd: Path | None):
        """The directory crom runs in, restored afterwards — see `invoke` for why it is
        an input rather than a detail. Both ways in share this one, so neither can be
        given the discipline the other has. [LAW:one-source-of-truth]"""
        previous = Path.cwd()
        os.chdir(cwd or self.project)
        try:
            yield
        finally:
            os.chdir(previous)

    def failure(self, *args, cwd: Path | None = None) -> CromError:
        """The `CromError` behind a failed command, for the reasons no envelope carries.

        A command that answers in prose only has no envelope, so its slug is unobservable
        from outside — click's standalone mode turns `CromGroup.invoke`'s answer into a
        `SystemExit`, and the chain goes with it. Standing that mode down keeps the link the boundary already
        builds — `raise _answer(...) from error` — which is how a test reads these reasons
        without calling a command's callback by hand and losing everything that runs
        before it.

        The command runs identically either way: only click's handling of what it raised
        differs, so a test may still assert on what the run left behind.
        """
        with self._standing_in(cwd), self.assertRaises(cli._Failure) as caught:
            CliRunner().invoke(
                cli.main, list(args), catch_exceptions=False, standalone_mode=False
            )
        return caught.exception.__cause__

    def crom(self, *args, cwd: Path | None = None, expect: int = 0):
        """What a script sees: stdout and stderr as one stream, which is what a terminal
        shows. `invoke` is for the few tests that need the two told apart."""
        return self.invoke(*args, cwd=cwd, expect=expect).output

    # --- bootstrap ------------------------------------------------------------------

    def test_the_suite_cannot_reach_the_real_home(self):
        """A guard rail rather than a feature test.

        `migrate.run_if_needed()` runs before every command, and migration locates the
        legacy installation through `Path.home()` — the one lookup that deliberately
        ignores XDG, because that is where the pre-namespace crom actually wrote. A harness redirecting only the XDG variables would let this suite find a
        developer's real `~/.config/crom/profiles.json` and migrate their profiles
        mid-run. If this fails, stop and fix the harness before trusting any result.
        """
        self.assertTrue(str(Path.home()).startswith(str(self.root)))
        self.assertTrue(str(migrate.legacy_registry_file()).startswith(str(self.root)))
        self.assertFalse(migrate.needed())

    def test_first_run_declares_a_user_default_profile(self):
        self.crom("list")
        self.assertIn('[profiles.default]', user_config_file().read_text())
        self.assertIn('seed = "default"', user_config_file().read_text())

    def test_a_corrupt_user_config_is_reset_rather_than_left_for_the_user(self):
        """A config crom cannot parse takes out every command, repairs included.

        `_bootstrap_user_config()` runs before any command does, so the failure used
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
        with mock.patch(
            "crom.config.find_chrome", side_effect=Reason.CHROME_UNUSABLE.error("no Chrome here")
        ):
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

        self.assertIn("  namespace: declared chosen, you asked for other", output.splitlines())
        self.assertIn("chosen", (self.project / ".crom.toml").read_text())

    def test_init_refuses_to_restate_a_different_seed(self):
        self.crom("init", "--seed", "fresh")

        output = self.crom("init", "--seed", "default", expect=4)

        # [LAW:one-source-of-truth] this label is also `declaration_differs.settings`, and
        # `init` carries no `--json` — the rendered line is the only place it is observable.
        # Line-exact, not `assertIn` over the text, which "[defaults].seed" ends in.
        self.assertIn("  seed: declared fresh, you asked for default", output.splitlines())
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

        self.assertIn("  seed: declared default, you asked for fresh", output.splitlines())
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

        self.assertIn(
            f"  port: declared (unpinned — crom assigned {assigned}), you asked for {assigned}",
            output.splitlines(),
        )
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

        self.assertIn(
            "  flags: declared --disable-blink-features=A, "
            "you asked for --disable-blink-features=B",
            output.splitlines(),
        )

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

        self.assertIn("  flags: declared (unset), you asked for --headless", output.splitlines())
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

    def test_a_bare_crom_launches_the_default_profile_like_any_other_command(self):
        """`crom` with no arguments is `crom up`, and it is one now rather than nearly one.

        The group used to carry `invoke_without_command` and reach `up`'s callback
        through `ctx.invoke`, which goes around `Command.invoke` — so everything
        `CromCommand` does to ready a command would have needed spelling a second time to
        cover the bare invocation. `CromGroup.parse_args` supplies the name instead,
        making it an ordinary invocation; this says it still means what the README says
        it means. [LAW:one-source-of-truth]
        """
        self.crom("init")

        with mock.patch("crom.chrome.launch", return_value=(4321,)):
            with mock.patch("crom.seed.materialize_under_lock"):
                bare = self.crom()
                named = self.crom("up")

        self.assertEqual(bare, named)
        self.assertIn("myproj/default", bare)

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

    # --- drift ----------------------------------------------------------------------

    def _record_the_launch_of(self, ref: str, env: dict[str, str] | None = None) -> str:
        """Leave the record a real launch of `ref` would leave, and name its directory.

        Written through `launched.record` from the argv `crom config` reports, so what
        these tests compare against is the file crom's own launch path writes rather than
        one this suite hand-built to a schema it invented. Chrome is never launched here,
        and this is the half of a launch that outlives the process.
        [LAW:one-source-of-truth]

        `env` is stated by the caller because it is the one half of a launch that
        `crom config --json` does not publish. Every config written below declares none.
        """
        resolved = json.loads(self.crom("config", ref, "--json"))["resolved"]
        directory = Path(resolved["profile_dir"])
        # The directory first, because a real launch has already been through
        # `seed.materialize_under_lock` by the time `chrome.launch` writes the record —
        # the record lives *inside* the user-data-dir. Without this, `record` correctly
        # reports that it could not write, and every verdict below is `unmeasured`.
        # [LAW:no-ambient-temporal-coupling] the ordering is stated, not assumed.
        directory.mkdir(parents=True, exist_ok=True)
        launched.record(directory, launched.Launch(tuple(resolved["argv"]), env or {}))
        return str(directory)

    def _resize_ci(self) -> None:
        """The config edit crom used to say nothing about: a flag changed under a browser."""
        config_path = self.project / ".crom.toml"
        config_path.write_text(config_path.read_text().replace("800,600", "1280,800", 1))

    def test_list_reports_a_running_browser_whose_config_has_since_changed(self):
        """The epic's whole complaint, at the surface a user actually reads.

        Before this, an edited profile and an untouched one gave `crom list` the same row
        — `running :<port>` — and the only way to find out the browser was launched from a
        configuration that no longer exists was to remember having edited the file.
        """
        self.crom("init")
        self.crom("add", "ci", "--flag", "--window-size=800,600")
        directory = self._record_the_launch_of("ci")

        with mock.patch("crom.chrome.scan", return_value={directory: (4242,)}):
            before = self._ci_row(self.crom("list"))
            self._resize_ci()
            after = self._ci_row(self.crom("list"))

        self.assertIn("running :", before)
        self.assertIn("running with what this configuration resolves to", before)
        self.assertIn("drifted — --window-size", after)

    def test_list_leaves_a_stopped_profile_out_of_the_drift_it_could_not_have(self):
        """The record outlives the browser, and a browser that is gone is not stale.

        `crom down` deliberately leaves the launch record in the user-data-dir, so the
        edit below has a record to disagree with. Reported as drifted, every stopped
        profile whose config was ever touched would read as a browser running the wrong
        flags — and `crom up` would be converging on something that is not there.
        """
        self.crom("init")
        self.crom("add", "ci", "--flag", "--window-size=800,600")
        self._record_the_launch_of("ci")
        self._resize_ci()

        row = self._ci_row(self.crom("list"))

        self.assertIn("stopped :", row)
        self.assertIn("not running, so its next launch takes this configuration", row)
        self.assertNotIn("drifted", row)

    def test_list_json_carries_the_verdict_and_what_moved_beside_the_record(self):
        """A script watching for stale browsers reads the verdict, never the prose."""
        self.crom("init")
        self.crom("add", "ci", "--flag", "--window-size=800,600")
        directory = self._record_the_launch_of("ci")
        self._resize_ci()

        with mock.patch("crom.chrome.scan", return_value={directory: (4242,)}):
            records = json.loads(self.crom("list", "--json"))

        row = next(record for record in records if record["ref"] == "myproj/ci")
        self.assertEqual(row["drift"]["verdict"], "drifted")
        self.assertEqual(
            row["drift"]["changes"],
            [
                {
                    "subject": "--window-size",
                    "launched": "--window-size=800,600",
                    "resolves": "--window-size=1280,800",
                }
            ],
        )

    def test_config_says_what_the_running_browser_was_launched_with_instead(self):
        """`crom config` prints what a launch *would* be, which is what hides the drift.

        Every line of the listing is the current resolution, so an edited `--window-size`
        appears there as its new value with nothing saying the running browser never got
        it. The old value has no other channel. [LAW:no-silent-failure]
        """
        self.crom("init")
        self.crom("add", "ci", "--flag", "--window-size=800,600")
        directory = self._record_the_launch_of("ci")
        self._resize_ci()

        with mock.patch("crom.chrome.scan", return_value={directory: (4242,)}):
            output = self.crom("config", "ci")

        self.assertIn("drifted — --window-size", output)
        self.assertIn(
            "--window-size: launched --window-size=800,600, now --window-size=1280,800", output
        )

    def test_config_json_carries_the_verdict_beside_the_resolution(self):
        self.crom("init")
        self.crom("add", "ci", "--flag", "--window-size=800,600")
        directory = self._record_the_launch_of("ci")

        with mock.patch("crom.chrome.scan", return_value={directory: (4242,)}):
            resolved = json.loads(self.crom("config", "ci", "--json"))["resolved"]

        self.assertEqual(resolved["drift"]["verdict"], "matches")
        self.assertEqual(resolved["drift"]["changes"], [])

    def test_a_running_browser_crom_has_no_record_of_is_not_reported_as_current(self):
        """No record is "crom cannot tell". Answered `matches`, crom would be guessing.

        Reachable on any browser a crom older than the launch record started, and on any
        profile whose directory was cleaned out under a running Chrome.
        """
        self.crom("init")
        self.crom("add", "ci")
        directory = json.loads(self.crom("config", "ci", "--json"))["resolved"]["profile_dir"]

        with mock.patch("crom.chrome.scan", return_value={directory: (4242,)}):
            row = self._ci_row(self.crom("list"))

        self.assertIn("crom has no record of how the browser in", row)
        self.assertNotIn("running with what this configuration resolves to", row)

    def _ci_row(self, listing: str) -> str:
        """The one line of a listing that is about `myproj/ci`."""
        (row,) = [line for line in listing.splitlines() if "myproj/ci" in line]
        return row

    # --- converging on drift ------------------------------------------------------------

    # What a stubbed launch reports, distinct from the 4242 a browser is found on above, so
    # a message naming both pids is read for the sequence it describes and not merely for
    # containing a number.
    RELAUNCHED = (5150,)

    @contextlib.contextmanager
    def _process_table_that(self, running: dict[str, tuple[int, ...]]):
        """A stubbed process table that a stubbed `kill` empties and a stubbed `launch` refills.

        `crom up` on a drifted browser reads the table, stops what it finds, and reads it
        again. Answering both reads from one fixed dict would have the second find the
        browser crom had just killed, so no relaunch would happen and the test would be
        asserting against a sequence that cannot occur live. Modelling the table as state
        the two effects move is what makes the ordering observable rather than assumed.
        [LAW:no-ambient-temporal-coupling]

        The stubbed launch writes the record a real one writes, and only that: the record
        is what the *next* verdict is computed from, so a stub that skipped it would leave
        every browser crom relaunched reading back as `unmeasured`.
        """

        def kill(profile):
            return running.pop(str(profile.profile_dir), ())

        def launch(profile):
            launched.record(profile.profile_dir, launched.Launch.of(profile))
            running[str(profile.profile_dir)] = self.RELAUNCHED
            return self.RELAUNCHED

        with (
            mock.patch("crom.chrome.scan", side_effect=lambda: dict(running)),
            mock.patch("crom.chrome.kill", side_effect=kill),
            mock.patch("crom.chrome.launch", side_effect=launch),
            mock.patch("crom.seed.materialize_under_lock"),
        ):
            yield

    def test_up_relaunches_a_browser_its_config_has_moved_past(self):
        """The epic's whole complaint, answered at the command that caused it.

        `crom up` here used to print "Already running" and leave the browser on the old
        command line, so applying an edit meant knowing, unprompted, to run `crom down`
        first — the one thing README's convergence promise says crom never asks of you.
        """
        self.crom("init")
        self.crom("add", "ci", "--flag", "--window-size=800,600")
        directory = self._record_the_launch_of("ci")
        self._resize_ci()

        with self._process_table_that({directory: (4242,)}):
            output = self.crom("up", "ci")
            settled = self._ci_row(self.crom("list"))

        self.assertIn("Relaunched myproj/ci", output)
        self.assertIn("was pid 4242, now pid 5150", output)
        self.assertIn(
            "--window-size: launched --window-size=800,600, now --window-size=1280,800", output
        )
        # Which layer supplied the value the browser now carries. `drift` cannot say — it
        # holds two argvs and no config — so this is the one assertion that the clause is
        # actually joined on rather than merely printed near.
        self.assertIn("(from [profiles.ci])", output)
        # And the half a message cannot make good on: the browser crom left running is the
        # one this configuration describes.
        self.assertIn("running with what this configuration resolves to", settled)

    def test_up_relaunches_on_a_drift_that_names_no_switch(self):
        """Flags that moved only in order are drifted, and `Drifted.changes` is empty.

        A converge written against `len(changes)` reads this as agreement and leaves the
        browser standing on an order Chrome was not given — so this is the case that says
        the decision is made on the verdict's type and never on the count.
        """
        self.crom("init")
        self.crom("add", "ci", "--flag", "--window-size=800,600", "--flag", "--no-pings")
        resolved = json.loads(self.crom("config", "ci", "--json"))["resolved"]
        directory = Path(resolved["profile_dir"])
        directory.mkdir(parents=True, exist_ok=True)
        # The same switches, recorded in an order no current resolution produces. `argv[0]`
        # stays put: it is the executable, not a switch, and reversing it in would be a
        # second change on top of the one this test is about.
        argv = resolved["argv"]
        launched.record(directory, launched.Launch((argv[0], *reversed(argv[1:])), {}))

        with self._process_table_that({str(directory): (4242,)}):
            output = self.crom("up", "ci")

        self.assertIn("Relaunched myproj/ci", output)
        self.assertIn("its flags are in a different order", output)

    def test_up_will_not_relaunch_a_browser_it_cannot_measure(self):
        """"crom cannot tell" is neither "nothing changed" nor "something changed".

        Every browser a crom older than the launch record started arrives here, so
        relaunching would make the upgrade itself kill a running browser — and take the
        user's tabs with it — on no evidence at all. Reporting a bare "Already running" is
        the other way to be wrong: crom would be claiming a check it never made.
        """
        self.crom("init")
        self.crom("add", "ci", "--flag", "--window-size=800,600")
        directory = json.loads(self.crom("config", "ci", "--json"))["resolved"]["profile_dir"]
        Path(directory).mkdir(parents=True, exist_ok=True)

        with self._process_table_that({directory: (4242,)}):
            record = json.loads(self.crom("up", "ci", "--json"))
            spoken = self.crom("up", "ci")

        self.assertEqual(record["found"]["verdict"], "unmeasured")
        self.assertEqual(record["stopped"], [])
        self.assertEqual(record["pids"], [4242])
        self.assertIn("Already running myproj/ci", spoken)
        self.assertIn("crom has no record of how the browser in", spoken)

    def test_up_leaves_a_browser_that_already_matches_alone(self):
        """Idempotence, which converging must not cost. README promises `crom up` on a
        running browser behaves like the end state it names, and a relaunch here would
        make the command that reports agreement the one that destroys it."""
        self.crom("init")
        self.crom("add", "ci", "--flag", "--window-size=800,600")
        directory = self._record_the_launch_of("ci")

        with self._process_table_that({directory: (4242,)}):
            record = json.loads(self.crom("up", "ci", "--json"))

        self.assertEqual(record["found"]["verdict"], "matches")
        self.assertEqual(record["stopped"], [])
        self.assertEqual(record["pids"], [4242])

    def test_up_json_carries_the_verdict_it_acted_on_and_the_pids_it_replaced(self):
        """What a script that ran `crom up` to apply an edit needs: whether anything was
        actually replaced, and why.

        Spelled `found` rather than `drift` deliberately. `crom list` and `crom config`
        publish `drift` for how a profile stands *now*; this is how it stood before this
        command fixed it, and one key meaning both across the JSON surface is how a
        consumer comes to alert on a browser crom had already put right.
        """
        self.crom("init")
        self.crom("add", "ci", "--flag", "--window-size=800,600")
        directory = self._record_the_launch_of("ci")
        self._resize_ci()

        # `stdout` and not the merged stream: a drifted `up` also says on stderr that it is
        # replacing the browser, and a `--json` caller parses one of the two.
        with self._process_table_that({directory: (4242,)}):
            record = json.loads(self.invoke("up", "ci", "--json").stdout)

        self.assertEqual(record["found"]["verdict"], "drifted")
        self.assertEqual(
            record["found"]["changes"],
            [
                {
                    "subject": "--window-size",
                    "launched": "--window-size=800,600",
                    "resolves": "--window-size=1280,800",
                }
            ],
        )
        self.assertEqual(record["stopped"], [4242])
        self.assertEqual(record["pids"], list(self.RELAUNCHED))

    def test_up_names_a_change_no_layer_supplies_without_inventing_one(self):
        """The two changes that have no layer clause to carry, in one relaunch.

        A switch the config has since removed has no current side to attribute at all, and
        an environment variable is not a switch — no layer *emits* one, so the provenance
        `crom config` computes has nothing to say about it. Both are reported as changes,
        and a clause on either would be crom naming a source it does not have.
        """
        self.crom("init")
        self.crom("add", "ci", "--flag", "--window-size=800,600")
        directory = self._record_the_launch_of("ci", env={"TZ": "UTC"})
        config_path = self.project / ".crom.toml"
        config_path.write_text(
            config_path.read_text().replace(
                'flags = ["--window-size=800,600"]', 'env = { TZ = "America/Denver" }'
            )
        )

        with self._process_table_that({directory: (4242,)}):
            output = self.crom("up", "ci")

        self.assertIn("--window-size: launched --window-size=800,600, now (absent)", output)
        self.assertIn("env TZ: launched UTC, now America/Denver", output)
        self.assertNotIn("(from", output)

    def test_show_raises_a_drifted_window_rather_than_replacing_it(self):
        """Converging is `crom up`'s job, and it is the whole difference between the two
        commands now that they share a start path.

        `show` names one end state — that window, in front — and a user who asked for it
        would otherwise have crom close the window instead, on the strength of a config
        edit they made after the browser started. The shared helper hands `show` the same
        verdict `up` acts on, so nothing but this keeps the convergence out of it.
        """
        self.crom("init")
        self.crom("add", "ci", "--flag", "--window-size=800,600")
        directory = self._record_the_launch_of("ci")
        self._resize_ci()

        with self._process_table_that({directory: (4242,)}):
            with mock.patch("crom.cli.window.raise_profile", return_value=1):
                record = json.loads(self.crom("show", "ci", "--json"))

        self.assertEqual(record["pids"], [4242])
        self.assertFalse(record["started"])

    def test_up_says_why_it_is_replacing_a_browser_before_it_stops_it(self):
        """On stderr, and before the kill, for the reason `restart` says what it stopped
        before starting again: a relaunch whose start half fails leaves the user with no
        browser, and an error naming only the start would hide that crom stopped the
        working one they had. It doubles as the progress line for the pause."""
        self.crom("init")
        self.crom("add", "ci", "--flag", "--window-size=800,600")
        directory = self._record_the_launch_of("ci")
        self._resize_ci()

        with self._process_table_that({directory: (4242,)}):
            result = self.invoke("up", "ci")

        self.assertIn("drifted — --window-size; replacing it", result.stderr)
        # Convergence is work crom did that the user did not ask for, so it goes where
        # crom says such things and not into the answer a script parses.
        self.assertNotIn("replacing it", result.stdout)

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

    # --- the ledger, and seeing into it ---------------------------------------------

    def test_doctor_reports_a_reservation_no_config_still_declares(self):
        """The leak the command exists for, and the reason `crom list` cannot report it.

        `crom list` reads the config files; the ledger is a second map of the same
        profiles, and deleting a stanza updates one of them. The reservation then holds
        its port forever — invisible to every command, and skipped by the next profile
        crom assigns, so the numbers develop a hole nothing explains.

        `crom list` is asserted here rather than taken on trust. Without it this passes
        against a `doctor` that only re-renders what the config declares, which is the one
        thing this command must not be. [LAW:verifiable-goals]
        """
        config_file = self.project / ".crom.toml"
        config_file.write_text('namespace = "myapp"\n\n[profiles.alpha]\n[profiles.beta]\n')
        self.crom("list")  # resolving both stanzas is what reserves both ports

        config_file.write_text('namespace = "myapp"\n\n[profiles.alpha]\n')

        self.assertNotIn("myapp/beta", self.crom("list"))
        self.assertIn("myapp/beta", self.crom("doctor"))

    def test_doctor_publishes_the_ledger_it_read_and_every_row_in_it(self):
        """The whole row, so the published shape is pinned rather than sampled.

        The ledger's own path is in the answer because releasing one orphaned reservation
        means editing that file by hand until `crom doctor` can do it — a reader shown a
        leak needs to know where it is written. It is in both renderings, which is why the
        JSON is an object where `crom list` gives an array: an array has nowhere to put a
        fact about the listing itself. [FRAMING:representation]

        Ports ascend because the ledger is a ledger of ports: a hole in the run, or two
        rows on one number, is what a reader is here to find, and neither survives an
        order sorted by name.
        """
        (self.project / ".crom.toml").write_text(
            'namespace = "myapp"\n\n[profiles.dev]\nport = 9333\n[profiles.qa]\n'
        )
        self.crom("list")

        answer = json.loads(self.crom("doctor", "--json"))

        self.assertEqual(answer["registry"], str(registry_file()))
        self.assertIn(
            {
                "ref": "myapp/dev",
                "port": 9333,
                "pinned": True,
                "source": str(self.project / ".crom.toml"),
                "standing": "declared",
                "finding": f"{self.project / '.crom.toml'} declares it",
                # The second verdict, published beside the first rather than folded into
                # it: this row is declared *and* nothing is on its port, and a reader who
                # only ever sees one of those cannot tell it from a row whose port a
                # stranger holds.
                "liveness": "idle",
                "liveness_finding": "nothing holds port 9333",
            },
            answer["reservations"],
        )
        ports = [row["port"] for row in answer["reservations"]]
        self.assertEqual(ports, sorted(ports))

    def test_doctor_reads_a_ledger_that_was_repaired_by_hand(self):
        """Hand-editing this file is how an orphan is released today, so the command that
        finds orphans has to survive a file that was hand-edited.

        `registry._read` checks every entry and no key, so an edit can leave a name
        `ProfileRef` would refuse. The row carries that name whole for exactly this
        reason: split into `namespace` and `profile` the way a profile record is, doctor
        would have to refuse the row, and the one command that could show the mess would
        be the one command that could not run on the machine that has it.
        [LAW:types-are-the-program] the row claims only what a ledger key really is.
        """
        self.crom("list")
        ledger = json.loads(registry_file().read_text())
        ledger["ports"]["Not A Ref!"] = {"port": 9444, "pinned": False, "source": None}
        registry_file().write_text(json.dumps(ledger))

        rows = json.loads(self.crom("doctor", "--json"))["reservations"]

        self.assertIn(
            {
                "ref": "Not A Ref!",
                "port": 9444,
                "pinned": False,
                "source": None,
                # Judged, not skipped and not fatal. `parse_ref` raises on this name, so a
                # standing derived by taking the key apart would have ended the command
                # here; the standings are decided by asking each config which keys it
                # declares and testing membership, which answers for every string a
                # ledger can hold.
                "standing": "unchecked",
                "finding": "the ledger records no config for it",
                # `unchecked` about its config and answered about its port, on one row.
                # Nothing holds 9444, and that is settled without ever naming a profile
                # directory — which is the only reason a key this shape can be answered
                # about at all.
                "liveness": "idle",
                "liveness_finding": "nothing holds port 9444",
            },
            rows,
        )

    def _rows(self) -> dict[str, dict]:
        """Every reservation row, keyed by the ledger key it was filed under."""
        rows = json.loads(self.crom("doctor", "--json"))["reservations"]
        return {row["ref"]: row for row in rows}

    def _standings(self) -> dict[str, str]:
        """Every reservation's standing, keyed by the ledger key it was filed under."""
        return {ref: row["standing"] for ref, row in self._rows().items()}

    def test_doctor_separates_a_reservation_nothing_declares_from_one_that_lives(self):
        """The epic's own reproduction, and the whole of this ticket.

        Listing every reservation made the leak visible; a reader still had to hold the
        config in their head to know which of them had leaked. Both rows here are in the
        ledger and only one is in the config, so a doctor that reported the two alike
        would be showing the leak without saying it is one.

        `alpha` is asserted as well as `beta`. Without it this passes against a doctor
        that calls everything orphaned, which is the answer that would send a user
        releasing a port their project is using. [LAW:verifiable-goals]
        """
        config_file = self.project / ".crom.toml"
        config_file.write_text('namespace = "myapp"\n\n[profiles.alpha]\n[profiles.beta]\n')
        self.crom("list")  # resolving both stanzas is what reserves both ports

        config_file.write_text('namespace = "myapp"\n\n[profiles.alpha]\n')

        standings = self._standings()

        self.assertEqual("declared", standings["myapp/alpha"])
        self.assertEqual("orphaned", standings["myapp/beta"])
        self.assertIn(f"{config_file} no longer declares it", self.crom("doctor"))

    def test_doctor_names_a_personal_profile_declared_like_any_other(self):
        """The `user` namespace lives in a file that may not name a namespace at all.

        `config.parse` refuses a `namespace` key in the user config — the name is a
        property of the fixed path, which is why `load_user_scope` supplies it — so
        consulting that file the way a project's is consulted fails on the one key it is
        forbidden to have. Every personal profile then read `unchecked`, which is the
        first row of the listing on every machine.
        """
        self.crom("list")

        self.assertEqual("declared", self._standings()["user/default"])

    def test_doctor_will_not_call_a_reservation_orphaned_on_a_config_it_could_not_read(self):
        """The distinction the command that releases a reservation will spend.

        A config that will not parse may well still declare the profile, so "orphaned"
        here would be crom turning "I cannot tell" into "nothing declares it" and handing
        a user a release for a port their project is using. Released ports are
        irreversible — every checked-in `.mcp.json` pointing at the number breaks — so
        the unreadable config gets its own standing rather than the confident one.
        [LAW:no-silent-failure]
        """
        config_file = self.project / ".crom.toml"
        config_file.write_text('namespace = "myapp"\n\n[profiles.alpha]\n')
        self.crom("list")

        config_file.write_text('namespace = "myapp"\n\n[profiles.alpha\n')

        self.assertEqual("unchecked", self._standings()["myapp/alpha"])
        self.assertIn(f"{config_file} could not be checked", self.crom("doctor"))

    def test_doctor_checks_a_config_that_is_gone_rather_than_declining_to_look(self):
        """A file that is not there declares nothing, and that is an answer, not a gap.

        The line against `unchecked` is whether crom learned anything: an absent config
        settles what it declares, where an unreadable one settles nothing. Filing this as
        unchecked instead would leave the commonest leak of all — a project directory
        deleted with its reservations still held — permanently unreleasable by the
        command the epic exists to build.
        """
        config_file = self.project / ".crom.toml"
        config_file.write_text('namespace = "myapp"\n\n[profiles.alpha]\n')
        self.crom("list")
        config_file.unlink()

        self.assertEqual("orphaned", self._standings()["myapp/alpha"])
        self.assertIn(f"{config_file} no longer exists", self.crom("doctor"))

    def test_doctor_orphans_a_reservation_whose_config_renamed_its_namespace(self):
        """A rename in place leaves reservations under a name nothing answers to.

        `remember_namespace` is additive, so nothing removes the old ledger keys when a
        config edits its own `namespace`. This needs no case of its own in the comparison
        and gets none: the standings come from asking each config which ledger keys it
        declares, and a renamed config simply stops producing the old ones.
        """
        config_file = self.project / ".crom.toml"
        config_file.write_text('namespace = "myapp"\n\n[profiles.alpha]\n')
        self.crom("list")

        config_file.write_text('namespace = "renamed"\n\n[profiles.alpha]\n')

        self.assertEqual("orphaned", self._standings()["myapp/alpha"])

    def test_doctor_judges_a_ledger_key_that_is_not_a_reference_at_all(self):
        """The trap this ticket was warned about, held open by a test.

        Deciding whether a key is still declared invites `model.parse_ref`, which *raises*
        on a name `ProfileRef` would refuse — and `registry._read` validates every entry
        and no key, so such keys exist and doctor deliberately reports them. A comparison
        that took keys apart would turn the command that shows the mess into the command
        that dies on the machine that has it.

        Both keys are provably declared by nothing: every key a config can produce is
        `str(ProfileRef(...))`, which carries exactly one `/` and no capitals. So
        `orphaned` is not leniency here — it is the true answer, and it is what will let
        a release reclaim a port that a hand-edit stranded.
        """
        config_file = self.project / ".crom.toml"
        config_file.write_text('namespace = "myapp"\n\n[profiles.alpha]\n')
        self.crom("list")
        ledger = json.loads(registry_file().read_text())
        for key, port in (("a/b/c", 9444), ("MyApp/UPPER", 9445)):
            ledger["ports"][key] = {"port": port, "pinned": False, "source": str(config_file)}
        registry_file().write_text(json.dumps(ledger))

        standings = self._standings()

        self.assertEqual("orphaned", standings["a/b/c"])
        self.assertEqual("orphaned", standings["MyApp/UPPER"])

    # --- who holds the port a reservation names -------------------------------------

    def _two_profiles(self) -> None:
        """Two reservations on ports this file chose, so a held port is one it named."""
        (self.project / ".crom.toml").write_text(
            'namespace = "myapp"\n\n[profiles.alpha]\nport = 9401\n'
            "[profiles.beta]\nport = 9402\n"
        )
        self.crom("list")  # resolving both stanzas is what reserves both ports

    def test_doctor_reports_a_reservation_whose_port_a_stranger_holds(self):
        """The finding this ticket exists for, and why it cannot be a fourth standing.

        crom promises a port that never moves, but `crom port`, `crom env` and `crom mcp`
        hand the number out with no liveness check — only `crom up` verifies, through
        `chrome._require_port_available`. So a tool wired by `crom mcp` connects to
        whatever is answering there, and nothing reported that until now.

        The row is `declared` in the same breath: the config is perfect, and the machine
        is not. A doctor that answered this on the standing axis would have to call a
        reservation both `declared` and something else at once, which one enum cannot say.
        [LAW:types-are-the-program]

        `beta` is what makes this a detection rather than a label — a doctor that called
        every reservation `foreign` passes the `alpha` assertion on its own.
        [LAW:verifiable-goals]
        """
        self._two_profiles()

        with mock.patch("crom.chrome.port_is_free", lambda port: port != 9401):
            rows = self._rows()

        self.assertEqual("foreign", rows["myapp/alpha"]["liveness"])
        self.assertEqual("declared", rows["myapp/alpha"]["standing"])
        self.assertEqual("idle", rows["myapp/beta"]["liveness"])

    def test_doctor_does_not_call_a_profiles_own_browser_a_stranger(self):
        """The false alarm the detection is worth nothing without.

        A running profile holds its port — that is what running *is* — so a doctor that
        read a held port as a stranger would fire on every profile anyone had up, every
        run, and the one row that matters would be lost in them.

        The directory is what settles it, because the directory is the identity
        `chrome.scan` groups by: crom knows what it would have launched this profile with,
        and asks the process table whether anything is there.
        """
        self._two_profiles()
        directory = str(state_home() / "profiles" / "myapp" / "alpha")

        with (
            mock.patch("crom.chrome.port_is_free", lambda port: port != 9401),
            mock.patch("crom.chrome.scan", return_value={directory: (4242,)}),
        ):
            rows = self._rows()

        self.assertEqual("own", rows["myapp/alpha"]["liveness"])

    def test_doctor_finds_the_browser_of_a_profile_its_config_no_longer_declares(self):
        """The two axes crossing, on the row where getting it wrong costs the most.

        Drop a profile from the config while its browser is up and the reservation is
        orphaned and running at once. Deriving the profile directory from what the config
        *declares* would find nothing for a key it no longer names, and report the user's
        own browser as a stranger — on the one row a reader is already being told to act
        on. So the directory is composed from the ledger key instead, through
        `model.ref_of`, and the two verdicts are decided independently.
        """
        self._two_profiles()
        (self.project / ".crom.toml").write_text(
            'namespace = "myapp"\n\n[profiles.beta]\nport = 9402\n'
        )
        directory = str(state_home() / "profiles" / "myapp" / "alpha")

        with (
            mock.patch("crom.chrome.port_is_free", lambda port: port != 9401),
            mock.patch("crom.chrome.scan", return_value={directory: (4242,)}),
        ):
            rows = self._rows()

        self.assertEqual("orphaned", rows["myapp/alpha"]["standing"])
        self.assertEqual("own", rows["myapp/alpha"]["liveness"])

    def test_doctor_will_not_name_a_holder_for_a_key_that_is_not_a_reference(self):
        """A hand-edited key on a held port, and the false `foreign` it must not print.

        Hand-editing the ledger is the only way to release an orphan today, so keys
        `ProfileRef` refuses really do reach here. Crom cannot name the directory such a
        key's browser would run in, so it cannot ask whether that browser is up — and a
        `foreign` reached by finding no browser on a directory that was never named is not
        a weaker finding, it is a wrong one. [LAW:no-silent-failure]
        """
        self._two_profiles()
        ledger = json.loads(registry_file().read_text())
        # Sourced to the config crom *can* read, so what goes unanswered is the key alone:
        # against a config it could not read the row would be unprobed for that reason
        # instead, and this would pass without `ref_of` ever being asked.
        ledger["ports"]["a/b/c"] = {
            "port": 9403,
            "pinned": False,
            "source": str(self.project / ".crom.toml"),
        }
        registry_file().write_text(json.dumps(ledger))

        with mock.patch("crom.chrome.port_is_free", lambda port: port != 9403):
            rows = self._rows()

        self.assertEqual("unprobed", rows["a/b/c"]["liveness"])
        self.assertIn("a/b/c", rows["a/b/c"]["liveness_finding"])

    def test_doctor_answers_for_a_free_port_even_when_the_key_names_no_profile(self):
        """A port nothing holds is answered, whoever the reservation turns out to be.

        The same unresolvable key as above, with nothing on its port. Refusing to answer
        here would make `unprobed` mean two different things — "a stranger might be there"
        and "nothing is there" — on the rows most likely to have been stranded by a hand
        repair, which are exactly the ports a release is going to reclaim.
        """
        self._two_profiles()
        ledger = json.loads(registry_file().read_text())
        # Sourced to the config crom *can* read, so what goes unanswered is the key alone:
        # against a config it could not read the row would be unprobed for that reason
        # instead, and this would pass without `ref_of` ever being asked.
        ledger["ports"]["a/b/c"] = {
            "port": 9403,
            "pinned": False,
            "source": str(self.project / ".crom.toml"),
        }
        registry_file().write_text(json.dumps(ledger))

        self.assertEqual("idle", self._rows()["a/b/c"]["liveness"])

    def test_doctor_will_not_name_a_holder_for_a_reservation_whose_config_is_gone(self):
        """Where this half parts company with the staging half, deliberately.

        `test_doctor_still_finds_the_leak_of_a_project_whose_config_is_gone` has the scan
        fall back to crom's default profile root and caveat it, because a staging
        directory found there really is there whatever `state_dir` that config used to
        set. This asks the opposite kind of question: it reports what it did *not* find,
        and no browser on a directory the profile may never have used is a false
        `foreign` rather than an incomplete answer. Same module, opposite treatment, and
        the difference is which way the evidence runs.
        """
        self._two_profiles()
        ledger = json.loads(registry_file().read_text())
        gone = self.root / "deleted.toml"
        ledger["ports"]["ghost/one"] = {"port": 9403, "pinned": False, "source": str(gone)}
        registry_file().write_text(json.dumps(ledger))

        with mock.patch("crom.chrome.port_is_free", lambda port: port != 9403):
            rows = self._rows()

        self.assertEqual("orphaned", rows["ghost/one"]["standing"])
        self.assertEqual("unprobed", rows["ghost/one"]["liveness"])
        self.assertIn(str(gone), rows["ghost/one"]["liveness_finding"])

    def test_doctor_reports_a_process_table_it_cannot_read_rather_than_crashing(self):
        """A doctor answers on the machine whose state is the problem, so it never raises.

        `chrome.scan` raises when `ps` is missing or exits nonzero, and this half calls it
        for every row at once. Unhandled that would end the command on precisely the
        machine it was run to diagnose, and the reservations it had already judged would
        go with it.
        """
        self._two_profiles()

        with (
            mock.patch("crom.chrome.port_is_free", lambda port: port != 9401),
            mock.patch(
                "crom.chrome.scan",
                side_effect=Reason.PROCESS_TABLE_UNREADABLE.error(
                    "could not read the process table: `ps` was not found on PATH.\n"
                    "crom answers 'is this profile running' by reading `ps`."
                ),
            ),
        ):
            rows = self._rows()

        self.assertEqual("unprobed", rows["myapp/alpha"]["liveness"])
        # The first line only, the way a config that would not load is reported: the
        # second is advice for a different command, and a row is a row.
        self.assertEqual(
            "could not read the process table: `ps` was not found on PATH.",
            rows["myapp/alpha"]["liveness_finding"],
        )

    def test_doctor_reports_a_port_it_could_not_probe_rather_than_calling_it_free(self):
        """A probe socket that cannot be made says nothing about the port.

        `chrome.port_is_free` raises rather than answering when the socket itself cannot
        be created — fd exhaustion, which is a fact about the process asking and not about
        the port. Reading that as `idle` would be an answer shaped like a fact, on the
        field a release is going to trust before it reclaims a number.
        """
        self._two_profiles()

        with mock.patch(
            "crom.chrome.port_is_free",
            side_effect=Reason.PORT_CHECK_FAILED.error(
                "could not check whether port 9401 is free: [Errno 24] Too many open files"
            ),
        ):
            rows = self._rows()

        self.assertEqual("unprobed", rows["myapp/alpha"]["liveness"])
        self.assertIn("Too many open files", rows["myapp/alpha"]["liveness_finding"])

    # --- what an interrupted seed left behind ---------------------------------------

    def _interrupted_seed(self, config: str) -> None:
        """Run `crom up` against a seed and kill it between the copy and the commit.

        SIGKILL is precisely "the `except BaseException` arm never ran", and no in-process
        test can produce that — the interpreter that would run the assertions is the one
        being killed. So it is expressed as the state that arm leaves untouched: a copy
        that fails after writing part of the profile, with `_staged`'s cleanup neutered.
        Everything that decides where the residue lands and what it is called —
        `mkdtemp(prefix=f".{destination.name}.")`, the profile root, the commit that never
        happens — is production code, which is the part detection has to agree with.
        [LAW:behavior-not-structure]
        """
        def half_copied(source, dest, **_kwargs):
            (Path(dest) / "Cookies").write_bytes(b"x" * 4096)
            raise Reason.SEED_UNREADABLE.error(f"seed {source} was interrupted")

        (self.root / "seed").mkdir(exist_ok=True)
        (self.project / ".crom.toml").write_text(config)
        with (
            mock.patch("crom.seed.shutil.copytree", side_effect=half_copied),
            mock.patch("crom.seed.shutil.rmtree"),
        ):
            # 1 is the bucket `CromError` sorts into; the exit code is not what this is
            # about, only that the run ended before the commit.
            self.crom("up", "dev", expect=1)

    def _staging(self) -> list[dict]:
        return json.loads(self.crom("doctor", "--json"))["staging"]

    def test_doctor_reports_the_staging_directory_an_interrupted_seed_left(self):
        """The leak this ticket exists for, and why nothing else can see it.

        `seed._staged` builds the profile beside its final path and moves it in only once
        it is whole, so a `crom up` that dies mid-copy leaves the half-built copy behind.
        Nothing reports it: the directory is dot-prefixed so `ls` hides it, the retry
        succeeds so no command fails, and the bytes are a whole seed's worth — two
        interrupted runs against a 234 MB seed cost 350 MB on the machine that prompted
        this epic.

        The size is asserted as the exact byte count rather than merely as truthy. A leak
        a user is deciding whether to delete is a decision about the number, so a doctor
        that reported the directory with a plausible-looking wrong size would be worse
        than one that reported no size at all.
        """
        self._interrupted_seed(
            f'namespace = "myapp"\n\n[profiles.dev]\nseed = "{self.root / "seed"}"\n'
        )

        found = self._staging()

        self.assertEqual(1, len(found), found)
        self.assertEqual("myapp", found[0]["namespace"])
        self.assertEqual(4096, found[0]["bytes"])
        self.assertTrue(Path(found[0]["path"]).name.startswith(".dev."))
        self.assertIn(Path(found[0]["path"]).name, self.crom("doctor"))

    def test_doctor_does_not_call_a_profile_lock_a_leak(self):
        """The other dot-prefixed resident of a profile root, and it is not residue.

        `locking.exclusive` keys its lock on a companion `.<name>.lock` beside the thing
        it guards, so every command that touches a profile leaves one in the very
        directory this scan reads. Detection turns on `is_dir` for exactly this reason:
        without it `crom doctor` would report a leak on every machine, on every run, for
        a file crom is supposed to leave there. [LAW:verifiable-goals]

        The committed profile directory is asserted absent from the answer too, which is
        what the leading dot is for — `model.validate_name` refuses a profile name that
        does not start alphanumeric, so no directory crom commits can be confused for one
        it abandoned.
        """
        self._interrupted_seed(
            f'namespace = "myapp"\n\n[profiles.dev]\nseed = "{self.root / "seed"}"\n'
        )
        root = Path(json.loads(self.crom("config", "dev", "--json"))["resolved"]["profile_dir"]).parent
        (root / "dev").mkdir(exist_ok=True)
        self.assertTrue((root / ".dev.lock").is_file())

        reported = {Path(item["path"]).name for item in self._staging()}

        self.assertNotIn(".dev.lock", reported)
        self.assertNotIn("dev", reported)

    def test_doctor_looks_under_the_profile_root_the_config_names(self):
        """A namespace that moves its profiles elsewhere moves the leak with them.

        `state_dir` relocates a namespace's user-data-dirs, so a scan rooted at crom's
        default would report a clean machine for the projects that set it — the silent
        half-answer this command must never give. The root is composed the way
        `resolve.resolve_spec` composes it, from the config the ledger names, so there is
        one rule about where a namespace's profiles live rather than two that can drift.
        [LAW:one-source-of-truth]
        """
        elsewhere = self.root / "elsewhere"
        self._interrupted_seed(
            f'namespace = "myapp"\nstate_dir = "{elsewhere}"\n\n'
            f'[profiles.dev]\nseed = "{self.root / "seed"}"\n'
        )

        found = self._staging()

        self.assertEqual(1, len(found), found)
        self.assertEqual(elsewhere / "myapp", Path(found[0]["path"]).parent)

    def test_doctor_still_finds_the_leak_of_a_project_whose_config_is_gone(self):
        """A deleted project is the case with the most left behind, not the least.

        Nothing will ever come back to clean this one up, so declining to look would be
        the worst place to decline. The config is what names a namespace's profile root,
        and losing it does not move the bytes: they are under crom's default root unless
        that config said otherwise, so the scan runs and reports what is genuinely there.

        Both halves are asserted because either alone is a lie. Without the finding, a
        real leak goes unnamed; without the caveat, finding none would read as proof there
        are none — see the sibling test for the arrangement that makes that difference
        observable. [LAW:no-silent-failure]
        """
        self._interrupted_seed(
            f'namespace = "myapp"\n\n[profiles.dev]\nseed = "{self.root / "seed"}"\n'
        )
        config_file = self.project / ".crom.toml"
        config_file.unlink()

        found = self._staging()

        leaked = [item for item in found if "path" in item]
        self.assertEqual(1, len(leaked), found)
        self.assertEqual(state_home() / "profiles" / "myapp", Path(leaked[0]["path"]).parent)
        self.assertEqual(4096, leaked[0]["bytes"])
        self.assertIn(f"{config_file} is gone", [item.get("error") for item in found][-1])

    def test_doctor_will_not_call_a_deleted_config_proof_of_where_its_profiles_were(self):
        """`state_dir` was a fact when the directories were made, and deleting the config
        does not move them.

        The reservation half may treat an absent config as a checked answer, because "does
        anything declare this?" is a question about the present and absence settles it.
        This question is about the past. A project that put its profiles on another volume
        and was then deleted leaves the leak exactly where it was, and crom's default root
        — the only one it can still name — has nothing in it. Reporting that empty scan
        without saying what it did not cover would be a clean bill of health issued for a
        directory nobody looked at.

        The default root is asserted empty as well as the caveat present. Without it this
        passes against a doctor that reports the caveat and also, wrongly, a finding.
        [LAW:verifiable-goals]
        """
        elsewhere = self.root / "volume"
        self._interrupted_seed(
            f'namespace = "myapp"\nstate_dir = "{elsewhere}"\n\n'
            f'[profiles.dev]\nseed = "{self.root / "seed"}"\n'
        )
        config_file = self.project / ".crom.toml"
        config_file.unlink()

        found = self._staging()

        self.assertTrue(any((elsewhere / "myapp").iterdir()))  # the leak is still there
        self.assertEqual([], [item for item in found if "path" in item])
        self.assertIn(f"{config_file} is gone", found[0]["error"])
        self.assertIn("state_dir", found[0]["error"])

    def test_doctor_will_not_read_a_profile_root_it_cannot_list_as_empty(self):
        """The one arm that turns a filesystem refusal into an answer, held open.

        A root crom cannot list yields no directories, and so does a root with nothing in
        it — the same value for two facts, if the failure is allowed to pass. This is the
        distinction the whole module is built to keep, and it is the arm a later refactor
        would collapse into the `FileNotFoundError` case without any test noticing.

        A regular file where the root belongs rather than `chmod 0`, which does not stop a
        suite running as root, or a patched `iterdir`, which would make this a statement
        about the mock. `NotADirectoryError` is an `OSError` that is not
        `FileNotFoundError`, which is exactly the branch under test, and `locking.exclusive`
        already names this state as one that occurs.
        """
        (self.project / ".crom.toml").write_text('namespace = "myapp"\n\n[profiles.dev]\n')
        self.crom("list")
        root = state_home() / "profiles"
        root.mkdir(parents=True, exist_ok=True)
        (root / "myapp").write_text("not a directory")

        found = self._staging()

        self.assertEqual([], [item for item in found if "path" in item])
        self.assertIn(f"{root / 'myapp'} could not be read", found[0]["error"])
        self.assertIn("1 namespace(s) crom could not check", self.crom("doctor"))

    def test_doctor_claims_no_uncertainty_about_where_personal_profiles_live(self):
        """The first `crom doctor` of a machine, and the row it must not print.

        What keeps that row off is `cli._bootstrap_user_config`, not anything doctor
        decides: it writes the user config from `CromGroup.command_class.invoke`, before
        the body of every command, so the survey below finds a file and reads it. The
        namespace crom is surest of is the one it has already made sure of.

        So this guards an ordering that lives in another module, and it is the only test
        that does. Move the bootstrap after the command body — or make an eager option
        skip it — and a fresh machine's first doctor starts reporting that it could not
        check where personal profiles live, on the machine least able to judge the claim.

        The `assertFalse` runs before the first invocation on purpose: it records that the
        machine really did start with no user config, which is what makes the two
        assertions after it statements about the bootstrap rather than about a file the
        harness happened to leave lying around.
        """
        self.assertFalse(user_config_file().exists())

        self.assertEqual([], self._staging())
        self.assertIn("0 namespace(s) crom could not check", self.crom("doctor"))

    def test_doctor_will_not_report_a_clean_root_for_a_config_it_could_not_read(self):
        """"Nothing is leaking" and "I could not check" are different answers.

        An unloadable config may name a `state_dir` crom never read, so there is no root
        to look under — and a scan that quietly skipped it would publish an empty
        `staging` array, which is the shape of a clean bill of health. The namespace is
        reported instead, under the `error` key `crom list` already gives a namespace it
        could not load. [LAW:no-silent-failure]

        The `user` namespace is asserted absent from that list, because it is the case a
        blanket rule gets wrong: a machine with no user config file at all has not failed
        to check anything — an absent config sets no `state_dir`, so crom's default root
        is where its profiles are, exactly as `load_user_scope` already answers.
        """
        config_file = self.project / ".crom.toml"
        config_file.write_text('namespace = "myapp"\n\n[profiles.alpha]\n')
        self.crom("list")
        config_file.write_text('namespace = "myapp"\n\n[profiles.alpha\n')

        errors = {item["namespace"]: item.get("error") for item in self._staging()}

        self.assertIn(f"{config_file} could not be checked", errors["myapp"])
        self.assertNotIn("user", errors)
        self.assertIn("1 namespace(s) crom could not check", self.crom("doctor"))

    def test_doctor_will_not_call_a_migration_staging_directory_a_seed_leak(self):
        """The one dot-prefixed directory here that must never be offered up for deletion.

        `seed._staged` is not the only writer in a profile root. `migrate._move_staged`
        stages a legacy profile as `.<name>.partial` under `default_profiles_root() /
        user`, and on a same-filesystem move it renames `old_dir` away *before* that path
        exists — so an interruption in that window leaves the `.partial` holding the only
        copy of the profile. Reported here it would appear as a seed's residue, beside a
        byte count `measure` calls what deleting it would reclaim. A reader who believed
        either would lose their cookies and logins.

        Both directories are planted, and the seed's is asserted present as well as the
        migration's absent: an exclusion that swallowed the real leak too would pass a
        test that only looked for the `.partial`. [LAW:verifiable-goals]
        """
        self.crom("list")
        root = state_home() / "profiles" / USER_NAMESPACE
        root.mkdir(parents=True, exist_ok=True)
        migrating = root / f".default{migrate.STAGING_SUFFIX}"
        leaked = root / ".default.qz93kd01"
        for directory in (migrating, leaked):
            directory.mkdir()
            (directory / "Cookies").write_bytes(b"x" * 2048)

        found = [Path(item["path"]) for item in self._staging() if "path" in item]

        self.assertEqual([leaked], found)

    @unittest.skipIf(os.geteuid() == 0, "root traverses any directory, so nothing is denied")
    def test_doctor_will_not_read_a_profile_root_it_cannot_search_as_empty(self):
        """Listing a root and classifying what came back are one read, and can fail apart.

        A root with read but no execute permission lists fine and then denies the `stat`
        behind `is_dir` on every entry it just named. On 3.12 — the floor
        `requires-python` promises — `Path.is_dir` re-raises anything outside
        `pathlib._ignore_error`, and EACCES is outside it, so with the filter sitting
        after the `try` this escaped as a traceback out of the command whose docstring
        promises it never raises for a directory it cannot read.

        The version split is why this is a test and not a note: `Path.is_dir` swallows
        every `OSError` from 3.13 on, so the bug is invisible to a contributor on a new
        interpreter and live on the one the project supports. Nothing but running it on
        3.12 would have said so.

        Skipped rather than adapted under root, which traverses regardless. That is not
        the `chmod 0` failure the sibling `NotADirectoryError` test refuses — a skip
        declines to make a claim, where that one would have passed while asserting
        nothing.
        """
        self.crom("list")
        root = state_home() / "profiles" / USER_NAMESPACE
        (root / ".default.qz93kd01").mkdir(parents=True)
        # Restored here rather than through `addCleanup`, which runs after `tearDown` has
        # already removed the sandbox — and an unsearchable directory is one `tearDown`
        # cannot remove either.
        root.chmod(0o600)
        try:
            found = self._staging()
        finally:
            root.chmod(0o700)

        self.assertEqual([], [item for item in found if "path" in item])
        self.assertIn(f"{root} could not be read", found[0]["error"])

    def test_doctor_still_scans_a_namespace_whose_mapping_crom_forgot(self):
        """The index that drives this half is lossy, and loses exactly the case it is for.

        `registry.forget_mapping` drops a namespace the moment `resolve.scope_for` meets a
        config it can no longer load, and deliberately keeps the ports — a config file
        that is missing right now is not proof the project is gone. So a project whose
        config was deleted and then referred to from elsewhere went on reporting
        `orphaned` reservations while its profile root was never looked at and never
        mentioned: silence, from the half of the command whose whole point is that a root
        crom did not check says so. [LAW:no-silent-failure]

        Driving the scan from the ledger as well as the index is what closes it, and the
        ledger is the register that cannot lose the namespace — releasing a reservation is
        a separate, deliberate act. The forget is triggered through a real cross-project
        reference rather than by calling `forget_mapping` directly, so this fails if that
        call site moves. [LAW:behavior-not-structure]
        """
        self._interrupted_seed(
            f'namespace = "myapp"\n\n[profiles.dev]\nseed = "{self.root / "seed"}"\n'
        )
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (self.project / ".crom.toml").unlink()

        self.crom("config", "myapp/dev", cwd=elsewhere, expect=3)
        self.assertEqual({}, json.loads((state_home() / "registry.json").read_text())["namespaces"])

        found = self._staging()

        leaked = [item for item in found if "path" in item]
        self.assertEqual(1, len(leaked), found)
        self.assertEqual("myapp", leaked[0]["namespace"])
        self.assertEqual(4096, leaked[0]["bytes"])

    def test_doctor_will_not_scan_a_root_the_ledger_key_never_named(self):
        """A hand-edited key that names no namespace, and the directories it aimed at.

        `model.namespace_of` takes the leading segment of a ledger key, and `_staging`
        joins it to a profile root. Two segments a hand repair can produce are not names
        at all but path syntax: `/alpha` splits to `""`, and `root / ""` is `root` — the
        *shared* profiles directory every namespace sits under — while `../alpha` splits
        to `".."` and walks out of the root entirely. Either one pointed the scan at a
        directory the key never named, and whatever was found there was published as a
        leak beside a size saying deleting it is free.

        So the decoys are planted rather than assumed absent: a clean report is what the
        bug already produced on an empty root, and only a directory sitting in the
        collapsed root can tell a scan that skipped it from a scan that never ran.
        [LAW:verifiable-goals] the real leak under `myapp` is asserted found in the same
        breath, because a `namespace_of` that answered `None` to everything would pass a
        test that only checked the decoys were spared.

        Both keys stay in the reservations half. Dropping a key from the scan is not
        dropping it from the report — `crom doctor` is the command a person runs because
        the ledger is a mess, and a stranded port it declined to mention is a port
        nothing will ever reclaim. [LAW:no-silent-failure]
        """
        config_file = self.project / ".crom.toml"
        config_file.write_text('namespace = "myapp"\n\n[profiles.alpha]\n')
        self.crom("list")
        root = state_home() / "profiles"
        (root / "myapp").mkdir(parents=True, exist_ok=True)
        leak = root / "myapp" / ".alpha.9f3c1a"
        decoys = (root / ".decoy.shared", root.parent / ".decoy.above")
        for directory in (leak, *decoys):
            directory.mkdir()
            (directory / "Cookies").write_bytes(b"x" * 4096)
        ledger = json.loads(registry_file().read_text())
        for key, port in (("/alpha", 9446), ("../alpha", 9447)):
            ledger["ports"][key] = {"port": port, "pinned": False, "source": str(config_file)}
        registry_file().write_text(json.dumps(ledger))

        found = self._staging()

        self.assertEqual([str(leak)], [item["path"] for item in found if "path" in item])
        for decoy in decoys:
            self.assertTrue(decoy.is_dir(), decoy)

        standings = self._standings()

        self.assertEqual("orphaned", standings["/alpha"])
        self.assertEqual("orphaned", standings["../alpha"])

    def test_doctor_counts_the_namespaces_it_could_not_check_not_the_reasons(self):
        """One namespace, two true reasons, and the sentence that has to add them up.

        A config that is gone gets both answers — its default root is scanned, and the
        caveat beside it says that root is where the profiles are only if that config
        never set a `state_dir`. When the scan *also* fails, both halves speak: `_leaks`
        returns its own `Unscanned` for the root it could not read, and the `_Gone` arm
        appends the caveat on top. Two entries, and they say different things, so neither
        may be dropped or fused into the other. [LAW:no-silent-failure]

        What was wrong is the sentence counting them: `len(unscanned)` counts entries
        under a line that says `namespace(s)`, so one namespace reported as two. The
        entries are the territory and the sentence is a map of it, and the map is what
        had to change. Asserting both — two errors, one namespace, and the line saying
        one — is what keeps a later fix from making the count true by deleting a reason.

        A regular file where the root belongs rather than `chmod 0`, which does not stop
        a suite running as root: `NotADirectoryError` is an `OSError` that is not
        `FileNotFoundError`, which is the arm this needs.
        """
        config_file = self.project / ".crom.toml"
        config_file.write_text('namespace = "myapp"\n\n[profiles.dev]\n')
        self.crom("list")
        config_file.unlink()
        root = state_home() / "profiles"
        root.mkdir(parents=True, exist_ok=True)
        (root / "myapp").write_text("not a directory")

        found = self._staging()

        self.assertEqual({"myapp"}, {item["namespace"] for item in found})
        errors = [item["error"] for item in found]
        self.assertEqual(2, len(errors), found)
        self.assertIn(f"{root / 'myapp'} could not be read", errors[0])
        self.assertIn(f"{config_file} is gone", errors[1])
        self.assertIn("1 namespace(s) crom could not check", self.crom("doctor"))

    # --- every leak at once -----------------------------------------------------------

    @contextlib.contextmanager
    def _a_machine_carrying_every_leak(self):
        """One machine with all four detections firing, which nothing else here builds.

        Every test above breaks one thing and asserts crom sees it, and no number of those
        can catch a detection that *shadows* another. `doctor.survey` leaves room for it:
        one `consulted` dict answers both halves of the survey for every namespace, and
        the process table is read once for every row — so a config that will not parse in
        one namespace has to leave the next namespace's answer untouched, and a machine
        with a single fault cannot say whether it does. [LAW:verifiable-goals]

        Four namespaces, arranged so all three standings and all four livenesses appear at
        once and each is somebody's only claim:

        - `myapp` — `dev` is declared with its own browser up, and an interrupted seed left
          a staging directory under its root; `alpha` was dropped from the config while a
          stranger took its port.
        - `broken` — reserved a port and then stopped parsing, so crom refuses to guess
          about the reservation and about the profile root alike.
        - `gone` — config deleted and its profile root replaced by a regular file. The one
          namespace here that yields two staging findings, which is what a reader keyed by
          namespace would silently drop.
        - `user` — untouched, so there is a clean row in the answer to lose.

        Plus `../alpha`, a ledger key naming no namespace — hand-editing this file is how
        an orphan was released before `crom release` existed, so the machines that need
        this command are the machines that have been hand-edited. It is here for both
        halves at once: the row has to survive into the report, and the scan has to
        decline to walk out of the profiles root after it.
        """
        dev_only = f'namespace = "myapp"\n\n[profiles.dev]\nport = 9401\nseed = "{self.root / "seed"}"\n'
        self._interrupted_seed(dev_only + "[profiles.alpha]\nport = 9402\n")
        self.crom("list")  # reserves alpha; dev's port was taken by the seeded run
        (self.project / ".crom.toml").write_text(dev_only)  # and now nothing declares alpha

        broken = self.root / "brokenproj"
        broken.mkdir()
        (broken / ".crom.toml").write_text('namespace = "broken"\n\n[profiles.x]\nport = 9403\n')
        self.crom("list", cwd=broken)
        (broken / ".crom.toml").write_text('namespace = "broken"\n\n[profiles.x\n')

        gone = self.root / "goneproj"
        gone.mkdir()
        (gone / ".crom.toml").write_text('namespace = "gone"\n\n[profiles.y]\nport = 9404\n')
        self.crom("list", cwd=gone)
        (gone / ".crom.toml").unlink()
        # A regular file where the root belongs, rather than `chmod 0`, which does not stop
        # a suite running as root: `NotADirectoryError` is an `OSError` that is not
        # `FileNotFoundError`, which is the arm this needs.
        (state_home() / "profiles").mkdir(parents=True, exist_ok=True)
        (state_home() / "profiles" / "gone").write_text("not a directory")

        ledger = json.loads(registry_file().read_text())
        ledger["ports"]["../alpha"] = {
            "port": 9405,
            "pinned": False,
            "source": str(self.project / ".crom.toml"),
        }
        registry_file().write_text(json.dumps(ledger))

        def port_is_free(port: int) -> bool:
            """Who is on each port: nobody, somebody, or a question that would not answer.

            This settles `idle` and `unprobed` outright and leaves the two held ports for
            the process table to tell apart — 9401 is `dev`'s own browser and 9402 is a
            stranger on `alpha`'s number, which is the pair a survey reading one probe for
            the whole machine would collapse.
            """
            if port == 9403:
                raise Reason.PORT_CHECK_FAILED.error(
                    "could not check whether port 9403 is free: [Errno 24] Too many open files"
                )
            return port not in (9401, 9402)

        with (
            mock.patch("crom.chrome.port_is_free", port_is_free),
            mock.patch(
                "crom.chrome.scan",
                return_value={str(state_home() / "profiles" / "myapp" / "dev"): (4242,)},
            ),
        ):
            yield

    def test_doctor_reports_every_leak_on_a_machine_that_has_them_all(self):
        """Every standing and every liveness in one run, asserted as the whole answer.

        The comparison is an equality over all six rows rather than a lookup per row, and
        that is the point of it: a detection that dropped a row, doubled one, or answered
        for a neighbour would pass every `assertIn` and fail here. The leaf tests each
        prove a verdict is reachable; this proves the six of them coexist.
        [LAW:verifiable-goals]

        Both axes on every row, because their independence is what regresses first. Three
        rows here are orphaned and no two of them agree about who holds the port, and
        `myapp/alpha` is orphaned *and* stolen — the row a reader is being told to act on,
        and the one a survey that folded the axes together could not describe.
        """
        with self._a_machine_carrying_every_leak():
            rows = self._rows()

        self.assertEqual(
            {
                "user/default": ("declared", "idle"),
                "myapp/dev": ("declared", "own"),
                "myapp/alpha": ("orphaned", "foreign"),
                "broken/x": ("unchecked", "unprobed"),
                "gone/y": ("orphaned", "idle"),
                "../alpha": ("orphaned", "idle"),
            },
            {ref: (row["standing"], row["liveness"]) for ref, row in rows.items()},
        )

    def test_doctor_keeps_both_staging_findings_of_the_namespace_with_two(self):
        """The array where one namespace speaks twice, and the reader that would halve it.

        `gone` has a config that was deleted and a profile root that is a regular file, so
        both arms answer: the scan reports the root it could not read, and the `_Gone` arm
        appends the caveat that a `state_dir` that config declared would have put the
        profiles somewhere crom can no longer name. Two entries saying different things,
        and any helper shaped like `{item["namespace"]: item}` keeps whichever came last.
        So this asserts the whole array as a list. [LAW:no-silent-failure]

        The array is asserted whole rather than searched, which is what also holds the
        scan open to the end. `broken` is the first namespace it walks and the first it
        cannot read, so a scan that gave up there would lose `myapp`'s real leak and both
        of `gone`'s findings behind it — and every one of those is a leak a reader is here
        to be shown.
        """
        with self._a_machine_carrying_every_leak():
            found = self._staging()

        self.assertEqual(["myapp", "broken", "gone", "gone"], [item["namespace"] for item in found])
        self.assertEqual(4096, found[0]["bytes"])
        self.assertTrue(Path(found[0]["path"]).name.startswith(".dev."))
        errors = [item["error"] for item in found if item["namespace"] == "gone"]
        self.assertIn(f"{state_home() / 'profiles' / 'gone'} could not be read", errors[0])
        self.assertIn(f"{self.root / 'goneproj' / '.crom.toml'} is gone", errors[1])

    def test_doctor_counts_each_kind_of_leak_over_the_collection_it_is_about(self):
        """Three counts that only diverge on a machine leaking in more than one way.

        Every machine above leaks one way, so its counts nearly coincide and a line
        totalling the wrong collection reads correctly on all of them. Here they separate:
        six reservations, one staging directory, and a `staging` array of four — so a
        summary counting that array would announce four abandoned profile copies, three of
        which are namespaces crom could not look at. Those namespaces are two, not the
        three entries they wrote. [FRAMING:representation] the entries are the territory;
        these lines are a map of it, and this is the machine where a wrong map shows.
        """
        with self._a_machine_carrying_every_leak():
            report = self.crom("doctor")

        self.assertIn(f"6 reservation(s) in {registry_file()}", report)
        self.assertIn("1 staging directory(s) from a seed", report)
        self.assertIn("2 namespace(s) crom could not check", report)

    # --- handing leaked state back ---------------------------------------------------

    def _orphan_beta(self) -> None:
        """Two reservations, then a config that declares only the first.

        The shape of the leak the epic reproduced: an edit removes a stanza and the ledger
        keeps the port forever. `beta` is the orphan, `alpha` is the neighbour every test
        here checks is still standing afterwards.
        """
        self._two_profiles()
        (self.project / ".crom.toml").write_text(
            'namespace = "myapp"\n\n[profiles.alpha]\nport = 9401\n'
        )

    def test_release_reclaims_one_orphaned_reservation_and_leaves_its_namespace_alone(self):
        """The whole ticket: one leak reclaimed, its neighbours untouched.

        `crom forget myapp` is the only other way to release a reserved port and it
        releases every port under the namespace — `alpha`'s included — so it cannot reach
        one orphan without taking the live profiles with it. `alpha` is asserted as hard as
        `beta` for exactly that reason: without it this passes against a release that is
        `crom forget` spelled differently, which is the one thing it must not be.
        [LAW:verifiable-goals]
        """
        self._orphan_beta()

        self.crom("release", "myapp/beta")

        rows = self._rows()
        self.assertNotIn("myapp/beta", rows)
        self.assertEqual(9401, rows["myapp/alpha"]["port"])
        self.assertEqual("declared", rows["myapp/alpha"]["standing"])

    def test_release_hands_the_number_back_to_the_profile_that_asks_next(self):
        """Released has to mean *reusable*, which no reading of the ledger can establish.

        The leak costs a port, so the repair is worth nothing until the allocator can hand
        that port out again — and `registry._allocate` steps over every number the ledger
        holds. A release that removed the row but left the number unreachable would satisfy
        every assertion in the test above.

        Ports are left unpinned here, where the rest of this section pins them, because a
        pin is the one thing that would let a new profile land on the number without the
        allocator ever having been free to choose it. The number is read back from the row
        rather than written as a literal: what is being asserted is that this number came
        back, not that crom numbers profiles in any particular way.
        """
        (self.project / ".crom.toml").write_text(
            'namespace = "myapp"\n\n[profiles.alpha]\n[profiles.beta]\n'
        )
        self.crom("list")
        released = self._rows()["myapp/beta"]["port"]
        (self.project / ".crom.toml").write_text('namespace = "myapp"\n\n[profiles.alpha]\n')

        self.crom("release", "myapp/beta")
        self.crom("add", "gamma")

        self.assertEqual(released, json.loads(self.crom("config", "gamma", "--json"))["resolved"]["port"])

    def test_release_reaches_a_ledger_key_no_profile_reference_could_name(self):
        """The keys a hand repair strands, which are the ones nothing else can reach.

        Hand-editing the ledger was the only way to release a single reservation before
        this command, so keys `ProfileRef` refuses — a second slash, a capital, a space —
        really are in there, and `registry._read` validates every entry and no key. A
        release that took a `ProfileRef` would be unable to name exactly the reservations
        its own predecessor left behind. So the argument is the raw ledger key, and this
        is what fails the moment anyone narrows it back. [LAW:types-are-the-program]
        """
        self._orphan_beta()
        ledger = json.loads(registry_file().read_text())
        for key, port in (("a/b/c", 9444), ("Not A Ref!", 9445)):
            ledger["ports"][key] = {"port": port, "pinned": False, "source": str(self.project / ".crom.toml")}
        registry_file().write_text(json.dumps(ledger))

        self.crom("release", "a/b/c")
        self.crom("release", "Not A Ref!")

        remaining = self._rows()
        self.assertNotIn("a/b/c", remaining)
        self.assertNotIn("Not A Ref!", remaining)
        self.assertEqual(9401, remaining["myapp/alpha"]["port"])

    def test_release_refuses_a_reservation_a_config_still_declares(self):
        """The port a live profile is still being handed, and it must not move.

        `crom port`, `crom env` and `crom mcp` go on giving `alpha` this number, so
        releasing it hands the same number to the next profile that asks for one. The
        reservation is asserted to survive rather than only the exit code: a refusal that
        printed and released anyway would pass on the code alone.
        """
        self._orphan_beta()

        failure = self.failure("release", "myapp/alpha")

        self.assertEqual(Reason.RESERVATION_DECLARED, failure.reason)
        self.assertIn("crom rm myapp/alpha", str(failure))
        self.assertEqual(9401, self._rows()["myapp/alpha"]["port"])

    def test_release_refuses_a_reservation_whose_config_crom_could_not_read(self):
        """`unchecked` is never a quieter `orphaned`, and this is where that costs money.

        A config that will not parse may declare this profile perfectly well, so
        'nothing declares it' was never established. A released port never comes back —
        every checked-in `.mcp.json` and `CDP_URL` pointing at the number breaks with it —
        which is why the one verdict crom refuses to guess at is also the one a release
        must refuse to act on. [LAW:no-silent-failure]
        """
        self._two_profiles()
        (self.project / ".crom.toml").write_text("namespace = \"myapp\"\n\n[profiles.alpha\n")

        failure = self.failure("release", "myapp/beta")

        self.assertEqual(Reason.RESERVATION_UNSETTLED, failure.reason)
        self.assertEqual("unchecked", self._rows()["myapp/beta"]["standing"])

    def test_release_refuses_a_reservation_whose_port_crom_could_not_probe(self):
        """The second axis's own version of the refusal above, and it earns its own arm.

        Standing and liveness are independent, so a reservation can be squarely orphaned —
        crom read the config, nothing declares it — while crom still has no idea who is on
        the number. `unprobed` means the probe never answered, and reading it as `idle` is
        acting on evidence crom never got, on the one field a release trusts before it
        hands a number back.
        """
        self._orphan_beta()

        with mock.patch(
            "crom.chrome.port_is_free",
            side_effect=Reason.PORT_CHECK_FAILED.error(
                "could not check whether port 9402 is free: [Errno 24] Too many open files"
            ),
        ):
            failure = self.failure("release", "myapp/beta")

        self.assertEqual(Reason.RESERVATION_UNSETTLED, failure.reason)
        self.assertIn("Too many open files", str(failure))
        self.assertIn("myapp/beta", self._rows())

    def test_release_refuses_a_reservation_whose_own_browser_is_still_running(self):
        """The declaration is gone and the browser is not, which is the deliberate refusal.

        Crom knows exactly what is happening here — unlike the two refusals above, this is
        not an evidence gap — and refuses anyway. A profile whose stanza vanished while its
        own Chrome kept running is far more often a config mid-edit or a checkout in flight
        than a profile someone meant to delete, and the two are indistinguishable from the
        ledger while the cost of being wrong runs one way only.

        `registry._allocate` binds before it hands a port out, so it would step over this
        number anyway — but that is the allocator protecting itself, and it stops the
        moment the browser exits. The refusal is this command being careful, which is a
        different thing and is why it is asserted here.
        """
        self._orphan_beta()
        directory = str(state_home() / "profiles" / "myapp" / "beta")

        with (
            mock.patch("crom.chrome.port_is_free", lambda port: port != 9402),
            mock.patch("crom.chrome.scan", return_value={directory: (4242,)}),
        ):
            failure = self.failure("release", "myapp/beta")

        self.assertEqual(Reason.RESERVATION_IN_USE, failure.reason)
        self.assertIn("myapp/beta", self._rows())

    def test_release_reclaims_a_reservation_a_stranger_has_taken_over(self):
        """The epic's motivating leak, and what keeps the refusal above about `own`.

        Identical setup to the test above but for the one fact that decides it: the port is
        held and the holder is not this profile's browser. That is `foreign`, the loudest
        thing `crom doctor` says, and it is precisely when a user most wants the number
        back — crom's promise that the port never moves is already broken.

        Paired deliberately. Without this, a release that refused every held port would
        pass the test above while making the command useless on the case the epic was
        opened for. [LAW:verifiable-goals]
        """
        self._orphan_beta()

        with mock.patch("crom.chrome.port_is_free", lambda port: port != 9402):
            self.assertEqual("foreign", self._rows()["myapp/beta"]["liveness"])
            self.crom("release", "myapp/beta")

        self.assertNotIn("myapp/beta", self._rows())

    def test_release_says_the_ledger_holds_no_such_key_rather_than_reporting_success(self):
        """A typo has to land somewhere a script can see, and exit 3 is where.

        A release that quietly succeeded on a key it never found would tell a repair script
        the leak was gone every time it misspelled one — an answer-shaped void on the exact
        command a machine reaches for when its state is already wrong.
        [LAW:parse-dont-validate]
        """
        self._orphan_beta()

        failure = self.failure("release", "myapp/no-such-profile")

        self.assertEqual(Reason.RESERVATION_UNKNOWN, failure.reason)
        self.assertIn("crom doctor", str(failure))

    def test_release_credits_a_concurrent_release_rather_than_claiming_the_port_itself(self):
        """The window between deciding and writing, and the only lie available inside it.

        `crom release` decides from a survey and writes under `registry.forget`'s own lock,
        so another release can empty the key in between. A command that printed "Released"
        there would be crediting crom with freeing a number it found already free — the
        quietest possible lie about crom's own state, and the whole reason `forget` reports
        whether an entry was actually removed. Nothing else reads that boolean.
        [LAW:no-silent-failure]

        The rival release is the real one, run from inside the survey the command is about
        to decide on, rather than a ledger fabricated to look raced: what is asserted is
        that the two orderings tell the user apart, not that a particular stub was reached.
        [LAW:behavior-not-structure]

        This exits 0, and that is the answer rather than a failure — the caller asked for
        the port to be unreserved and it is.
        """
        self._orphan_beta()
        survey = doctor.survey

        def lose_the_race():
            found = survey()
            registry.forget("myapp/beta")
            return found

        with mock.patch("crom.doctor.survey", lose_the_race):
            output = self.crom("release", "myapp/beta")

        self.assertIn("Already released myapp/beta", output)
        self.assertNotIn("myapp/beta", self._rows())

    def test_clean_deletes_the_staging_directory_a_seed_abandoned(self):
        """The other half of the leak, and the bytes are the point.

        Two interrupted runs against a 234 MB seed cost 350 MB on the machine that prompted
        this epic, hidden behind a leading dot with every retry succeeding. The directory is
        asserted gone from the filesystem and not merely absent from the next report: a
        `doctor` that stopped listing it would satisfy the second on its own.
        """
        self._interrupted_seed(
            f'namespace = "myapp"\n\n[profiles.dev]\nseed = "{self.root / "seed"}"\n'
        )
        (leaked,) = (Path(item["path"]) for item in self._staging())
        self.assertTrue(leaked.is_dir())

        self.crom("clean", str(leaked), "--yes")

        self.assertFalse(leaked.exists())
        self.assertEqual([], self._staging())

    def test_clean_asks_before_deleting_and_a_silent_answer_is_no(self):
        """The bytes are unrecoverable, so the default answer has to be the safe one.

        `crom rm` prompts before deleting a profile's data for the same reason, and this
        deletes the same kind of thing. With nothing on stdin `click.confirm` aborts, which
        is what a script that piped nothing in gets — so the directory has to survive that,
        not merely survive a typed `n`.
        """
        self._interrupted_seed(
            f'namespace = "myapp"\n\n[profiles.dev]\nseed = "{self.root / "seed"}"\n'
        )
        (leaked,) = (Path(item["path"]) for item in self._staging())

        self.invoke("clean", str(leaked), expect=1)

        self.assertTrue(leaked.is_dir())

    def test_clean_will_not_delete_a_migrations_staging_copy(self):
        """The one directory here that is sometimes the only copy of someone's profile.

        `migrate._move_staged` stages a legacy profile as `.<name>.partial` in this very
        root and, on a same-filesystem move, renames the original away *before* the copy
        exists — so for part of that run the `.partial` holds the profile's only cookies
        and logins. It is dot-prefixed and it is a directory, which is every test a
        seed's residue passes.

        `crom doctor` already refuses to call it a leak, and `clean` deletes only what the
        survey reported rather than re-deriving what a leak is. That is what this holds
        open: a second reader of the rule would be free to drift from the report it acts
        on. [LAW:single-enforcer]
        """
        self._interrupted_seed(
            f'namespace = "myapp"\n\n[profiles.dev]\nseed = "{self.root / "seed"}"\n'
        )
        (leaked,) = (Path(item["path"]) for item in self._staging())
        migrating = leaked.parent / f".legacy{migrate.STAGING_SUFFIX}"
        migrating.mkdir()

        failure = self.failure("clean", str(migrating), "--yes")

        self.assertEqual(Reason.STAGING_UNKNOWN, failure.reason)
        self.assertTrue(migrating.is_dir())

    def test_clean_takes_a_path_spelled_however_a_shell_produced_it(self):
        """Tab completion gives a relative path; `crom doctor` prints an absolute one.

        Both name the same directory, so both have to reach it — a repair command that
        only accepted one spelling would refuse the completion the user's own shell just
        offered. Selection is by resolved path, while what gets deleted is always the path
        `doctor` walked to, so a looser spelling can pick a finding and can never widen
        one. [LAW:parse-dont-validate]
        """
        self._interrupted_seed(
            f'namespace = "myapp"\n\n[profiles.dev]\nseed = "{self.root / "seed"}"\n'
        )
        (leaked,) = (Path(item["path"]) for item in self._staging())

        self.crom("clean", f"./{leaked.name}", "--yes", cwd=leaked.parent)

        self.assertFalse(leaked.exists())

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
            mock.patch(
                "crom.cli.chrome.launch",
                side_effect=Reason.CHROME_STARTUP_FAILED.error("Chrome exited 1"),
            ),
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
            mock.patch(
                "crom.cli.window.raise_profile",
                side_effect=Reason.AUTOMATION_DENIED.error("no Automation access"),
            ),
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

    def test_a_size_counts_only_what_deleting_the_directory_reclaims(self):
        """`doctor.measure` answers what removing a directory frees, so a symlink's
        target — which is not deleted — must not be counted, and a dangling link must not
        raise: the two callers are a prompt shown before a destructive act and a leak
        report, and neither is improved by refusing to say a number.

        Asserted through both halves of the answer, because they are used together and
        separately: `rm`'s prompt renders this count, and `crom doctor` publishes it raw
        as `bytes` for a script deciding what a leak is costing."""
        directory = self.root / "profile"
        (directory / "sub").mkdir(parents=True)
        (directory / "sub" / "real.bin").write_bytes(b"x" * 2048)

        outside = self.root / "huge.bin"
        outside.write_bytes(b"y" * 100_000)
        (directory / "link-to-huge").symlink_to(outside)
        (directory / "dangling").symlink_to(self.root / "gone")

        total = doctor.measure(directory)

        self.assertEqual(total, 2048)  # neither link counted
        self.assertEqual(cli._human_size(total), "2KB")

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
            error = self.failure("rm", "ci", "--yes")

        # `rm` carries no `--json`, so this slug reaches no envelope and the rendered
        # sentence was all the suite could see. `profile_dir_undeletable` is what makes
        # the retry the message promises a fact a script can act on rather than prose.
        self.assertIs(error.reason, Reason.PROFILE_DIR_UNDELETABLE)
        self.assertIn("still declared", str(error))
        self.assertIn("[profiles.ci]", (self.project / ".crom.toml").read_text())
        # Still nameable, on its original port — which is what makes the retry real.
        self.assertEqual(json.loads(self.crom("config", "ci", "--json"))["resolved"], before)

    def test_a_profile_removed_mid_declaration_is_named_rather_than_left_a_key_error(self):
        """`add_cmd` reads the file back after `ensure_profile` wrote it, so a concurrent
        `crom rm` — or a `git checkout` over the file — landing between the two arrives
        here. Left to the dict lookup it was a `KeyError`, which would leave this module's
        exit-code contract as a traceback. [LAW:no-silent-failure]

        The read-back is mocked because the race cannot be scheduled: what is under test
        is what crom says when it loses it, not how narrow the window is.
        """
        self.crom("init")
        real = config.load_file

        def as_if_removed(target, **kwargs):
            return replace(real(target, **kwargs), profiles={})

        with mock.patch.object(config, "load_file", side_effect=as_if_removed):
            error = self.failure("add", "ci")

        self.assertIs(error.reason, Reason.PROFILE_VANISHED)
        self.assertIn("was removed while crom was declaring it", str(error))

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

        The `--json` envelope inherits the carve-out rather than reopening it: a reader
        that has already gone is the one failure with nowhere to put a document, so both
        streams stay empty even when a caller asked for JSON.
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

        # The same two errno values decide the envelope, and asserting both is what says
        # the carve-out is keyed on `EPIPE` rather than on `BrokenPipeError`: one class,
        # two answers. A reader that left gets silence on both streams; a socket crom
        # really failed to write is an ordinary failure and gets its document.
        with mock.patch(
            "crom.chrome.scan", side_effect=BrokenPipeError(errno.EPIPE, "Broken pipe")
        ):
            gone = self.invoke("list", "--json", expect=1)
        self.assertEqual((gone.stdout, gone.stderr), ("", ""))

        with mock.patch(
            "crom.chrome.scan", side_effect=BrokenPipeError(errno.ESHUTDOWN, "Cannot send")
        ):
            refused = self.invoke("list", "--json", expect=1)
        self.assertEqual(
            json.loads(refused.stdout)["error"],
            # `ESHUTDOWN` and not a slug of crom's own devising: the OS published this
            # name, so the envelope repeats it rather than inventing a second spelling
            # for a fact crom does not own. [LAW:one-source-of-truth]
            # `path` is null because this refusal names no file: the `BrokenPipeError` is
            # about a reader that left, not about anything on disk.
            {
                "code": 1,
                "kind": "os_error",
                "reason": "ESHUTDOWN",
                "fields": {"path": None},
                "message": "Cannot send",
            },
        )

    @contextlib.contextmanager
    def _two_profiles_pinning_one_port(self):
        """A config `list` refuses to load, restored afterwards so one case cannot
        decide what the next one sees."""
        path = self.project / ".crom.toml"
        original = path.read_text()
        path.write_text(original + "\n[profiles.ci]\nport = 9401\n")
        try:
            yield
        finally:
            path.write_text(original)

    def test_a_json_caller_gets_the_failure_as_data(self):
        """A caller that asked for JSON gets a document on stdout for every exit code.

        Each refusal is run twice — once plain, once with `--json` — because the promise
        is that the flag *adds* the machine's copy rather than trading the human's away.
        Comparing the two runs is what makes that checkable: stderr has to come back
        byte-identical, so no script reading crom's messages today is disturbed, and plain
        stdout has to stay empty, because `crom list > out` already assumes it.

        The message is asserted to be the stderr sentence itself, not merely a non-empty
        string: it is one string rendered to two streams, and a test that accepted any
        text would pass against two wordings that had drifted apart.
        [LAW:one-source-of-truth]

        `fields` is checked by name and not by value, which is the claim that belongs
        here: whatever the failure was, the envelope carries the data crom looked up
        under names decided by the reason. What is actually in them is pinned at the
        raise sites, where the values come from.
        """
        self.crom("init")
        self.crom("add", "dev", "--port", "9401")

        refusals = (
            # 3: a namespace nothing declares.
            (("up", "nosuchns/x"), contextlib.nullcontext(), 3, "not_found", ("known",)),
            # 1: the operating system refusing the process table.
            (
                ("list",),
                mock.patch(
                    "crom.chrome.scan",
                    side_effect=PermissionError(13, "Permission denied", "/bin/ps"),
                ),
                1,
                "os_error",
                ("path",),
            ),
            # 4: two profiles pinning one port, which `list` meets while loading.
            (("list",), self._two_profiles_pinning_one_port(), 4, "conflict", ("port",)),
        )

        for args, refusal, code, kind, fields in refusals:
            with self.subTest(command=args[0], code=code):
                with refusal:
                    plain = self.invoke(*args, expect=code)
                    asked = self.invoke(*args, "--json", expect=code)

                self.assertEqual(plain.stdout, "")
                self.assertEqual(asked.stderr, plain.stderr)

                error = json.loads(asked.stdout)["error"]
                self.assertEqual(error["code"], code)
                self.assertEqual(error["kind"], kind)
                self.assertEqual(tuple(error["fields"]), fields)
                self.assertEqual(f"Error: {error['message']}", plain.stderr.strip())

    def test_the_kind_says_what_the_exit_code_cannot(self):
        """Both of these are exit 1, and they are not the same failure.

        crom refusing and the OS refusing share a code, because the published vocabulary
        has four values and cannot grow without breaking the contract a script branches
        on. So `kind` is the field that separates them — one means the request was wrong,
        the other means the machine got in the way, and only the second is worth a retry.

        This is also what stops `kind` from being `code` spelled a second time: derive one
        from the other and this test fails, because there is no function from 1 to both
        answers. [LAW:one-source-of-truth]
        """
        self.crom("init")

        kinds = []
        for error in (
            PermissionError(13, "Permission denied", "/bin/ps"),
            Reason.PROCESS_TABLE_UNREADABLE.error("ps is not on PATH"),
        ):
            with mock.patch("crom.chrome.scan", side_effect=error):
                result = self.invoke("list", "--json", expect=1)
            kinds.append(json.loads(result.stdout)["error"]["kind"])

        self.assertEqual(kinds, ["os_error", "failure"])

    def test_the_reason_says_what_the_kind_cannot(self):
        """Three failures, one exit code, one kind — and three different next moves.

        This is the case the epic opened on. Exit 1 and `kind: failure` cover a port held
        by a stranger, a Chrome that will not run, and a Chrome that started and died,
        and a script that wants to retry, or to tell its user to install a browser, or to
        go read a log, cannot get to any of those from either field. The reason is what
        separates them — which is also what stops it from being `kind` spelled finer:
        derive one from the other and this test collapses to a single key.
        """
        self.crom("init")

        seen = {}
        for reason in (Reason.PORT_IN_USE, Reason.CHROME_UNUSABLE, Reason.CHROME_STARTUP_FAILED):
            with mock.patch("crom.chrome.scan", side_effect=reason.error("refused")):
                result = self.invoke("list", "--json", expect=1)
            error = json.loads(result.stdout)["error"]
            seen[error["reason"]] = (error["code"], error["kind"])

        self.assertEqual(
            seen,
            {
                "port_in_use": (1, "failure"),
                "chrome_unusable": (1, "failure"),
                "chrome_startup_failed": (1, "failure"),
            },
        )

    def test_the_namespaces_that_do_exist_reach_a_script_as_a_list(self):
        """The case this ticket opened on: crom already knew the answer and spent it on a
        sentence.

        `unknown namespace 'x'. Known namespaces: myproj, user` is crom reading its own
        registry on the caller's behalf and then handing back the result as English. A
        script that wants to offer the real choices — or pick one — has to match a prose
        prefix and split on a comma, which is a parser for crom's wording rather than for
        crom's data, and it breaks the day the wording improves.

        The list is asserted against the sentence as well as against its value, because
        the two are one list rendered twice: the field is what the message was joined
        from, not a second lookup that could come to disagree with it.
        [LAW:one-source-of-truth]
        """
        self.crom("init")

        error = json.loads(self.invoke("up", "nosuchns/x", "--json", expect=3).stdout)["error"]

        self.assertEqual(error["fields"], {"known": ["myproj", "user"]})
        self.assertTrue(
            error["message"].endswith("Known namespaces: myproj, user"), error["message"]
        )

    def test_a_profile_nothing_declares_names_the_file_and_what_is_in_it(self):
        """The other enumeration, and the one whose next move is a file edit.

        Both facts are lookups: which config governs this directory is the result of
        walking up from the working directory, and what it declares is the result of
        parsing it. Neither is anywhere in the argument the caller typed.

        `down` rather than `up`, because `up` declares a missing profile instead of
        refusing it — this reason belongs to the commands that must not create what they
        were asked to converge away from.
        """
        self.crom("init")
        self.crom("add", "dev", "--port", "9401")

        error = json.loads(self.invoke("down", "nope", "--json", expect=3).stdout)["error"]

        self.assertEqual(
            error["fields"],
            # `default` as well as `dev`: `init` declares one, and the field is the file's
            # whole list rather than the profiles this test happened to add.
            {"source": str(self.project / ".crom.toml"), "declared": ["default", "dev"]},
        )

    def test_an_os_refusal_names_the_file_it_was_refused(self):
        """The OS arm's own field, and the one place the boundary fills a payload itself.

        This arm's message is `<path>: <reason>` joined at the boundary, so without the
        field a caller would be splitting crom's sentence on a colon to learn which file
        the OS would not open — and paths contain colons. The parts arrive already
        separate; flattening them and asking the reader to undo it is the whole failure
        this ticket exists to fix, one arm over from crom's own refusals.

        Both spellings of `filename`, because the stdlib uses both: most calls hand back
        the string they were given, and `shutil.rmtree(Path(...))` hands back the
        `PosixPath`. Measured, not assumed. JSON has one of those types, so the boundary
        renders — which is its job, and not one a raise site could have done here, since
        the OS built this failure and there is no raise site.
        """
        self.crom("init")

        for named in ("/bin/ps", Path("/bin/ps")):
            with self.subTest(filename=type(named).__name__):
                with mock.patch(
                    "crom.chrome.scan",
                    side_effect=PermissionError(13, "Permission denied", named),
                ):
                    result = self.invoke("list", "--json", expect=1)
                error = json.loads(result.stdout)["error"]
                self.assertEqual(error["fields"], {"path": "/bin/ps"})

    def test_a_refusal_the_os_did_not_number_answers_with_no_reason_at_all(self):
        """The one failure that reaches the envelope with nothing to put in `reason`.

        `_errno_detail` looks the slug up in the OS's own errno table rather than
        inventing a parallel spelling beside it, and not every `OSError` carries a number
        to look up — `shutil.Error` aggregates per-file failures and carries none.
        Documented, and until this test never once executed.

        `null` is the honest answer and the only one that cannot be mistaken for a slug:
        a stand-in like `"unknown"` would be an answer-shaped void, a value shaped like an
        identification that a script could branch on as though crom had made one.
        [LAW:parse-dont-validate]

        `kind` still says `os_error`, which is the whole point of the two fields being
        separate — crom knows the machine refused even when it cannot say how.
        """
        self.crom("init")

        with mock.patch("crom.chrome.scan", side_effect=shutil.Error("gave up partway")):
            result = self.invoke("list", "--json", expect=1)

        error = json.loads(result.stdout)["error"]
        # Subscripted rather than `.get`: present-and-null is the contract, so a key that
        # went missing must fail here instead of reading as the null it should have been.
        self.assertIsNone(error["reason"])
        self.assertEqual(error["kind"], "os_error")
        # An `OSError` carrying neither `filename` nor `strerror` still owes the user a
        # sentence; the halves the boundary drops must not take the message with them.
        # [LAW:no-silent-failure]
        self.assertIn("gave up partway", error["message"])

    def test_a_restated_declaration_answers_one_slug_for_both_of_its_callers(self):
        """`crom add` comparing a profile's declaration and `crom init` comparing the
        project's own namespace both raise from `_reject_restatement`, and neither
        command carries `--json` — so this is the only seam where the reason can be read
        at all, which is why the assertion is here rather than on an envelope.

        One slug and not two. A slug earns its place by separating next moves, and both
        callers say the same thing to whoever receives it: the file declares something
        else, so edit it or change the request. A second name no script would branch on
        differently would be a mode, not a distinction. [LAW:no-mode-explosion] The name
        is `declaration_differs` rather than `profile_differs` because `crom init` names
        no profile, and a slug that says otherwise sends a reader to the wrong stanza.
        """
        with self.assertRaises(Conflict) as caught:
            cli._reject_restatement(
                "already configures this project, and this asks to change it:",
                # A second fact that agrees, so `settings` is asserted to name what
                # differs rather than everything that was compared.
                (("namespace", "chosen", "other"), ("seed", "fresh", "fresh")),
                "Edit the file, or ask for what it already declares.",
            )
        self.assertIs(caught.exception.reason, Reason.DECLARATION_DIFFERS)
        # Which setting contradicts the file, in the file's own vocabulary. The two values
        # stay in the sentence: they are for a human choosing between them, and a script
        # acting on them would be rewriting a config from the text of a refusal.
        self.assertEqual(caught.exception.fields, {"settings": ("namespace",)})

    def test_every_reason_answers_with_the_class_its_own_row_names(self):
        """The whole table walked: each reason raises the exception its row declares, and
        the boundary answers for that exception under that same row.

        This is what a raise site never naming a class buys. Pair a reason with a class
        at each of a hundred raise sites and the two can disagree — silently, since both
        halves are plausible on their own, and what a script sees is the wrong exit code.
        Here the class is derived, so there is one place for the pairing to be wrong and
        this walks all of it. [LAW:one-source-of-truth]

        The message is asserted too, because `Exception` renders a two-argument
        construction as its tuple: hand the reason to `super().__init__` and every
        sentence in crom becomes `"('...', <Reason...>)"` — on stderr and in the
        envelope's `message` both.
        """
        for reason in Reason:
            with self.subTest(reason=reason.value):
                # Lowercase and underscores, checked because the slug is a published name
                # a script matches on: a `Chrome_Unusable` slipping into the table is a
                # vocabulary with two spellings, and slugs cannot be renamed later.
                self.assertRegex(reason.value, r"^[a-z][a-z0-9_]*$")
                # A field name is published on the same terms as the slug that discriminates
                # it, so it is held to the same spelling.
                for name in reason.carries:
                    self.assertRegex(name, r"^[a-z][a-z0-9_]*$")

                error = reason.error("a sentence", **dict.fromkeys(reason.carries, "a value"))
                self.assertIsInstance(error, reason.raises)
                self.assertIs(error.reason, reason)
                self.assertEqual(tuple(error.fields), reason.carries)
                self.assertEqual(str(error), "a sentence")

                answer = next(a for a in cli._ANSWERS if isinstance(error, a.error))
                self.assertIs(answer.error, reason.raises)

    def test_a_reason_decides_its_own_fields_rather_than_taking_what_it_is_given(self):
        """The schema belongs to the reason, so a raise site supplies values and nothing
        else — not which names, not which order.

        Without this, `reason` would discriminate a payload whose shape it did not decide:
        one `profile_unknown` site could answer with `declared` and another with
        `profiles`, both plausible, and a script branching on the slug would be reading a
        shape the slug does not actually promise. [LAW:types-are-the-program]

        The reordering half matters for the same reason the refusal does. Two raise sites
        naming the same fields in different orders would emit two key orders for one
        reason, and a reader diffing crom's output across runs would see a change where
        nothing changed.
        """
        for fields in ({}, {"known": ("a",), "extra": 1}, {"declared": ()}):
            with self.subTest(given=tuple(fields)):
                with self.assertRaises(TypeError):
                    Reason.NAMESPACE_UNKNOWN.error("a sentence", **fields)

        jumbled = Reason.PROFILE_UNKNOWN.error("a sentence", declared=(), source=None)
        self.assertEqual(tuple(jumbled.fields), Reason.PROFILE_UNKNOWN.carries)

    def test_no_module_builds_a_failure_without_naming_its_reason(self):
        """The backstop for the hundred raise sites this suite never executes.

        `CromError` cannot be constructed without a reason, so a raise that forgot one is
        a `TypeError`. But Python only says so on the line that runs, and these lines run
        only when something has already gone wrong — a new `raise CromError(...)` in a
        rarely-taken branch would ship, and the first person to reach it would get a
        crash where crom meant to hand them a sentence. So the invariant is asserted over
        the source rather than over one execution.

        What it looks for is a call on the bare name: `CromError(...)`, `NotFound(...)`,
        `Conflict(...)`. The one legitimate construction — `self.raises(...)`, inside
        `Reason.error` — is a call on an attribute and so is not one, which is why
        this needs no list of blessed exceptions to stay accurate.

        Same shape as `test_help_sections_cover_every_command`: a completeness check on a
        contract, not an assertion about how any one function is written.
        """
        family = {"CromError", "NotFound", "Conflict"}
        direct = [
            f"{path.name}:{node.lineno}: {node.func.id}(...)"
            for path in sorted(Path(cli.__file__).parent.glob("*.py"))
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in family
        ]
        self.assertEqual(direct, [], "build these through Reason.<REASON>.error(...)")

    def test_no_module_raises_a_reason_with_fields_it_does_not_declare(self):
        """The same backstop as above, one field over, and needed for the same reason.

        `Reason.error` refuses a payload its reason did not declare — but only on the line
        that runs, and these lines run only when something has already gone wrong. A raise
        site that spelled `declared` as `profiles` would pass review, pass the suite, and
        then hand the first person to reach it a `TypeError` where crom meant to hand them
        a sentence. So the agreement is asserted over the source instead.

        A raise through a local or an expression names no member, so it answers to the one
        rule still static: pass no fields — which is what makes it safe for the reasons it
        can reach. Neither arm sees a reason *reachable through* such a site later
        declaring one; that constraint lives in `Reason`.
        """
        trees = [
            (path.name, ast.parse(path.read_text()))
            for path in sorted(Path(cli.__file__).parent.glob("*.py"))
        ]
        sites = [
            (name, node)
            for name, tree in trees
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "error"
            and isinstance(node.func.value, ast.Attribute)
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "Reason"
        ]
        # A pattern that had drifted would match nothing, find nothing wrong, and pass —
        # an answer-shaped void wearing a green test as a costume. The floor is well under
        # the real count so that deleting a raise site is not a test failure.
        # [LAW:parse-dont-validate]
        self.assertGreater(len(sites), 80)

        wrong = [
            f"{name}:{node.lineno}: {node.func.value.attr} given "
            f"{tuple(k.arg for k in node.keywords)}"
            for name, node in sites
            if {k.arg for k in node.keywords} != set(Reason[node.func.value.attr].carries)
        ]
        self.assertEqual(wrong, [], "each raise site supplies exactly what its reason carries")

        # Derived from `sites`, not a second spelling of its pattern: the two lists
        # partition the same trees, so what counts as statically named is stated once.
        named = {id(node) for _, node in sites}
        dynamic = [
            f"{name}:{node.lineno}: given {tuple(k.arg for k in node.exc.keywords)}"
            for name, tree in trees
            for node in ast.walk(tree)
            if isinstance(node, ast.Raise)
            and isinstance(node.exc, ast.Call)
            and isinstance(node.exc.func, ast.Attribute)
            and node.exc.func.attr == "error"
            and id(node.exc) not in named
            and node.exc.keywords
        ]
        self.assertEqual(dynamic, [], "name the reason at the raise, or pass it no fields")

    def test_a_reader_that_left_does_not_turn_the_envelope_into_a_traceback(self):
        """The envelope is stdout, and stdout is what gets piped — so it can meet a
        reader that has already gone.

        Measured against the real binary before this was fixed: `crom up nosuchns/x
        --json` with stdout on a reader-less pipe exited 120 with a stack trace, while the
        same command without the flag exited 3 in silence. The envelope was being written
        from the exception's `show`, which click calls from its own `except
        ClickException` arm — and that arm's sibling `except OSError`, the one installing
        `PacifyFlushWrapper` for `errno.EPIPE`, cannot catch what it raises. 120 rather
        than 1 for the same reason as ever: the wrapper never got installed, so shutdown
        then failed to flush too.

        Writing it from the boundary instead puts it back inside the region click
        protects. Patching `click.echo` reaches only that write — `click.exceptions` binds
        its own `echo` by direct import, so the stderr prose is untouched by this mock and
        an escape here can only have come from the envelope.
        """
        self.crom("init")
        with mock.patch(
            "crom.cli.click.echo", side_effect=BrokenPipeError(errno.EPIPE, "Broken pipe")
        ):
            result = self.invoke("up", "nosuchns/x", "--json", expect=1)

        # Silence on both streams, which is the conventional end of a pipeline whose
        # reader left — and exit 1, the same ending `crom list | head` already gets.
        self.assertEqual((result.stdout, result.stderr), ("", ""))

    def test_a_failure_while_readying_crom_still_answers_the_flag_on_the_command_line(self):
        """Nothing crom does happens ahead of the parse, so nothing it does can miss the
        flag.

        Migration and the user-config bootstrap can both refuse, and they used to run
        from the group callback — which click calls *before* `make_context` parses the
        invoked subcommand's options, so `--json` had not been recorded yet and the
        refusal reached a machine as prose over an empty stdout. `CromCommand.invoke`
        runs them past the parse instead, which is the whole of what turns this into a
        document.

        Both halves, because the claim is about the window and not about migration: a
        command is readied by two calls, and either one refusing has the same answer.
        Which reason fills each is not under test — the envelope's other keys are covered
        by the tests that raise from inside a command — so each half carries the reason
        its own failure would really carry, and the assertion is the same either way.
        """
        self.crom("init")
        readying = (
            ("crom.migrate.run_if_needed", Reason.MIGRATION_NEEDS_QUIET, "a legacy Chrome is still running"),
            ("crom.cli._bootstrap_user_config", Reason.CONFIG_UNWRITABLE, "could not write the user config"),
        )
        for target, reason, sentence in readying:
            with self.subTest(target=target):
                with mock.patch(target, side_effect=reason.error(sentence)):
                    result = self.invoke("list", "--json", expect=1)

                self.assertEqual(
                    json.loads(result.stdout)["error"],
                    {
                        "code": 1,
                        "kind": "failure",
                        "reason": reason.value,
                        "fields": {},
                        "message": sentence,
                    },
                )
                self.assertEqual(result.stderr.strip(), f"Error: {sentence}")

    def test_an_eager_option_is_answered_without_readying_crom_at_all(self):
        """Every `--help` still answers on a machine where no command can run.

        click answers an eager option while the command line is still being parsed, so it
        never reaches `CromCommand.invoke`. That is what keeps the help text reachable on
        the machine where someone is most likely to be reaching for it. Readying that ran
        any earlier would take it out with everything else: from the group callback,
        bootstrap happened before click could descend into a subcommand, so
        `crom up --help` failed where `crom --help` worked — splitting the two things a
        confused user tries, on exactly the machine where they are confused.

        The commands come from the click group rather than a list here, so a command
        added tomorrow is covered without anyone remembering to cover it.
        [LAW:single-enforcer]

        The refused readying is the premise and not decoration: `crom list` failing under
        it is what stops this passing on a healthy home and pinning nothing.
        [LAW:verifiable-goals]
        """
        with mock.patch(
            "crom.cli._bootstrap_user_config",
            side_effect=Reason.CONFIG_UNWRITABLE.error("could not write the user config"),
        ):
            self.crom("list", expect=1)

            self.assertIn("Usage:", self.crom("--help"))
            for name in cli.main.commands:
                with self.subTest(command=name):
                    self.assertIn("Usage:", self.crom(name, "--help"))

    def test_bad_usage_is_the_one_failure_left_without_an_envelope(self):
        """The carve-out the README publishes, and now the only one.

        A malformed command line fails *during* the parse, so the `--json` sitting on it
        was never understood either — there is no flag to honour rather than a flag
        arriving too late. That is what keeps this outside the envelope permanently,
        where the readying failures above were only outside by accident of ordering, and
        it is why closing that accident does not promise envelopes for typos.
        """
        self.crom("init")

        result = self.invoke("list", "--nosuchflag", "--json", expect=2)

        self.assertEqual(result.stdout, "")
        self.assertIn("No such option", result.stderr)

    def test_every_command_offering_json_answers_a_failure_in_json(self):
        """The envelope belongs to the flag, so every command carrying the flag has it.

        The commands are discovered from click rather than listed here, which is the
        whole claim: `--json` is one declaration whose callback records that it was
        passed, so a command added tomorrow is covered without anyone remembering to
        cover it. Naming them instead would test the rule's instances and leave the
        rule itself unasserted. [LAW:single-enforcer]

        Readying is the seam because `CromGroup.command_class` puts it in front of every
        command by construction, so the induced failure reaches a command added tomorrow
        for the same reason the envelope does. `load_ambient` stood here first, on the
        grounds that all six commands crossed it to learn what is declared — which was
        true, and was a coincidence: `crom doctor` reads the ledger and asks no config
        anything, because a doctor has to answer on the machine whose config is the
        problem. A seam that holds for the commands that happen to exist is a claim about
        them and not about the flag. [LAW:behavior-not-structure]

        Its sibling below covers the other dimension — that *both* calls in the readying
        window answer, rather than that every command does — so neither subsumes the
        other.
        """
        self.crom("init")
        offering = _commands_offering_json()
        # A discovery that found nothing would loop zero times and assert nothing — an
        # answer-shaped void wearing a passing test as a costume. New commands may join
        # freely; the loop is what covers them. [LAW:parse-dont-validate]
        self.assertGreaterEqual(offering, {"up", "down", "restart", "show", "list", "config"})

        sentence = "a legacy Chrome is still running"
        for name in offering:
            with self.subTest(command=name):
                with mock.patch(
                    "crom.migrate.run_if_needed",
                    side_effect=Reason.MIGRATION_NEEDS_QUIET.error(sentence),
                ):
                    result = self.invoke(name, "--json", expect=1)
                self.assertEqual(
                    json.loads(result.stdout)["error"],
                    {
                        "code": 1,
                        "kind": "failure",
                        "reason": Reason.MIGRATION_NEEDS_QUIET.value,
                        # Present and empty rather than absent, because this reason
                        # looked nothing up. A caller reads `fields` the same way for
                        # every failure instead of testing for the key first.
                        "fields": {},
                        "message": sentence,
                    },
                )

    def test_the_readme_table_of_fields_matches_what_each_reason_carries(self):
        """The `fields` schema is published twice — once in `Reason`, once in a table a
        reader has no other way to discover — so the copy is held to the original.

        A duplicated enumeration that nothing checks drifts on a schedule, and this repo
        has already paid for one: a comment listing the config keys quietly stopped
        naming `flags`, and stayed plausible for as long as nobody compared it. This
        table cannot be deleted the way that comment could, so it is guarded instead.
        [LAW:one-source-of-truth]

        One equality carries the contract in both directions. A reason that gains fields
        and never reaches the README fails here; so does a row for a reason that lost
        them, a field renamed on one side only, and a row naming the right fields in the
        wrong order — `Reason.error` builds the envelope's key order out of `carries`, so
        the order printed in the table is the order a caller will actually receive.
        """
        readme = _README.read_text().splitlines()
        # `index` rather than a search that can come back empty: a table renamed, moved
        # or deleted has to fail here, not leave the loop below with nothing to walk and
        # a passing test to show for it. [LAW:no-silent-failure]
        start = readme.index("| reason | carries | what those hold |") + 2

        tabled = {}
        for row in takewhile(lambda line: line.startswith("|"), readme[start:]):
            # Split into cells first and read only the second. Column three is prose
            # carrying backticks of its own, so a sweep for backticks across the whole
            # row would come back with `null` and `CROM_CONFIG` as field names.
            reason, carries, _prose = row.strip().strip("|").split("|")
            (slug,) = _readme_names(reason)
            tabled[slug] = _readme_names(carries)

        # A parse that found no rows would compare empty against empty on the day the
        # last reason stops carrying anything — an answer-shaped void wearing a green
        # test as a costume. [LAW:parse-dont-validate]
        self.assertTrue(tabled, "the fields table is gone from README.md")
        self.assertEqual(tabled, {reason.value: reason.carries for reason in Reason if reason.carries})

    def test_the_readme_names_exactly_the_commands_that_take_json(self):
        """The other enumeration a reader cannot get anywhere else: which commands answer
        `--json` at all, and which never will.

        Both halves are asserted because each goes stale in its own way. A new command
        carrying the flag and missing from the first list leaves a caller not knowing it
        may parse that command's answer; a new command *without* the flag and missing
        from the second leaves the sentence claiming to be exhaustive when it is not. The
        second list is derived by subtracting the first from the same group, so what the
        commands are is read once. [LAW:one-source-of-truth]

        Both come from click and never from a literal here. Names written into this file
        would be a third copy of the fact the test exists to keep at two, green for
        exactly as long as nobody touched the CLI. [LAW:behavior-not-structure]
        """
        commands = set(cli.main.list_commands(None))
        offering = _commands_offering_json()
        # The sentence is wrapped across lines in the file, so whitespace is normalised
        # before it is matched. Either side admits only a backticked list, so the search
        # cannot begin early at some unrelated backtick and swallow the prose between.
        sentence = re.search(
            r"(`\w+`(?:(?:, | and )`\w+`)*) take the flag; "
            r"(`\w+`(?:(?:, | and )`\w+`)*) answer in prose only\.",
            " ".join(_README.read_text().split()),
        )
        self.assertIsNotNone(sentence, "README.md no longer says which commands take --json")

        self.assertEqual(set(_readme_names(sentence[1])), offering)
        self.assertEqual(set(_readme_names(sentence[2])), commands - offering)

    # --- crom's own version ----------------------------------------------------------

    def test_the_version_reported_is_the_installed_distributions_own(self):
        """crom states a version, and it is the one the build recorded.

        Asserted against the metadata rather than against `"0.1.0"`, because a literal
        here would be the second spelling of the version the reading exists to avoid:
        it would have to be edited on every release, and until someone did, it would
        pin crom to a version crom is not. [LAW:one-source-of-truth]

        Whole lines rather than a substring, so the format is pinned too — `assertIn`
        would pass just as happily against click's default `crom, version 0.1.0`, which
        spells the answer differently depending on argv[0].
        """
        self.assertEqual(
            self.crom("--version").splitlines(), [importlib.metadata.version("crom")]
        )

    def test_the_version_answers_where_every_other_command_fails(self):
        """Asking crom which crom this is must not require a crom that works.

        crom migrates and bootstraps a user config before every command, so a home
        crom cannot write to takes every one of them out — which is exactly the machine
        someone is standing on when they need to know what they are running. `--version`
        is answered while the command line is still being parsed, before any of that.

        The failing `list` is the premise, not decoration: without it this passes on a
        perfectly healthy home and pins nothing. [LAW:verifiable-goals]
        """
        blocker = self.root / "a-file-where-a-home-should-be"
        blocker.write_text("")
        unusable = {
            "HOME": str(blocker / "home"),
            "XDG_CONFIG_HOME": str(blocker / "config"),
            "XDG_STATE_HOME": str(blocker / "state"),
        }

        with mock.patch.dict(os.environ, unusable):
            self.crom("list", expect=1)

            self.assertEqual(
                self.crom("--version").splitlines(), [importlib.metadata.version("crom")]
            )


if __name__ == "__main__":
    unittest.main()
