"""Tests for locating the Chrome executable.

The module promises to name every path it tried "rather than failing later inside
Popen", which only holds if a candidate it accepts is actually runnable.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crom import browser
from crom.model import CromError


class ResolveTest(unittest.TestCase):
    def setUp(self):
        # `setUp` runs per test method, so an unowned `mkdtemp` here leaks one directory
        # of stub binaries per test rather than one per class.
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _binary(self, name: str, *, executable: bool) -> Path:
        path = self.root / name
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755 if executable else 0o644)
        return path

    def test_an_executable_absolute_path_resolves(self):
        binary = self._binary("chrome", executable=True)
        self.assertEqual(browser._resolve(str(binary)), binary)

    def test_a_non_executable_absolute_path_does_not_resolve(self):
        """`shutil.which` applies this test on the other branch of the same function, and
        `config._parse_chrome_binary` applies it to a configured binary. Without it the
        two halves meant different things — one "usable as a browser", the other merely
        "exists" — and a stripped or corrupted install was returned as the answer."""
        binary = self._binary("chrome", executable=False)
        self.assertIsNone(browser._resolve(str(binary)))

    def test_a_directory_does_not_resolve_even_though_it_exists(self):
        (self.root / "Chrome.app").mkdir()
        self.assertIsNone(browser._resolve(str(self.root / "Chrome.app")))

    def test_an_absent_absolute_path_does_not_resolve(self):
        self.assertIsNone(browser._resolve(str(self.root / "nope")))

    def test_a_non_executable_candidate_is_skipped_for_the_next_one(self):
        """The behavioural consequence: discovery keeps looking rather than returning
        something that cannot be launched."""
        broken = self._binary("broken", executable=False)
        working = self._binary("working", executable=True)
        with mock.patch.dict(browser._CANDIDATES, {os.sys.platform: [str(broken), str(working)]}):
            self.assertEqual(browser.find_chrome(), working)

    def test_with_no_usable_candidate_it_names_what_it_tried(self):
        broken = self._binary("broken", executable=False)
        with mock.patch.dict(browser._CANDIDATES, {os.sys.platform: [str(broken)]}):
            with self.assertRaises(CromError) as caught:
                browser.find_chrome()
        self.assertIn(str(broken), str(caught.exception))


if __name__ == "__main__":
    unittest.main()
