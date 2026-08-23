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

from .model import CromError


@contextlib.contextmanager
def exclusive(path: Path) -> Iterator[None]:
    """Hold an exclusive lock keyed on `path` until the block exits.

    The lock lives in a companion dotfile rather than on `path` itself, because the
    protected thing frequently does not exist yet — a config file being created, a
    profile directory being seeded — and locking it directly would race with its own
    creation. Keying on the name means every process derives the same lock file from
    the same target without coordinating.
    """
    lock_path = path.parent / f".{path.name}.lock"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "w")
    except OSError as e:
        # This primitive sits under nearly every command, so a raw OSError here — an
        # unwritable parent, a regular file where a directory belongs — would escape as
        # a traceback from all of them. [LAW:no-silent-failure] name the path instead.
        raise CromError(f"could not take the lock at {lock_path}: {e}") from e

    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
        except OSError as e:
            # The guarantee has to cover acquisition, not just creation. Advisory locking
            # is not universally supported — a network or FUSE mount under a relocated
            # XDG_STATE_HOME can refuse it, and ENOLCK is reachable under resource
            # exhaustion — and this primitive sits under nearly every command, so a raw
            # OSError from here escapes as a traceback exactly as one from `open` did.
            raise CromError(f"could not take the lock at {lock_path}: {e}") from e
        try:
            yield
        finally:
            # Deliberately swallowed. Closing the descriptor — which `with handle`
            # guarantees, on every path out — releases the lock by itself, so this call
            # is belt-and-braces and its failure changes nothing. Raising here would
            # replace an exception already travelling out of the body with a less
            # informative one about the cleanup, losing the failure the caller needs.
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            except OSError:
                pass
