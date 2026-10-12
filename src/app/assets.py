"""Where the app's images are, and loading them sharp on every screen.

Images live in assets/ next to src/ (in a packaged app: in the bundle). Each image can come in
1x and 2x versions: idle.png and idle@2x.png. Qt picks the 2x one on Retina and high-DPI screens.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

log = logging.getLogger("qcbot.app")

STATES = ("idle", "working", "done", "error")


def assets_dir() -> Path:
    # PyInstaller unpacks bundled files under sys._MEIPASS.
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent.parent))
    return base / "assets"


def load_icon(path_1x: Path):
    """A QIcon with the 1x image and, if present, its @2x version. Empty if neither exists."""
    from PySide6.QtGui import QIcon

    icon = QIcon()
    path_2x = path_1x.with_name(f"{path_1x.stem}@2x{path_1x.suffix}")
    for path in (path_1x, path_2x):
        if path.is_file():
            icon.addFile(str(path))
    if icon.isNull():
        log.warning("image missing: %s", path_1x)
    return icon


def mascot_icons(folder: Path | None = None) -> dict:
    """state -> QIcon, from assets/mascot/<state>.png (and <state>@2x.png)."""
    folder = folder or assets_dir() / "mascot"
    return {state: load_icon(folder / f"{state}.png") for state in STATES}
