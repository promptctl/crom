"""Profile config management — single source of truth in ~/.config/crom/profiles.json"""

import json
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "crom"
CONFIG_FILE = CONFIG_DIR / "profiles.json"
STATE_DIR = Path.home() / ".local" / "state" / "crom"
CHROME_SRC = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"

# The "default" profile anchors on the conventional Chrome debugging port; every
# other profile is allocated the lowest free port above it.
CDP_BASE_PORT = 9222


def _load() -> dict:
    if not CONFIG_FILE.exists():
        return {"profiles": {}}
    return json.loads(CONFIG_FILE.read_text())


def _save(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2))


def list_profiles() -> dict[str, dict]:
    return _load()["profiles"]


def get_profile(name: str) -> dict | None:
    return _load()["profiles"].get(name)


def profile_port(name: str) -> int:
    """The stable CDP port assigned to a profile.

    [LAW:one-source-of-truth] A profile's port lives in profiles.json and is
    allocated exactly once, then never changes — so any consumer (Chrome's
    launch args, an MCP client config) references one fixed value instead of a
    port that moves on every launch. `default` is pinned to CDP_BASE_PORT; every
    other profile takes the lowest free port above it, collision-free by
    construction so distinct profiles can run concurrently. Allocation is
    idempotent: the first call assigns and persists, every later call reads back.
    """
    data = _load()
    entries = data["profiles"]
    if name not in entries:
        raise KeyError(f"Profile '{name}' not found")
    entry = entries[name]
    if "port" in entry:
        return entry["port"]

    if name == "default":
        port = CDP_BASE_PORT
    else:
        used = {e["port"] for e in entries.values() if "port" in e}
        used.add(CDP_BASE_PORT)  # reserved for "default"
        port = CDP_BASE_PORT + 1
        while port in used:
            port += 1
    entry["port"] = port
    _save(data)
    return port


def add_profile(name: str) -> dict:
    data = _load()
    if name in data["profiles"]:
        raise ValueError(f"Profile '{name}' already exists")
    data["profiles"][name] = {}
    _save(data)
    return data["profiles"][name]


def remove_profile(name: str) -> None:
    data = _load()
    if name not in data["profiles"]:
        raise KeyError(f"Profile '{name}' not found")
    del data["profiles"][name]
    _save(data)


def profile_state_dir(name: str) -> Path:
    return STATE_DIR / name


def ensure_default() -> None:
    data = _load()
    if "default" not in data["profiles"]:
        data["profiles"]["default"] = {}
        _save(data)


def pid_file(name: str) -> Path:
    return STATE_DIR / f"{name}.pid"
