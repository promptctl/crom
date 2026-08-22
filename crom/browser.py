"""Locates the Chrome executable on this platform.

[LAW:no-silent-failure] When no Chrome is found we name every path we tried and point
at the config key that overrides the search, rather than failing later inside Popen
with a bare ENOENT.
"""

import shutil
import sys
from pathlib import Path

from .model import CromError

# Checked in order. macOS installs to a fixed app-bundle path; Linux distributions vary,
# so we ask PATH. [LAW:dataflow-not-control-flow] platform differences are data in this
# table, not branches in the search.
_CANDIDATES: dict[str, tuple[str, ...]] = {
    "darwin": (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ),
    "linux": (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    ),
    "win32": (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ),
}


def _resolve(candidate: str) -> Path | None:
    """A candidate is either an absolute path that exists or a name found on PATH."""
    path = Path(candidate)
    if path.is_absolute():
        return path if path.exists() else None
    found = shutil.which(candidate)
    return Path(found) if found else None


def find_chrome() -> Path:
    candidates = _CANDIDATES.get(sys.platform, _CANDIDATES["linux"])
    for candidate in candidates:
        resolved = _resolve(candidate)
        if resolved:
            return resolved
    tried = "\n  ".join(candidates)
    raise CromError(
        f"no Chrome executable found on {sys.platform}. Tried:\n  {tried}\n"
        f"Set chrome_binary in your crom config to point at it explicitly."
    )
