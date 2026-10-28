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

    "shape": "circle"   (optional; default "free") a round badge: every image is masked to the circle
        inscribed in its square when loaded (white or ragged corners disappear), hover/click/drag/
        drop respond only inside the circle, and the glow is a halo just outside its edge with a
        thin bright ring on the border.

    "glow": {...}       (optional) colors, strengths and timings of the glow; see DEFAULT_GLOW. Missing
        or invalid values fall back to the defaults, so a skin can set just one color. The glow
        colors also color the app's menus and windows (src/app/theme.py).

    "badge": "#RRGGBB"  (optional) the issue-count badge and the progress ring; default: the
        "working" glow color
"""

from __future__ import annotations

import copy
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("qcbot.app")

STATES = ("idle", "working", "done", "error")
DEFAULT_SKIN = "avatar"
EYE_STYLES = ("open", "focused", "happy", "cross")
DEFAULT_STATE_EYES = {"idle": "open", "working": "focused", "done": "happy", "error": "cross"}
DEFAULT_EYE_SHAPE = {"left": [46, 64], "right": [74, 64], "radius": 6.5, "color": "#24303C", "highlight": "#FFFFFF"}
SHAPES = ("free", "circle")

# The glow, drawn by the app. Distances are logical pixels (Qt doubles them on Retina / 200%).
#   width            how far the halo reaches beyond the circle edge
#   edge_opacity     the halo's opacity at the edge, at strength 1
#   ring_width       the thin bright ring on the border (circle skins), always at full strength
#   shadow_opacity   a faint dark shadow under the halo, so it still reads on a white wallpaper
#   idle / error     steady: color and strength (0-1)
#   working          pulse between strength [low, high] and scale [low, high], once every period_ms
#   done             a flash_ms bright pulse at full strength, then back to the idle glow by return_ms
DEFAULT_GLOW = {
    "width": 14,
    "edge_opacity": 0.85,
    "ring_width": 2,
    "shadow_opacity": 0.22,
    "idle": {"color": "#C08CF0", "strength": 0.4},
    "working": {"color": "#7C6CFF", "strength": [0.45, 1.0], "scale": [1.0, 1.1], "period_ms": 1400},
    "done": {"color": "#2ECC71", "strength": 0.9, "flash_ms": 450, "return_ms": 3000},
    "error": {"color": "#EB4034", "strength": 0.9},
}
_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


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
    shape: str = "free"                        # "free" or "circle"
    glow: dict = field(default_factory=lambda: copy.deepcopy(DEFAULT_GLOW))
    badge_color: str | None = None             # None: the "working" glow color

    @property
    def badge(self) -> str:
        return self.badge_color or self.glow["working"]["color"]

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
    outline = data.get("shape", "free")
    if outline not in SHAPES:
        log.warning("%s: shape must be \"free\" or \"circle\", not %r; using free", path, outline)
        outline = "free"
    badge = data.get("badge")
    if badge is not None and not (isinstance(badge, str) and _COLOR.match(badge)):
        log.warning("%s: badge must be a #RRGGBB color, not %r; using the working glow color", path, badge)
        badge = None
    return {"name": str(data.get("name") or path.parent.name), "eyes": eyes, "eye_shape": shape,
            "state_eyes": state_eyes, "shape": outline, "glow": read_glow(data.get("glow"), path), "badge": badge}


def _valid(value, default) -> bool:
    """value has the same kind as default: a #RRGGBB color, a [low, high] pair, a number >= 0
    (at most 1 where the default is a fraction)."""
    if isinstance(default, str):
        return isinstance(value, str) and bool(_COLOR.match(value))
    if isinstance(default, list):
        return (isinstance(value, list) and len(value) == 2 and all(_valid(v, d) for v, d in zip(value, default))
                and value[0] <= value[1])
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return False
    return value <= 1 if isinstance(default, float) and default <= 1 else True


def read_glow(given, path: Path) -> dict:
    """skin.json's "glow" over DEFAULT_GLOW: each valid value replaces its default."""
    glow = copy.deepcopy(DEFAULT_GLOW)
    if given is None:
        return glow
    if not isinstance(given, dict):
        log.warning("%s: glow must be an object; using the default glow", path)
        return glow
    for key, value in given.items():
        default = glow.get(key)
        if isinstance(default, dict) and isinstance(value, dict):
            for sub, v in value.items():
                if sub in default and _valid(v, default[sub]):
                    default[sub] = v
                else:
                    log.warning("%s: glow.%s.%s = %r ignored", path, key, sub, v)
        elif default is not None and not isinstance(default, dict) and _valid(value, default):
            glow[key] = value
        else:
            log.warning("%s: glow.%s = %r ignored", path, key, value)
    return glow


def circle_icon(icon):
    """The same icon with every size masked to the circle inscribed in its square (antialiased)."""
    from PySide6.QtCore import QRectF, QSize, Qt
    from PySide6.QtGui import QBrush, QIcon, QImage, QPainter, QPixmap

    result = QIcon()
    for size in sorted({s.width() for s in icon.availableSizes()} or {120}):
        source = icon.pixmap(QSize(size, size), 1.0)
        image = QImage(source.width(), source.height(), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(source))                      # textured brush: the image, cut to the ellipse
        side = min(source.width(), source.height())
        painter.drawEllipse(QRectF((source.width() - side) / 2, (source.height() - side) / 2, side, side))
        painter.end()
        result.addPixmap(QPixmap.fromImage(image))
    return result


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
                state_eyes=meta["state_eyes"], shape=meta["shape"], glow=meta["glow"], badge_color=meta["badge"])
    for state in STATES:
        icon = load_icon(folder / f"{state}.png")
        skin.icons[state] = circle_icon(icon) if skin.shape == "circle" and not icon.isNull() else icon
        if skin.eyes == "frames":
            blink = load_icon(folder / f"{state}_blink.png", warn=False)
            if not blink.isNull():
                skin.blink_icons[state] = circle_icon(blink) if skin.shape == "circle" else blink
    return skin


def mascot_icons(folder: Path | None = None) -> dict:
    """state -> QIcon from one folder (kept for callers that only need the images)."""
    folder = folder or mascot_dir() / DEFAULT_SKIN
    return {state: load_icon(folder / f"{state}.png") for state in STATES}
