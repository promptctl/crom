"""Generates the .mcp.json entry that points chrome-devtools-mcp at a given CDP port."""

import json
from pathlib import Path

SERVER_NAME = "chrome-devtools-mcp"


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


def write(port: int, path: Path) -> None:
    """Merge the chrome-devtools-mcp server entry for `port` into `path`.

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

    servers[SERVER_NAME] = server_entry(port)
    path.write_text(json.dumps(config, indent=2) + "\n")
