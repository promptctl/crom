"""Generates the .mcp.json entry that points chrome-devtools-mcp at a given profile."""

import json
from pathlib import Path

from .model import ProfileRef, ResolvedProfile

# What marks an entry in `.mcp.json` as crom's, and the only part of the key crom
# chooses rather than derives. Four characters because the rest of the key is a budget:
# Claude Code exposes an MCP tool to the model as `mcp__<key>__<tool>` and holds the
# result to `^[A-Za-z0-9_-]{1,64}$`, so a key of 30-odd characters is all that fits
# beside chrome-devtools-mcp's longest tool name, `performance_analyze_insight`. The
# obvious prefix — the server's own `chrome-devtools-mcp` — spends 19 of those before
# naming any profile at all. The command below still says which server this runs.
KEY_PREFIX = "crom"

# How a namespace or a profile name is spelled inside the key. Two substitutions,
# because a name matches `^[a-z0-9][a-z0-9._-]*$` and the key alphabet is only
# `[a-z0-9_-]`: `.` has nowhere to land, and `_` has to move aside so that `__` can
# separate the two components without a name ever producing one. `-` is deliberately
# absent — it is the character names actually use, and it survives untouched.
_ESCAPES = {"_": "_u", ".": "_d"}


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
    return f"{KEY_PREFIX}__{namespace}__{name}"


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


def write(profile: ResolvedProfile, path: Path) -> None:
    """Merge `profile`'s chrome-devtools-mcp server entry into `path`.

    Takes the whole profile rather than a ref and a port because those are one fact:
    [LAW:types-are-the-program] a `write(ref, port, path)` would let a caller key an
    entry to one profile and point it at another profile's port, and nothing downstream
    could tell that it had.

    Preserves any other servers already declared in `path`. Raises ValueError
    if `path` exists and its content isn't a JSON object we can merge into
    (invalid JSON, a non-object root, or a non-object "mcpServers") — we
    never overwrite a file we can't parse into that shape.
    """
    if path.exists():
        try:
            config = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise ValueError(f"{path} exists but is not valid JSON: {e}") from e
        if not isinstance(config, dict):
            raise ValueError(f"{path} must contain a JSON object, got {type(config).__name__}")
        servers = config.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            raise ValueError(f'{path}: "mcpServers" must be an object, got {type(servers).__name__}')
    else:
        config = {}
        servers = config.setdefault("mcpServers", {})

    servers[entry_key(profile.ref)] = server_entry(profile.port)
    path.write_text(json.dumps(config, indent=2) + "\n")
