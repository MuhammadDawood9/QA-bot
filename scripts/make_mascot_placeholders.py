"""Draw placeholder mascot skins and tray icons, so the app runs before the real art exists.

    python scripts/make_mascot_placeholders.py            # writes only the files that are missing
    python scripts/make_mascot_placeholders.py --force    # overwrites them

Skins (assets/mascot/<skin>/), each with skin.json:
  inspector   eyes "frames": <state>.png has eyes, <state>_blink.png the same with them closed
  cloud       eyes "drawn":  <state>.png has no eyes; the app paints them from skin.json
  robot       eyes "frames": the first placeholder robot
Every image is 120 x 120 with a transparent background, plus an @2x version at 240 x 240.
No glow in the images: the app draws the glow behind the sprite.

Tray icons (assets/tray/): tray.png / tray@2x.png in color for Windows, trayTemplate.png /
trayTemplate@2x.png black on transparent for the macOS menu bar.

To use your own art: make assets/mascot/<your skin>/ with the same file names and a skin.json
(copy one of these), then pick it under Skin in the right-click menu.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
MASCOT = ROOT / "assets" / "mascot"
TRAY = ROOT / "assets" / "tray"
OVERSAMPLE = 4                     # drawn large, then shrunk: smooth edges
STATES = ("idle", "working", "done", "error")

DARK = (30, 40, 50, 255)
WHITE = (255, 255, 255, 255)


class Canvas:
    """A drawing surface in 120-unit coordinates, at any output size."""

    def __init__(self, size: int):
        self.size = size
        self.s = size * OVERSAMPLE
        self.img = Image.new("RGBA", (self.s, self.s), (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.img)
        self.u = self.s / 120

    def box(self, x0, y0, x1, y1):
        return [x0 * self.u, y0 * self.u, x1 * self.u, y1 * self.u]

    def w(self, width):
        return max(1, int(width * self.u))

    def done(self) -> Image.Image:
        return self.img.resize((self.size, self.size), Image.LANCZOS)


def eyes(c: Canvas, state: str, centers, blink: bool, r=7):
    """Eyes for the frame skins: open, focused, happy, crossed, or closed for a blink."""
    for cx, cy in centers:
        if blink:
            c.d.line([(cx - r) * c.u, cy * c.u, (cx + r) * c.u, cy * c.u], fill=DARK, width=c.w(4))
        elif state == "idle":
            c.d.ellipse(c.box(cx - r, cy - r - 2, cx + r, cy + r + 2), fill=DARK)
            c.d.ellipse(c.box(cx - 2, cy - 6, cx + 3, cy - 1), fill=WHITE)
        elif state == "working":
            c.d.ellipse(c.box(cx - r, cy - 3, cx + r, cy + r + 2), fill=DARK)
            c.d.line([(cx - r - 2) * c.u, (cy - 5) * c.u, (cx + r + 2) * c.u, (cy - 5) * c.u], fill=DARK, width=c.w(3))
        elif state == "done":
            c.d.arc(c.box(cx - 9, cy - 6, cx + 9, cy + 10), 200, 340, fill=DARK, width=c.w(4))
        else:
            for (ax, ay), (bx, by) in (((-6, -6), (6, 6)), ((-6, 6), (6, -6))):
                c.d.line([(cx + ax) * c.u, (cy + ay) * c.u, (cx + bx) * c.u, (cy + by) * c.u], fill=DARK, width=c.w(4))


def mouth(c: Canvas, state: str, cx=60, cy=82):
    if state == "idle":
        c.d.arc(c.box(cx - 12, cy - 10, cx + 12, cy + 6), 20, 160, fill=DARK, width=c.w(4))
    elif state == "working":
        c.d.line([(cx - 7) * c.u, cy * c.u, (cx + 7) * c.u, cy * c.u], fill=DARK, width=c.w(4))
    elif state == "done":
        c.d.chord(c.box(cx - 15, cy - 10, cx + 15, cy + 12), 0, 180, fill=DARK)
    else:
        c.d.arc(c.box(cx - 12, cy - 2, cx + 12, cy + 14), 200, 340, fill=DARK, width=c.w(4))


# --- inspector: a round detective with a deerstalker cap and a magnifying glass ------------------

INSPECTOR_TINT = {"idle": (242, 193, 78), "working": (240, 180, 70), "done": (232, 196, 90), "error": (236, 168, 120)}


def inspector(state: str, size: int, blink: bool = False) -> Image.Image:
    c = Canvas(size)
    skin = INSPECTOR_TINT[state] + (255,)
    cap, cap_dark = (150, 105, 70, 255), (110, 75, 50, 255)
    c.d.ellipse(c.box(18, 30, 102, 112), fill=skin, outline=DARK, width=c.w(4))           # head
    c.d.pieslice(c.box(26, 10, 94, 66), 180, 360, fill=cap, outline=DARK, width=c.w(4))   # cap crown
    c.d.rounded_rectangle(c.box(14, 36, 106, 46), radius=5 * c.u, fill=cap_dark, outline=DARK, width=c.w(3))  # brim
    c.d.ellipse(c.box(55, 8, 65, 18), fill=cap_dark, outline=DARK, width=c.w(3))         # button
    eyes(c, state, [(45, 66), (75, 66)], blink)
    mouth(c, state, cy=90)
    # the magnifying glass, held up while working, resting otherwise
    lens = (88, 70, 112, 94) if state == "working" else (86, 84, 108, 106)
    c.d.line([(lens[0] + 4) * c.u, (lens[3] - 4) * c.u, (lens[0] - 8) * c.u, (lens[3] + 8) * c.u], fill=DARK, width=c.w(6))
    c.d.ellipse(c.box(*lens), fill=(210, 235, 250, 200), outline=DARK, width=c.w(4))
    return c.done()


# --- cloud: a soft cloud whose eyes the app paints (skin.json "eyes": "drawn") -----------------

CLOUD_TINT = {"idle": (246, 248, 252), "working": (232, 238, 252), "done": (236, 250, 240), "error": (252, 236, 236)}


def cloud(state: str, size: int) -> Image.Image:
    c = Canvas(size)
    fill = CLOUD_TINT[state] + (255,)
    puffs = [(14, 50, 54, 90), (32, 26, 82, 76), (62, 34, 104, 76), (70, 52, 110, 94), (24, 60, 96, 104)]
    for p in puffs:                                    # outline first, then the fill over it, so only the rim shows
        c.d.ellipse(c.box(p[0] - 2.5, p[1] - 2.5, p[2] + 2.5, p[3] + 2.5), fill=DARK)
    for p in puffs:
        c.d.ellipse(c.box(*p), fill=fill)
    c.d.ellipse(c.box(30, 70, 40, 78), fill=(255, 170, 180, 140))     # cheeks
    c.d.ellipse(c.box(80, 70, 90, 78), fill=(255, 170, 180, 140))
    mouth(c, state, cy=84)
    return c.done()


CLOUD_SKIN = {
    "name": "Cloud",
    "eyes": "drawn",
    "eye_shape": {"left": [46, 64], "right": [74, 64], "radius": 6.5, "color": "#24303C", "highlight": "#FFFFFF"},
    "state_eyes": {"idle": "open", "working": "focused", "done": "happy", "error": "cross"},
}

# --- robot: the first placeholder ------------------------------------------------------------

ROBOT_BODY = {"idle": (64, 160, 170), "working": (70, 130, 200), "done": (70, 170, 110), "error": (210, 80, 80)}


def robot(state: str, size: int, blink: bool = False) -> Image.Image:
    c = Canvas(size)
    body = ROBOT_BODY[state] + (255,)
    c.d.line([60 * c.u, 22 * c.u, 60 * c.u, 8 * c.u], fill=DARK, width=c.w(4))
    c.d.ellipse(c.box(53, 2, 67, 16), fill=body, outline=DARK, width=c.w(3))
    c.d.rounded_rectangle(c.box(14, 20, 106, 108), radius=30 * c.u, fill=body, outline=DARK, width=c.w(4))
    c.d.rounded_rectangle(c.box(24, 32, 96, 94), radius=20 * c.u, fill=(245, 248, 250, 255))
    eyes(c, state, [(45, 55), (75, 55)], blink)
    if state == "working":
        for x in (48, 60, 72):
            c.d.ellipse(c.box(x - 3, 77, x + 3, 83), fill=DARK)
    else:
        mouth(c, state, cy=78)
    return c.done()


# --- tray -------------------------------------------------------------------------------------

def tray_template(size: int) -> Image.Image:
    """Black head silhouette with two eye holes; macOS recolors it for light and dark menu bars."""
    c = Canvas(size)
    u = c.s / 18
    black, clear = (0, 0, 0, 255), (0, 0, 0, 0)
    c.d.rounded_rectangle([2 * u, 4 * u, 16 * u, 16 * u], radius=4 * u, fill=black)
    c.d.rectangle([8.3 * u, 1 * u, 9.7 * u, 4 * u], fill=black)
    c.d.ellipse([5 * u, 8 * u, 7.6 * u, 10.6 * u], fill=clear)
    c.d.ellipse([10.4 * u, 8 * u, 13 * u, 10.6 * u], fill=clear)
    return c.done()


def save(img: Image.Image, path: Path, force: bool) -> bool:
    if path.exists() and not force:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG")
    return True


def save_json(data: dict, path: Path, force: bool) -> bool:
    if path.exists() and not force:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Draw placeholder mascot skins and tray icons.")
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    args = parser.parse_args(argv)
    n = 0
    for skin, draw, meta in (("inspector", inspector, {"name": "Inspector", "eyes": "frames"}),
                             ("robot", robot, {"name": "Robot", "eyes": "frames"})):
        folder = MASCOT / skin
        for state in STATES:
            for scale, suffix in ((1, ""), (2, "@2x")):
                n += save(draw(state, 120 * scale), folder / f"{state}{suffix}.png", args.force)
                n += save(draw(state, 120 * scale, blink=True), folder / f"{state}_blink{suffix}.png", args.force)
        n += save_json(meta, folder / "skin.json", args.force)
    for state in STATES:
        for scale, suffix in ((1, ""), (2, "@2x")):
            n += save(cloud(state, 120 * scale), MASCOT / "cloud" / f"{state}{suffix}.png", args.force)
    n += save_json(CLOUD_SKIN, MASCOT / "cloud" / "skin.json", args.force)
    n += save(robot("idle", 32), TRAY / "tray.png", args.force)
    n += save(robot("idle", 64), TRAY / "tray@2x.png", args.force)
    n += save(tray_template(18), TRAY / "trayTemplate.png", args.force)
    n += save(tray_template(36), TRAY / "trayTemplate@2x.png", args.force)
    print(f"wrote {n} file(s) under {MASCOT.parent.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
