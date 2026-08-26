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
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from crom import cli
from crom.config import load_ambient
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
        self.assertIn('seed = "chrome"', user_config_file().read_text())

    def test_a_corrupt_user_config_is_an_error_not_a_traceback(self):
        """`main` runs `_bootstrap_user_config()` before anything else, on every command.

        That path reaches `configwrite._load` — and so `tomlkit.parse` — before
        `config.parse` would ever have rejected the file. An unguarded parse error there
        came back from every command, including the ones a user would run to repair it,
        and as a traceback rather than the documented exit code.
        """
        user_config_file().parent.mkdir(parents=True, exist_ok=True)
        user_config_file().write_text("not = = valid toml [[[")

        for command in (["list"], ["config"], ["port"]):
            with self.subTest(command=command):
                output = self.crom(*command, expect=1)
                self.assertIn("cannot be read as TOML", output)

    def test_a_user_config_with_a_non_table_profiles_key_is_an_error(self):
        user_config_file().parent.mkdir(parents=True, exist_ok=True)
        user_config_file().write_text('profiles = "typo"\n')

        output = self.crom("list", expect=1)
        self.assertIn("`profiles` must be a table", output)

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

    def test_losing_a_concurrent_add_leaves_the_winners_port_alone(self):
        """`profile.ref` is the profile's shared identity, not one attempt's.

        Two `crom add ci` calls resolve the same ref and the same reserved port. When the
        loser's write raises FileExistsError, the declaration that now exists is the
        *winner's* and it owns that reservation — releasing it here silently moved a live
        profile to a new port on its next resolve.

        The race is reproduced deterministically: `declares` is stubbed False so the
        pre-check passes as it would for the loser, and `add_profile` then raises for
        real because the name is genuinely already declared.
        """
        self.crom("init")
        self.crom("add", "ci")
        winners_port = self.crom("port", "ci")

        with mock.patch("crom.configwrite.declares", return_value=False):
            self.crom("add", "ci", expect=4)

        self.assertEqual(self.crom("port", "ci"), winners_port)

    def test_add_refuses_when_the_project_config_vanished_after_discovery(self):
        """`_declare` creates a missing file, and the header it would use carries no
        `namespace` key — so recreating a deleted project config yields a file the parser
        rejects wholesale, breaking every command in the project.

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
            self.crom("add", "two", expect=3)

        self.assertFalse((self.project / ".crom.toml").exists())

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
        self.assertEqual(project, "chrome")
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
