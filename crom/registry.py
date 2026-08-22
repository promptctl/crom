"""Owns the machine-wide ledger of which CDP port belongs to which profile.

[LAW:no-shared-mutable-globals] Ports are a shared resource across every project on the
machine, so they get one owner with an explicit API rather than each config picking a
number and hoping. [LAW:one-source-of-truth] a config file may *pin* a port, and then
the config is authority and the ledger is a reservation derived from it, rewritten on
every load; an unpinned profile is assigned once by the ledger and reads back forever.

Every read-modify-write runs under an exclusive lock on the ledger file, because the
case crom exists for — several agents each bringing up their own browser — is exactly
the case where two `crom up` calls race for the same free port.
"""

import contextlib
import fcntl
import json
import os
import socket
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .model import Conflict, ProfileRef
from .paths import registry_file

SCHEMA_VERSION = 2

# `user/default` anchors on Chrome's conventional debugging port so the common case
# needs no lookup at all; everything else takes the lowest free port above it.
BASE_PORT = 9222
MAX_PORT = 65535


@dataclass(frozen=True)
class Reservation:
    port: int
    pinned: bool
    source: str | None


def _empty() -> dict:
    return {"version": SCHEMA_VERSION, "ports": {}, "namespaces": {}}


@contextlib.contextmanager
def _locked() -> Iterator[Path]:
    """Hold an exclusive lock on the ledger for the duration of a read-modify-write."""
    path = registry_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    with open(lock_path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield path
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _read(path: Path) -> dict:
    if not path.exists():
        return _empty()
    data = json.loads(path.read_text())
    if data.get("version") != SCHEMA_VERSION:
        raise Conflict(
            f"{path}: unsupported registry version {data.get('version')!r} "
            f"(this crom speaks version {SCHEMA_VERSION})"
        )
    return data


def _write(path: Path, data: dict) -> None:
    """Replace the ledger atomically so a crash can never leave a half-written file."""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _port_is_free(port: int) -> bool:
    """True when nothing on this machine currently holds the port.

    The ledger only knows about ports crom handed out; an unrelated dev server on 9222
    is invisible to it. Binding is the honest check.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def reservations() -> dict[str, Reservation]:
    with _locked() as path:
        data = _read(path)
    return {
        ref: Reservation(entry["port"], entry.get("pinned", False), entry.get("source"))
        for ref, entry in data["ports"].items()
    }


def namespaces() -> dict[str, Path]:
    """Every project namespace crom has seen, mapped to the config file that declares it.

    This index is what makes a namespace a *global* address: `crom up worker-a/dev`
    works from any directory, because the ledger remembers where worker-a lives.
    """
    with _locked() as path:
        data = _read(path)
    return {name: Path(entry["config"]) for name, entry in data["namespaces"].items()}


def remember_namespace(namespace: str, source: Path) -> None:
    with _locked() as path:
        data = _read(path)
        data["namespaces"][namespace] = {"config": str(source)}
        _write(path, data)


def forget_namespace(namespace: str) -> int:
    """Drop a namespace and every port reserved under it; report how many were released."""
    prefix = f"{namespace}/"
    with _locked() as path:
        data = _read(path)
        released = [key for key in data["ports"] if key.startswith(prefix)]
        for key in released:
            del data["ports"][key]
        data["namespaces"].pop(namespace, None)
        _write(path, data)
    return len(released)


def forget(ref: ProfileRef) -> None:
    with _locked() as path:
        data = _read(path)
        data["ports"].pop(str(ref), None)
        _write(path, data)


def adopt(ref: ProfileRef, port: int, source: Path | None) -> None:
    """Record a port crom already handed out, without re-allocating it.

    Used by migration: profiles that predate the ledger's current shape keep the exact
    ports they have been running on, because `.mcp.json` files and app configs out in
    the world already point at them.
    """
    key = str(ref)
    with _locked() as path:
        data = _read(path)
        _reject_foreign_claim(key, port, data["ports"])
        data["ports"][key] = {"port": port, "pinned": False, "source": str(source) if source else None}
        _write(path, data)


def port_for(ref: ProfileRef, *, pinned: int | None, source: Path | None) -> int:
    """The port for this profile: the pinned one, or one assigned once and remembered.

    Assignment is idempotent — the first call picks and persists, every later call reads
    back the same number — which is the whole contract a checked-in `.mcp.json` or an
    app's `CDP_URL` relies on.
    """
    key = str(ref)
    with _locked() as path:
        data = _read(path)
        ports: dict[str, dict] = data["ports"]

        port = pinned if pinned is not None else ports.get(key, {}).get("port")
        if port is None:
            port = _allocate(ref, ports)

        _reject_foreign_claim(key, port, ports)
        ports[key] = {
            "port": port,
            "pinned": pinned is not None,
            "source": str(source) if source else None,
        }
        _write(path, data)
        return port


def _reject_foreign_claim(key: str, port: int, ports: dict[str, dict]) -> None:
    for other_key, entry in ports.items():
        if other_key == key or entry["port"] != port:
            continue
        origin = entry.get("source") or "assigned by crom"
        raise Conflict(
            f"port {port} is already held by profile '{other_key}' ({origin}).\n"
            f"Change the `port` for '{key}', or remove it to let crom assign a free one."
        )


def _allocate(ref: ProfileRef, ports: dict[str, dict]) -> int:
    taken = {entry["port"] for entry in ports.values()}
    # BASE_PORT is held for user/default whether or not it has been created yet, so a
    # project profile never steals the port a bare `crom up` will want.
    preferred = BASE_PORT if str(ref) == "user/default" else BASE_PORT + 1
    reserved = taken if str(ref) == "user/default" else taken | {BASE_PORT}

    for port in range(preferred, MAX_PORT + 1):
        if port not in reserved and _port_is_free(port):
            return port
    raise Conflict(f"no free port available at or above {preferred} for '{ref}'")
