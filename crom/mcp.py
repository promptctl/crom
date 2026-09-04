"""Generates the .mcp.json entry that points chrome-devtools-mcp at a given profile."""

import json
from enum import Enum, auto
from pathlib import Path

from .model import ProfileRef, Reason, ResolvedProfile

# What marks an entry in `.mcp.json` as crom's, and the only part of the key crom
# chooses rather than derives. Four characters, because the rest of the key is a budget
# — see KEY_LIMIT. The obvious prefix, the server's own `chrome-devtools-mcp`, would
# spend 19 of it before naming any profile at all; the command below still says which
# server this entry runs.
KEY_PREFIX = "crom"

# Why the key has a length at all. Claude Code exposes an MCP server's tools to the
# model as `mcp__<key>__<tool>`, and a tool name is held to `^[A-Za-z0-9_-]{1,64}$` —
# so the key does not get 64 characters, it gets what is left after the two joins and
# the longest tool name the server on the other side publishes. Spelled as the
# subtraction rather than as the answer, because every term is a fact about someone
# else's software: when chrome-devtools-mcp adds a longer tool, the budget moves, and
# the line that has to change is the one naming the tool.
_TOOL_NAME_LIMIT = 64
_LONGEST_SERVER_TOOL = "performance_analyze_insight"
KEY_LIMIT = _TOOL_NAME_LIMIT - len("mcp__") - len("__") - len(_LONGEST_SERVER_TOOL)

# How a namespace or a profile name is spelled inside the key. Two substitutions,
# because a name matches `^[a-z0-9][a-z0-9._-]*$` and the key alphabet is only
# `[a-z0-9_-]`: `.` has nowhere to land, and `_` has to move aside so that `__` can
# separate the two components without a name ever producing one. `-` is deliberately
# absent — it is the character names actually use, and it survives untouched.
_ESCAPES = {"_": "_u", ".": "_d"}

# The key every crom wrote before the key was derived from the profile (0f4b8a2). It
# names no profile, so an entry still sitting under it says which browser it points at
# and nothing about whose browser that is — `write` explains what is done with that.
LEGACY_KEY = "chrome-devtools-mcp"


class Legacy(Enum):
    """What `write` found under `LEGACY_KEY`, and therefore what it did with it.

    Three values rather than the `str | None` this would collapse to, because "renamed
    it" and "there was none" do not exhaust the outcomes: an entry `write` cannot show
    is crom's own stays where it is, and the file goes on declaring two chrome-devtools
    servers. A bare `None` folds that onto "nothing to report" and the one outcome the
    user cannot see for themselves is the one that goes out silently.
    [LAW:no-silent-failure] [LAW:types-are-the-program]
    """

    ABSENT = auto()
    REPLACED = auto()
    KEPT = auto()

    @classmethod
    def of(cls, servers: dict, entry: dict) -> "Legacy":
        """Which one the servers already in the file are, measured against `entry`.

        The one place the question is asked, so the file `write` leaves and the sentence
        its caller prints about that file come from a single answer rather than from two
        tests that can drift into disagreeing. [LAW:one-source-of-truth]

        Membership and not `servers.get(LEGACY_KEY)`, because `{"chrome-devtools-mcp":
        null}` is legal JSON: a `.get` reads that file as having no legacy entry at all
        and answers ABSENT — the one value that says nothing. [LAW:no-silent-failure]
        """
        if LEGACY_KEY not in servers:
            return cls.ABSENT
        return cls.REPLACED if servers[LEGACY_KEY] == entry else cls.KEPT


def entry_key(ref: ProfileRef) -> str:
    """The `mcpServers` key `ref` owns, and that no other profile can be handed.

    A constant key made `.mcp.json` a one-profile file: wiring `ci` after `dev` in one
    directory overwrote `dev` and reported success, which is the collision crom exists
    to prevent. [LAW:one-source-of-truth] a ref is a profile's identity everywhere else
    — the ledger keys on it, the profile directory is named for it — so the entry key
    derives from that identity instead of becoming a second name for the same profile.

    Deriving it is not joining it. `-` is legal *inside* a namespace and inside a name,
    so a bare `-` between the two would put `a-b/c` and `a/b-c` on one key and bring the
    overwrite back in a narrower case; `.` is legal in a name and is stripped from the
    tool name Claude Code derives, so `a.b` and `ab` would fold together one layer down
    where crom cannot see it. Escaping first and joining on `__` closes both: every `_`
    in a component becomes `_u` before the separator is written, so a `__` in the key is
    always the separator and never a name. [LAW:no-silent-failure] a collision here is
    silent by nature — the loser is a config entry, and nothing reads it again to
    notice it went missing.
    """
    namespace, name = (
        "".join(_ESCAPES.get(char, char) for char in part) for part in (ref.namespace, ref.name)
    )
    key = f"{KEY_PREFIX}__{namespace}__{name}"
    if len(key) > KEY_LIMIT:
        raise Reason.MCP_KEY_TOO_LONG.error(
            f"profile '{ref}' does not fit in a .mcp.json entry key: it needs "
            f"{len(key)} characters and only {KEY_LIMIT} are available, because Claude "
            f"Code names this server's tools `mcp__{key}__<tool>` and refuses a tool "
            f"name past {_TOOL_NAME_LIMIT} characters.\n"
            f"Shorten the profile name, or give the project a shorter namespace with "
            f"`crom init <namespace>`."
        )
    return key


