"""Tests for crom.mcp — which key a profile gets in .mcp.json, and how it is merged in.

The key tests assert two properties rather than one spelling, because both are what a
constant key cost: distinct refs must reach distinct keys, and every key must survive
into the tool name Claude Code derives from it (`mcp__<key>__<tool>`, held to
`^[A-Za-z0-9_-]{1,64}$`) without being rewritten. [LAW:behavior-not-structure] the
escape spelling is free to change; a profile quietly losing its entry is not.
"""

import itertools
import json
import re
import tempfile
import unittest
from pathlib import Path

from crom import mcp
from crom.model import ProfileRef, ResolvedProfile, SeedFresh

# The alphabet a namespace or profile name is built from, plus the two characters that
# make key derivation hard: `-`, which is legal inside a name and so cannot separate
# two of them, and `.`, which is legal inside a name and illegal in a tool name.
_NAME_CHARS = "ab-._1"
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def legal_names(length: int) -> list[str]:
    """Every legal name of `length` characters over `_NAME_CHARS`, given names start alnum."""
    return ["a" + "".join(rest) for rest in itertools.product(_NAME_CHARS, repeat=length - 1)]


def profile(ref: ProfileRef, port: int) -> ResolvedProfile:
    """A profile carrying the only two fields `mcp.write` reads."""
    return ResolvedProfile(
        ref=ref,
        port=port,
        profile_dir=Path("/profiles") / ref.namespace / ref.name,
        chrome_binary=Path("/chrome"),
        argv=("/chrome",),
        env={},
        seed=SeedFresh(),
        source=None,
    )


class EntryKeyTest(unittest.TestCase):
    def test_names_the_profile_it_wires(self):
        self.assertEqual(mcp.entry_key(ProfileRef("myproj", "default")), "crom__myproj__default")

    def test_a_hyphen_in_a_name_does_not_move_the_boundary(self):
        # The collision a bare `-` between namespace and name would have produced, and
        # the reason the components are escaped before they are joined.
        self.assertNotEqual(
            mcp.entry_key(ProfileRef("a-b", "c")),
            mcp.entry_key(ProfileRef("a", "b-c")),
        )

    def test_a_dot_survives_as_itself(self):
        # `.` is stripped from the derived tool name, so a key that passed it through
        # would fold `a.b` onto `ab` one layer below crom.
        self.assertNotEqual(
            mcp.entry_key(ProfileRef("a.b", "c")),
            mcp.entry_key(ProfileRef("ab", "c")),
        )

    def test_distinct_refs_reach_distinct_keys(self):
        names = [name for length in (1, 2, 3) for name in legal_names(length)]
        keys = {}
        for namespace, name in itertools.product(names, repeat=2):
            key = mcp.entry_key(ProfileRef(namespace, name))
            self.assertNotIn(key, keys, f"{namespace}/{name} collides with {keys.get(key)}")
            keys[key] = f"{namespace}/{name}"

    def test_every_key_survives_into_a_tool_name(self):
        for length in (1, 2, 3):
            for name in legal_names(length):
                key = mcp.entry_key(ProfileRef(name, name))
                self.assertRegex(key, _TOOL_NAME)


class WriteTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / ".mcp.json"
        self.dev = profile(ProfileRef("myproj", "dev"), 9222)

    def tearDown(self):
        self.tmpdir.cleanup()

    def servers(self) -> dict:
        return json.loads(self.path.read_text())["mcpServers"]

    def test_creates_file_when_absent(self):
        mcp.write(self.dev, self.path)
        self.assertEqual(
            self.servers()[mcp.entry_key(self.dev.ref)]["args"][-1],
            "http://127.0.0.1:9222",
        )

    def test_merges_with_existing_unrelated_server(self):
        self.path.write_text(json.dumps({
            "mcpServers": {"other": {"type": "stdio", "command": "foo", "args": []}}
        }))
        mcp.write(self.dev, self.path)
        self.assertIn("other", self.servers())
        self.assertIn(mcp.entry_key(self.dev.ref), self.servers())

    def test_rewires_the_same_profile_in_place(self):
        mcp.write(self.dev, self.path)
        mcp.write(profile(self.dev.ref, 9500), self.path)
        self.assertEqual(list(self.servers()), [mcp.entry_key(self.dev.ref)])
        self.assertEqual(self.servers()[mcp.entry_key(self.dev.ref)]["args"][-1],
                         "http://127.0.0.1:9500")

    def test_rejects_invalid_json(self):
        self.path.write_text("not json")
        with self.assertRaises(ValueError):
            mcp.write(self.dev, self.path)
        self.assertEqual(self.path.read_text(), "not json")

    def test_rejects_non_object_root(self):
        original = json.dumps([1, 2, 3])
        self.path.write_text(original)
        with self.assertRaises(ValueError):
            mcp.write(self.dev, self.path)
        self.assertEqual(self.path.read_text(), original)

    def test_rejects_non_object_mcp_servers(self):
        original = json.dumps({"mcpServers": "oops"})
        self.path.write_text(original)
        with self.assertRaises(ValueError):
            mcp.write(self.dev, self.path)
        self.assertEqual(self.path.read_text(), original)


if __name__ == "__main__":
    unittest.main()
