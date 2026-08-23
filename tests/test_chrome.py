"""Tests for reading the process table — which running Chrome belongs to which profile.

`ps` flattens argv into one string, so recovering the user-data-dir from it is a parsing
problem with real edge cases: directories containing spaces, and profile paths that are
prefixes of one another. Both decide whether `crom up` sees its own browser or launches
a second one on top of it.
"""

import unittest
from pathlib import Path

from crom import chrome
from crom.resolve import build_argv


def ps_line(pid: int, argv) -> str:
    """Render argv the way `ps -Ao pid=,command=` does: space-joined, boundaries lost."""
    return f"{pid} {' '.join(argv)}"


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

    def test_a_browser_crom_did_not_launch_is_not_a_crom_profile(self):
        # The user's own Chrome carries a --user-data-dir but no CDP port; it is not a
        # profile crom manages and must not be reported as one.
        output = ps_line(99, ("/chrome", "--user-data-dir=/Users/bmf/Library/Application Support/Google/Chrome"))

        self.assertEqual(chrome._group_by_user_data_dir(output), {})

    def test_ps_header_and_blank_lines_are_not_processes(self):
        self.assertEqual(chrome._group_by_user_data_dir("  PID COMMAND\n\n   \n"), {})


if __name__ == "__main__":
    unittest.main()
