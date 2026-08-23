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
import json
import os
import socket
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .locking import exclusive
from .model import Conflict, CromError, ProfileRef
from .paths import USER_NAMESPACE, registry_file

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
    with exclusive(path):
        yield path


def _read(path: Path) -> dict:
    """Load the ledger, or fail naming the file a human has to repair.

    [LAW:no-silent-failure] Both ways a ledger can be unusable — unparseable, or written
    by a crom that speaks a later schema — surface as `CromError`, so they reach the
    user through the CLI's documented exit-code contract instead of escaping as a raw
    traceback. Every command touches the ledger, so an uncaught raise here takes the
    whole tool down with no indication of which file is at fault.

    A `CromError` and not a `Conflict`: `Conflict` means two declarations claim one
    resource and maps to exit 4. A ledger crom cannot read is neither.
    """
    if not path.exists():
        return _empty()
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise CromError(
            f"{path}: the port ledger is not valid JSON ({e}).\n"
            f"Repair or delete the file; crom rebuilds it, but every profile then gets "
            f"a freshly assigned port."
        ) from e
    if data.get("version") != SCHEMA_VERSION:
        raise CromError(
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
    """Record which config file owns a namespace, refusing a second claimant.

    A namespace is free text a user types, checked for legal characters and nothing
    else, so two unrelated projects can both pick `app`. Left unchecked they would share
    a `ProfileRef` — and therefore one ledger key and one profile directory, mixing two
    projects' cookies and logins in the same Chrome data dir. That is the exact bleed
    namespaces exist to prevent, so the second claimant is refused by name rather than
    silently becoming the owner.
    """
    with _locked() as path:
        data = _read(path)
        recorded = data["namespaces"].get(namespace, {}).get("config")
        if recorded is not None and recorded != str(source):
            raise Conflict(
                f"namespace '{namespace}' is already claimed by {recorded}.\n"
                f"{source} cannot use it too — they would share profile directories and "
                f"ports. Rename this project's `namespace`, or run "
                f"`crom forget {namespace}` if {recorded} is gone."
            )
        data["namespaces"][namespace] = {"config": str(source)}
        _write(path, data)


def forget_namespace(namespace: str) -> int:
    """Drop a namespace and every port reserved under it; report how many were released.

    [LAW:single-enforcer] The `user` namespace is refused here, at the ledger that owns
    the reservations, rather than at each entry point — `config.parse` and `crom init`
    already reject it on their own paths, and a third copy of the rule in `forget_cmd`
    would be a third chance to drift. Dropping `user/` would silently release the port
    reservations behind personal profiles, which are declared, in use, and would come
    back on different numbers.
    """
    if namespace == USER_NAMESPACE:
        raise Conflict(
            f"namespace '{USER_NAMESPACE}' is reserved for your personal profiles and "
            f"cannot be forgotten — dropping it would release the ports they are using. "
            f"Remove individual profiles with `crom rm {USER_NAMESPACE}/<name>`."
        )
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
    _reject_base_port_pin(key, pinned)
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


# Built through ProfileRef rather than spelled out, so the "namespace/name" format has
# one owner — `ProfileRef.__str__` — and this cannot drift from the keys the ledger is
# actually written with. [LAW:one-source-of-truth]
_DEFAULT_REF = str(ProfileRef(USER_NAMESPACE, "default"))


def _reject_base_port_pin(key: str, pinned: int | None) -> None:
    """Keep BASE_PORT for `user/default`, on the pinned path as well as the assigned one.

    `_allocate` holds 9222 back from auto-assignment, but a config pinning `port = 9222`
    took the other branch and skipped that reservation entirely — after which a bare
    `crom` quietly lands on 9223 and the documented "the common case needs no lookup at
    all" guarantee stops being true, with nothing reporting that it changed.
    """
    if pinned == BASE_PORT and key != _DEFAULT_REF:
        raise Conflict(
            f"port {BASE_PORT} is reserved for '{_DEFAULT_REF}', which a bare `crom` "
            f"expects to find there without a lookup.\n"
            f"Pin a different port for '{key}', or remove the pin to let crom assign one."
        )


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
    preferred = BASE_PORT if str(ref) == _DEFAULT_REF else BASE_PORT + 1
    reserved = taken if str(ref) == _DEFAULT_REF else taken | {BASE_PORT}

    for port in range(preferred, MAX_PORT + 1):
        if port not in reserved and _port_is_free(port):
            return port
    raise Conflict(f"no free port available at or above {preferred} for '{ref}'")
