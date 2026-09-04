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
from crom.model import CromError, ProfileRef, Reason, ResolvedProfile, SeedFresh

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

    def test_a_ref_that_would_overrun_the_tool_name_is_refused(self):
        # Long but legal: `_NAME_RE` allows 64 characters in each half, and a namespace
        # is slugged from the project directory, so the user need not have chosen it.
        long_ref = ProfileRef("a" * 40, "dev")
        with self.assertRaises(CromError) as caught:
            mcp.entry_key(long_ref)
        self.assertIn(str(long_ref), str(caught.exception))
        self.assertIn(str(mcp.KEY_LIMIT), str(caught.exception))
        # Against its neighbour below: both refusals mean "crom did not write
        # your .mcp.json", and they send the user to different files — this one
        # to rename a profile, that one to repair the JSON.
        self.assertIs(caught.exception.reason, Reason.MCP_KEY_TOO_LONG)

    def test_a_ref_that_exactly_fills_the_budget_is_kept(self):
        # `crom` + two `__` joins leaves KEY_LIMIT - 8 characters for the two names.
        ref = ProfileRef("a" * (mcp.KEY_LIMIT - 8 - 3), "dev")
        self.assertEqual(len(mcp.entry_key(ref)), mcp.KEY_LIMIT)

    def test_the_budget_leaves_room_for_the_longest_tool_name(self):
        # The whole reason KEY_LIMIT exists: `mcp__<key>__<tool>` is what Claude Code
        # sends as a tool name, and the API holds a tool name to 64 characters.
        longest = f"mcp__{'k' * mcp.KEY_LIMIT}__{mcp._LONGEST_SERVER_TOOL}"
        self.assertEqual(len(longest), mcp._TOOL_NAME_LIMIT)

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

    def test_rejects_a_ref_it_cannot_spell(self):
        # The fourth instance of the same contract as the three below: a refused write
        # leaves `path` as it found it. Asserted through `write` and not only through
        # `entry_key`, because what is at stake is the order — deriving the key after
        # `path.write_text` would clobber a good file for a profile it then refuses.
        original = json.dumps({"mcpServers": {"other": {"command": "foo"}}})
        self.path.write_text(original)
        with self.assertRaises(CromError):
            mcp.write(profile(ProfileRef("a" * 40, "dev"), 9222), self.path)
        self.assertEqual(self.path.read_text(), original)

    def test_upgrades_a_legacy_entry_wired_to_this_profile(self):
        # The file a previous crom left: one entry under the constant key, naming the
        # browser this profile owns. It is the same wiring, so it is renamed, not
        # duplicated — the point of the ticket, and the end of the epic's interim state.
        self.path.write_text(json.dumps({
            "mcpServers": {mcp.LEGACY_KEY: mcp.server_entry(9222)}
        }))
        mcp.write(self.dev, self.path)
        self.assertEqual(list(self.servers()), [mcp.entry_key(self.dev.ref)])
        self.assertEqual(self.servers()[mcp.entry_key(self.dev.ref)], mcp.server_entry(9222))

    def test_an_unrelated_server_survives_the_upgrade(self):
        # [LAW:no-silent-failure] the upgrade may only ever touch the one entry it can
        # prove is crom's; a neighbour losing fields to it would be invisible until the
        # neighbour's own tool broke.
        other = {"type": "stdio", "command": "foo", "args": ["--flag"], "env": {"A": "1"}}
        self.path.write_text(json.dumps({
            "mcpServers": {"other": other, mcp.LEGACY_KEY: mcp.server_entry(9222)}
        }))
        mcp.write(self.dev, self.path)
        self.assertEqual(self.servers()["other"], other)
        self.assertNotIn(mcp.LEGACY_KEY, self.servers())

    def test_keeps_a_legacy_key_entry_crom_did_not_write(self):
        # Same key, same port, hand-written body. Recognition is `== server_entry(port)`
        # precisely so this survives: crom cannot show it wrote this, so it must not
        # delete it.
        theirs = {"command": "npx", "args": ["--browserUrl", "http://127.0.0.1:9222"]}
        self.path.write_text(json.dumps({"mcpServers": {mcp.LEGACY_KEY: theirs}}))
        mcp.write(self.dev, self.path)
        self.assertEqual(self.servers()[mcp.LEGACY_KEY], theirs)
        self.assertIn(mcp.entry_key(self.dev.ref), self.servers())

    def test_keeps_a_legacy_entry_wired_to_another_port(self):
        # crom's handwriting, but naming a browser that is not this profile's. Renaming
        # it would need the ledger to say whose port that is, and a recycled port makes
        # that answer confidently wrong — so it waits for its own profile to be wired.
        stranger = mcp.server_entry(9500)
        self.path.write_text(json.dumps({"mcpServers": {mcp.LEGACY_KEY: stranger}}))
        mcp.write(self.dev, self.path)
        self.assertEqual(self.servers()[mcp.LEGACY_KEY], stranger)
        self.assertIn(mcp.entry_key(self.dev.ref), self.servers())

    def test_a_ref_it_cannot_spell_leaves_a_legacy_entry_wired(self):
        # The refusal must not land between the drop and the replacement: a profile that
        # outgrew KEY_LIMIT would otherwise lose the wiring it still had.
        original = json.dumps({"mcpServers": {mcp.LEGACY_KEY: mcp.server_entry(9222)}})
        self.path.write_text(original)
        with self.assertRaises(CromError):
            mcp.write(profile(ProfileRef("a" * 40, "dev"), 9222), self.path)
        self.assertEqual(self.path.read_text(), original)

    def test_reports_that_there_was_no_legacy_entry(self):
        self.assertIs(mcp.write(self.dev, self.path), mcp.Legacy.ABSENT)

    def test_reports_the_legacy_entry_it_renamed(self):
        self.path.write_text(json.dumps({"mcpServers": {mcp.LEGACY_KEY: mcp.server_entry(9222)}}))
        self.assertIs(mcp.write(self.dev, self.path), mcp.Legacy.REPLACED)

    def test_reports_a_legacy_entry_left_on_another_port(self):
        self.path.write_text(json.dumps({"mcpServers": {mcp.LEGACY_KEY: mcp.server_entry(9500)}}))
        self.assertIs(mcp.write(self.dev, self.path), mcp.Legacy.KEPT)

    def test_reports_a_legacy_entry_left_because_crom_did_not_write_it(self):
        # The second shape that reaches KEPT. It is one value with the case above and not
        # two, because separating them means parsing a body crom did not write — the
        # guess `write` refuses to make, and so the phrasing must not claim either.
        theirs = {"command": "npx", "args": ["--browserUrl", "http://127.0.0.1:9222"]}
        self.path.write_text(json.dumps({"mcpServers": {mcp.LEGACY_KEY: theirs}}))
        self.assertIs(mcp.write(self.dev, self.path), mcp.Legacy.KEPT)

    def test_a_null_legacy_entry_is_not_reported_as_absent(self):
        # `{"chrome-devtools-mcp": null}` is legal JSON, and `servers.get(LEGACY_KEY)`
        # reads it as no legacy entry at all — reporting ABSENT, the one outcome that
        # says nothing, about a file that still declares two servers after the write.
        self.path.write_text(json.dumps({"mcpServers": {mcp.LEGACY_KEY: None}}))
        self.assertIs(mcp.write(self.dev, self.path), mcp.Legacy.KEPT)
        self.assertIsNone(self.servers()[mcp.LEGACY_KEY])

    def test_the_rename_is_reported_once_and_not_on_every_write(self):
        # Convergence, not a standing complaint: the second write has no legacy entry to
        # rename, and reporting one would be crom claiming work it did not do.
        self.path.write_text(json.dumps({"mcpServers": {mcp.LEGACY_KEY: mcp.server_entry(9222)}}))
        mcp.write(self.dev, self.path)
        self.assertIs(mcp.write(self.dev, self.path), mcp.Legacy.ABSENT)

    def test_rejects_invalid_json(self):
        self.path.write_text("not json")
        with self.assertRaises(CromError) as caught:
            mcp.write(self.dev, self.path)
        self.assertIs(caught.exception.reason, Reason.MCP_CONFIG_INVALID)
        self.assertEqual(self.path.read_text(), "not json")

    def test_rejects_non_object_root(self):
        original = json.dumps([1, 2, 3])
        self.path.write_text(original)
        with self.assertRaises(CromError):
            mcp.write(self.dev, self.path)
        self.assertEqual(self.path.read_text(), original)

    def test_rejects_non_object_mcp_servers(self):
        original = json.dumps({"mcpServers": "oops"})
        self.path.write_text(original)
        with self.assertRaises(CromError):
            mcp.write(self.dev, self.path)
        self.assertEqual(self.path.read_text(), original)


if __name__ == "__main__":
    unittest.main()
