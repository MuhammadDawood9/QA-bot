"""The app's version, and "Check for update" against latest.json in the shared folder.

latest.json (written by whoever publishes a new build; see docs/IT_DEPLOYMENT.md):

    {
      "version": "0.6.0",
      "windows": "\\\\qcserver\\QC\\VideoQCBot\\installers\\VideoQCBot-0.6.0-windows.zip",
      "macos": "/Volumes/QC/VideoQCBot/installers/VideoQCBot-0.6.0-macos.zip",
      "notes": "Audio checks for podcasts."
    }

Nothing is downloaded or installed automatically: the user is told where the new version is.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

__version__ = "0.6.0"


def parse_version(text: str) -> tuple[int, ...]:
    """"0.6.0" -> (0, 6, 0); "v1.2" -> (1, 2); anything after the numbers is ignored."""
    m = re.match(r"\s*v?(\d+(?:\.\d+)*)", str(text))
    return tuple(int(p) for p in m.group(1).split(".")) if m else (0,)


@dataclass
class UpdateInfo:
    version: str
    installer: str            # where this OS's installer is ("" if latest.json doesn't say)
    notes: str = ""


def os_key() -> str:
    return "windows" if sys.platform == "win32" else "macos" if sys.platform == "darwin" else "other"


def check_for_update(latest_json: Path, current: str = __version__) -> UpdateInfo | None:
    """The newer version described by latest.json, or None when it's missing, unreadable or not newer."""
    try:
        data = json.loads(Path(latest_json).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("version"):
        return None
    if parse_version(data["version"]) <= parse_version(current):
        return None
    return UpdateInfo(version=str(data["version"]), installer=str(data.get(os_key()) or data.get("installer") or ""),
                      notes=str(data.get("notes") or ""))
