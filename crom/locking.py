"""Serializes the read-modify-write of a file two crom processes can both reach.

Several agents each bringing up their own browser is the case crom exists for, so every
file crom edits in place — the port ledger, a config file a human also owns, a profile
directory being materialized — has more than one possible writer.

[LAW:single-enforcer] One implementation of "hold an exclusive lock across a
read-modify-write" serves all of them. A second copy of the flock dance would be a
second rulebook, and the two would drift.

POSIX only, like the rest of crom: `chrome.scan` shells out to `ps`, so there is no
platform where a Windows lock would have anything to protect.
"""

import contextlib
import fcntl
from collections.abc import Iterator
from pathlib import Path


@contextlib.contextmanager
def exclusive(path: Path) -> Iterator[None]:
    """Hold an exclusive lock keyed on `path` until the block exits.

    The lock lives in a companion dotfile rather than on `path` itself, because the
    protected thing frequently does not exist yet — a config file being created, a
    profile directory being seeded — and locking it directly would race with its own
    creation. Keying on the name means every process derives the same lock file from
    the same target without coordinating.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / f".{path.name}.lock"
    with open(lock_path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
