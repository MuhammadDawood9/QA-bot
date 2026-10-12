"""The app's own settings, in the OS config folder (never in the code or the project):

    Windows   %LOCALAPPDATA%\\VideoQCBot\\settings.json
    macOS     ~/Library/Application Support/VideoQCBot/settings.json

Saved as UTF-8 JSON, written to a temporary file first so a crash never leaves half a file. A
missing or damaged file just means the defaults.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path

import platformdirs

log = logging.getLogger("qcbot.app")

APP_NAME = "VideoQCBot"

DEFAULTS: dict = {
    "window": {"x": None, "y": None},       # the mascot's position; None = bottom-right of the main screen
}


def config_dir() -> Path:
    return platformdirs.user_config_path(APP_NAME, appauthor=False, roaming=False)


def log_dir() -> Path:
    return platformdirs.user_log_path(APP_NAME, appauthor=False)


class Settings:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else config_dir() / "settings.json"
        self.data = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        try:
            with self.path.open(encoding="utf-8") as f:
                stored = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("settings file %s unreadable (%s); using defaults", self.path, exc)
            return
        if isinstance(stored, dict):
            for key, value in stored.items():
                if isinstance(value, dict) and isinstance(self.data.get(key), dict):
                    self.data[key].update(value)
                else:
                    self.data[key] = value

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8", newline="\n") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("couldn't save settings to %s: %s", self.path, exc)

    @property
    def window_position(self) -> tuple[int, int] | None:
        x, y = self.data["window"].get("x"), self.data["window"].get("y")
        return (int(x), int(y)) if isinstance(x, (int, float)) and isinstance(y, (int, float)) else None

    @window_position.setter
    def window_position(self, pos: tuple[int, int]) -> None:
        self.data["window"]["x"], self.data["window"]["y"] = int(pos[0]), int(pos[1])
