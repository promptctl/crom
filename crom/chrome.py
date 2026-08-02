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
import urllib.error
import urllib.request
from pathlib import Path

from .profiles import CHROME_SRC, profile_port, profile_state_dir

CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# How every crom-managed Chrome launches, independent of *which* profile: one quiet,
# non-phone-home, no-upsell launch policy applied identically on every launch.
# [LAW:one-source-of-truth] this list is the sole owner of that policy;
# [LAW:dataflow-not-control-flow] it is data spread into argv, not branches in launch().
#
# The top-level switches are long-stable Chrome/Chromium command-line switches. The
# trailing --disable-features entries are the version-fragile part: Chrome silently
# ignores feature names it no longer knows, so new promo/upsell surfaces get suppressed
# by adding a name there, not by touching launch().
LAUNCH_POLICY_FLAGS = [
    # "Don't check for default browser" — suppress the default-browser nag.
    "--no-default-browser-check",

    # "Don't send telemetry." No single switch does this; --disable-background-networking
    # is the big one (kills UMA metrics upload, field-trial fetches, and component /
    # safe-browsing update pings at once), and the rest close the remaining back-channels.
    "--disable-background-networking",
    "--disable-breakpad",            # crash-report upload
    "--disable-domain-reliability",  # network-error reports to Google
    "--no-pings",                    # hyperlink-auditing pings

    # "Don't register a profile / sign-in junk" — skip the first-run welcome/registration
    # flow and the account sync machinery entirely.
    "--no-first-run",
    "--disable-sync",

    # "Don't try to sell me things" — the upsell surfaces. --disable-search-engine-choice-screen
    # kills the search-engine chooser; ChromeWhatsNewUI is the post-update "What's New" promo tab.
    "--disable-search-engine-choice-screen",
    "--disable-features=ChromeWhatsNewUI",
]


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


def _cdp_ready(port: int) -> bool:
    """True once Chrome's CDP HTTP endpoint answers on this port.

    We probe the endpoint we intend to use rather than Chrome's DevToolsActivePort
    file: Chrome only writes that file to *report* a port it chose itself (the
    `--remote-debugging-port=0` case), not when we hand it a fixed port. The live
    endpoint is the honest readiness signal.
    """
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=1
        ) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def launch(name: str) -> int:
    """Launch Chrome for this profile on its stable CDP port and return it.

    The port comes from the profile config (profile_port), so it is the same on
    every launch — the contract a client config can rely on. We launch on that
    port, then poll the CDP endpoint until it answers. [LAW:no-silent-failure]
    if it never comes up (e.g. the port is already in use), we raise rather than
    return a lie.
    """
    state_dir = profile_state_dir(name)
    state_dir.mkdir(parents=True, exist_ok=True)
    port = profile_port(name)
    subprocess.Popen(
        [
            CHROME_BIN,
            *LAUNCH_POLICY_FLAGS,
            f"--user-data-dir={state_dir}",
            f"--remote-debugging-port={port}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.time() + 30.0
    while time.time() < deadline:
        if _cdp_ready(port):
            return port
        time.sleep(0.1)
    raise RuntimeError(
        f"Chrome did not open CDP port {port} for '{name}' within 30s "
        f"(is port {port} already in use?)"
    )


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
