"""Where the app's images are, mascot skins, and loading images sharp on every screen.

Images live in assets/ next to src/ (in a packaged app: in the bundle). Each image can come in 1x
and 2x versions (idle.png, idle@2x.png); Qt picks the 2x one on Retina and high-DPI screens.

A skin is a folder assets/mascot/<skin>/ with idle, working, done and error images (120 x 120,
transparent background, no glow: the app draws that) and a skin.json:

    {"name": "Inspector", "eyes": "frames"}
        each <state>.png has eyes; <state>_blink.png is the same with them closed (optional per
        state: no blink frame, no blink in that state)

    {"name": "Cloud", "eyes": "drawn",
     "eye_shape": {"left": [46, 64], "right": [74, 64], "radius": 6.5,
                   "color": "#24303C", "highlight": "#FFFFFF"},
     "state_eyes": {"idle": "open", "working": "focused", "done": "happy", "error": "cross"}}
        the images have no eyes; the app paints them at those positions (120 x 120 sprite
        coordinates). Styles: open, focused, happy, cross; a blink draws them closed.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("qcbot.app")

STATES = ("idle", "working", "done", "error")
DEFAULT_SKIN = "inspector"
EYE_STYLES = ("open", "focused", "happy", "cross")
DEFAULT_STATE_EYES = {"idle": "open", "working": "focused", "done": "happy", "error": "cross"}
DEFAULT_EYE_SHAPE = {"left": [46, 64], "right": [74, 64], "radius": 6.5, "color": "#24303C", "highlight": "#FFFFFF"}


def assets_dir() -> Path:
    # PyInstaller unpacks bundled files under sys._MEIPASS.
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent.parent))
    return base / "assets"


def load_icon(path_1x: Path, warn: bool = True):
    """A QIcon with the 1x image and, if present, its @2x version. Empty if neither exists."""
    from PySide6.QtGui import QIcon

    icon = QIcon()
    path_2x = path_1x.with_name(f"{path_1x.stem}@2x{path_1x.suffix}")
    for path in (path_1x, path_2x):
        if path.is_file():
            icon.addFile(str(path))
    if icon.isNull() and warn:
        log.warning("image missing: %s", path_1x)
    return icon


@dataclass
class Skin:
    key: str                                   # folder name
    name: str                                  # shown in the menu
    eyes: str                                  # "frames" or "drawn"
    folder: Path
    icons: dict = field(default_factory=dict)          # state -> QIcon
    blink_icons: dict = field(default_factory=dict)    # state -> QIcon (frames skins; only states that have one)
    eye_shape: dict = field(default_factory=lambda: dict(DEFAULT_EYE_SHAPE))
    state_eyes: dict = field(default_factory=lambda: dict(DEFAULT_STATE_EYES))

    def can_blink(self, state: str) -> bool:
        return self.eyes == "drawn" or state in self.blink_icons


def mascot_dir() -> Path:
    return assets_dir() / "mascot"


def available_skins(base: Path | None = None) -> list[str]:
    """Skin folders (they have a skin.json), sorted, the default first."""
    base = base or mascot_dir()
    keys = sorted(p.name for p in base.iterdir() if p.is_dir() and (p / "skin.json").is_file()) if base.is_dir() else []
    return sorted(keys, key=lambda k: (k != DEFAULT_SKIN, k))


def read_skin_json(path: Path) -> dict:
    """skin.json with every value checked; anything wrong is logged and replaced by its default."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as exc:
        log.warning("%s unreadable (%s); using defaults", path, exc)
        data = {}
    eyes = data.get("eyes", "frames")
    if eyes not in ("frames", "drawn"):
        log.warning("%s: eyes must be \"frames\" or \"drawn\", not %r; using frames", path, eyes)
        eyes = "frames"
    shape = dict(DEFAULT_EYE_SHAPE)
    given = data.get("eye_shape") or {}
    for key in ("left", "right"):
        point = given.get(key)
        if isinstance(point, list) and len(point) == 2 and all(isinstance(v, (int, float)) for v in point):
            shape[key] = point
        elif point is not None:
            log.warning("%s: eye_shape.%s must be [x, y]", path, key)
    if isinstance(given.get("radius"), (int, float)) and given["radius"] > 0:
        shape["radius"] = given["radius"]
    for key in ("color", "highlight"):
        if isinstance(given.get(key), str):
            shape[key] = given[key]
    state_eyes = dict(DEFAULT_STATE_EYES)
    for state, style in (data.get("state_eyes") or {}).items():
        if state in STATES and style in EYE_STYLES:
            state_eyes[state] = style
        else:
            log.warning("%s: state_eyes %r: %r ignored (styles: %s)", path, state, style, ", ".join(EYE_STYLES))
    return {"name": str(data.get("name") or path.parent.name), "eyes": eyes, "eye_shape": shape,
            "state_eyes": state_eyes}


def load_skin(key: str | None = None, base: Path | None = None) -> Skin:
    """The named skin; the default (or the first one there is) if it doesn't exist."""
    base = base or mascot_dir()
    skins = available_skins(base)
    if key not in skins:
        if key:
            log.warning("skin %r not found; using %s", key, skins[0] if skins else "the drawn fallback")
        key = skins[0] if skins else DEFAULT_SKIN
    folder = base / key
    meta = read_skin_json(folder / "skin.json") if (folder / "skin.json").is_file() else read_skin_json(Path("-"))
    skin = Skin(key=key, name=meta["name"], eyes=meta["eyes"], folder=folder, eye_shape=meta["eye_shape"],
                state_eyes=meta["state_eyes"])
    for state in STATES:
        skin.icons[state] = load_icon(folder / f"{state}.png")
        if skin.eyes == "frames":
            blink = load_icon(folder / f"{state}_blink.png", warn=False)
            if not blink.isNull():
                skin.blink_icons[state] = blink
    return skin


def mascot_icons(folder: Path | None = None) -> dict:
    """state -> QIcon from one folder (kept for callers that only need the images)."""
    folder = folder or mascot_dir() / DEFAULT_SKIN
    return {state: load_icon(folder / f"{state}.png") for state in STATES}