def server_entry(port: int) -> dict:
    return {
        "type": "stdio",
        "command": "pnpm",
        "args": [
            "dlx",
            "-y",
            "chrome-devtools-mcp@latest",
            "--no-usage-statistics",
            "--browserUrl",
            f"http://127.0.0.1:{port}",
        ],
        "env": {},
    }


def write(profile: ResolvedProfile, path: Path) -> Legacy:
    """Merge `profile`'s chrome-devtools-mcp server entry into `path`.

    Takes the whole profile rather than a ref and a port because those are one fact:
    [LAW:types-are-the-program] a `write(ref, port, path)` would let a caller key an
    entry to one profile and point it at another profile's port, and nothing downstream
    could tell that it had.

    Preserves any other servers already declared in `path`, and refuses rather
    than writing: a file that isn't a JSON object we can merge into, and a ref
    `entry_key` cannot spell, both leave `path` exactly as they found it.

    An entry under `LEGACY_KEY` that is *this* wiring is renamed rather than left
    beside it. Recognising it is exact, not a guess: `server_entry` has produced one
    shape since crom's first commit, so every entry crom ever wrote for a port equals
    `server_entry(port)` today, and [LAW:one-source-of-truth] comparing against the
    producer means the two cannot drift into disagreeing about crom's own handwriting.
    Anything a human wrote under that key differs somewhere and survives untouched —
    which is the direction to fail in, since the alternative is deleting config crom
    did not write.

    Matching this profile's port, rather than any port the ledger holds, is what makes
    the upgrade a rename and never a re-pointing: the entry dropped and the entry
    written name the same browser, so the file behaves identically and only the key
    moves. A legacy entry on some *other* profile's port is left alone — it still
    works, crom cannot tell whose it is (ports get recycled, so the ledger would answer
    confidently and sometimes wrongly), and it is renamed when that profile is next
    wired. The key is derived before the drop, so a ref `entry_key` refuses leaves the
    legacy entry wired rather than dropping it for a replacement that never arrives.

    Hands back what became of that entry instead of saying so itself, because a sentence
    for a user is the CLI's job and this is the only place the fact exists — once the
    file is written, `path` no longer records that anything was renamed.
    [LAW:effects-at-boundaries] A caller reconstructing it by reading `path` first would
    be a second source for an event decided here, and a weaker one: it cannot tell an
    entry that was renamed from one deliberately left alone. [LAW:one-source-of-truth]
    """
    if path.exists():
        try:
            config = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise Reason.MCP_CONFIG_INVALID.error(
                f"{path} exists but is not valid JSON: {e}"
            ) from e
        if not isinstance(config, dict):
            raise Reason.MCP_CONFIG_INVALID.error(
                f"{path} must contain a JSON object, got {type(config).__name__}"
            )
        servers = config.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            raise Reason.MCP_CONFIG_INVALID.error(
                f'{path}: "mcpServers" must be an object, got {type(servers).__name__}'
            )
    else:
        config = {}
        servers = {}

    key = entry_key(profile.ref)
    entry = server_entry(profile.port)
    legacy = Legacy.of(servers, entry)
    # [LAW:dataflow-not-control-flow] the filter runs on every write; whether a legacy
    # entry is there to drop is a fact about the data, not a branch in the mechanics. It
    # reads the answer above rather than asking again.
    servers = {n: e for n, e in servers.items() if (n, legacy) != (LEGACY_KEY, Legacy.REPLACED)}
    servers[key] = entry
    config["mcpServers"] = servers
    path.write_text(json.dumps(config, indent=2) + "\n")
    return legacy
