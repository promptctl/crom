"""Tests for crom.mcp.write()."""

import json
import tempfile
import unittest
from pathlib import Path

from crom import mcp


class WriteTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / ".mcp.json"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_creates_file_when_absent(self):
        mcp.write(9222, self.path)
        config = json.loads(self.path.read_text())
        self.assertEqual(
            config["mcpServers"][mcp.SERVER_NAME]["args"][-1],
            "http://127.0.0.1:9222",
        )

    def test_merges_with_existing_unrelated_server(self):
        self.path.write_text(json.dumps({
            "mcpServers": {"other": {"type": "stdio", "command": "foo", "args": []}}
        }))
        mcp.write(9222, self.path)
        config = json.loads(self.path.read_text())
        self.assertIn("other", config["mcpServers"])
        self.assertIn(mcp.SERVER_NAME, config["mcpServers"])

    def test_rejects_invalid_json(self):
        self.path.write_text("not json")
        with self.assertRaises(ValueError):
            mcp.write(9222, self.path)
        self.assertEqual(self.path.read_text(), "not json")

    def test_rejects_non_object_root(self):
        original = json.dumps([1, 2, 3])
        self.path.write_text(original)
        with self.assertRaises(ValueError):
            mcp.write(9222, self.path)
        self.assertEqual(self.path.read_text(), original)

    def test_rejects_non_object_mcp_servers(self):
        original = json.dumps({"mcpServers": "oops"})
        self.path.write_text(original)
        with self.assertRaises(ValueError):
            mcp.write(9222, self.path)
        self.assertEqual(self.path.read_text(), original)


if __name__ == "__main__":
    unittest.main()
