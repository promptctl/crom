"""Profile config management — single source of truth in ~/.config/crom/profiles.json"""

import json
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "crom"
CONFIG_FILE = CONFIG_DIR / "profiles.json"
STATE_DIR = Path.home() / ".local" / "state" / "crom"
CHROME_SRC = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"


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
