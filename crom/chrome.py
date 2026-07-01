"""Chrome launch/kill — all process management lives here.

[LAW:one-source-of-truth] The OS process table is the sole authority on
"is this profile running." We identify a crom-managed Chrome by the
absolute `--user-data-dir` path it was launched with — no pidfiles, no
shadow state that can drift from reality.
"""

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from .profiles import CHROME_SRC, profile_state_dir

CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def copy_profile(name: str) -> Path:
    dest = profile_state_dir(name)
    if dest.exists():
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(CHROME_SRC / "Default", dest / "Default")
    return dest


def _find_main_pids(name: str) -> list[int]:
    """Return PIDs of the main browser process(es) for this profile.

    Matches the full command line for `--user-data-dir=<state_dir>` and
    excludes Chrome helper processes (which carry `--type=...`).
    """
    state_dir = profile_state_dir(name)
    needle = f"--user-data-dir={state_dir}"
    # macOS BSD `pgrep` doesn't support -a (print cmdline), so we use
    # `ps` and filter in Python. This is the portable path and gives us
    # the full argv to distinguish main browser from helper processes.
    result = subprocess.run(
        ["ps", "-Ao", "pid=,command="],
        capture_output=True,
        text=True,
        check=True,
    )
    pids: list[int] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_str, _, cmd = line.partition(" ")
        if not pid_str.isdigit():
            continue
        if needle not in cmd:
            continue
        if "--type=" in cmd:  # helper/renderer/gpu — not the main process
            continue
        pids.append(int(pid_str))
    return pids


def debug_port(name: str) -> int | None:
    """The CDP port Chrome chose, read from the DevToolsActivePort file it
    writes into the profile dir. None when the file is absent (not running,
    or a running instance that carries no remote-debugging port).
    """
    port_file = profile_state_dir(name) / "DevToolsActivePort"
    try:
        return int(port_file.read_text().splitlines()[0])
    except (FileNotFoundError, IndexError, ValueError):
        return None


def launch(name: str) -> int:
    """Launch Chrome for this profile and return the CDP port it chose.

    `--remote-debugging-port=0` tells Chrome to bind a free port itself and
    record it in <user-data-dir>/DevToolsActivePort. We clear any stale file
    first, then poll until Chrome writes the fresh one. [LAW:no-silent-failure]
    if the port never appears, we raise rather than return a lie.
    """
    state_dir = profile_state_dir(name)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "DevToolsActivePort").unlink(missing_ok=True)
    subprocess.Popen(
        [
            CHROME_BIN,
            f"--user-data-dir={state_dir}",
            "--remote-debugging-port=0",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.time() + 10.0
    while time.time() < deadline:
        port = debug_port(name)
        if port is not None:
            return port
        time.sleep(0.05)
    raise RuntimeError(f"Chrome did not report a debug port for '{name}' within 10s")


def kill(name: str) -> int | None:
    """Terminate all main Chrome processes bound to this profile.

    Returns the first PID killed, or None if nothing was running.
    """
    pids = _find_main_pids(name)
    if not pids:
        return None
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    # Give Chrome a moment to shut down gracefully, then SIGKILL stragglers.
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if not _find_main_pids(name):
            return pids[0]
        time.sleep(0.1)
    for pid in _find_main_pids(name):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return pids[0]


def is_running(name: str) -> bool:
    return bool(_find_main_pids(name))
