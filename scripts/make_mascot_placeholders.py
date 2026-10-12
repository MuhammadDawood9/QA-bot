"""Draw placeholder mascot and tray images, so the app runs before the real art exists.

    python scripts/make_mascot_placeholders.py            # writes only the files that are missing
    python scripts/make_mascot_placeholders.py --force    # overwrites them

Writes:
  assets/mascot/idle.png, working.png, done.png, error.png         120 x 120 (1x)
  assets/mascot/idle@2x.png ...                                    240 x 240 (Retina / high-DPI)
  assets/tray/tray.png, tray@2x.png                                32 / 64, in color (Windows tray)
  assets/tray/trayTemplate.png, trayTemplate@2x.png                18 / 36, black on transparent
                                                                   (macOS menu bar "template" image)

To use your own art, replace the files in assets/mascot/ with PNGs of the same names (transparent
background). The window is 120 px; give a 1x and an @2x file for sharp results on every screen.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
MASCOT = ROOT / "assets" / "mascot"
TRAY = ROOT / "assets" / "tray"
OVERSAMPLE = 4                     # drawn large, then shrunk: smooth edges without anti-aliasing support

BODY = {"idle": (64, 160, 170), "working": (70, 130, 200), "done": (70, 170, 110), "error": (210, 80, 80)}
DARK = (30, 40, 50, 255)
WHITE = (255, 255, 255, 255)


def face(state: str, size: int) -> Image.Image:
    s = size * OVERSAMPLE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = s / 120                                            # drawing units: the 1x pixel grid
    body = BODY[state] + (255,)

    # antenna, head and a soft outline
    d.line([(60 * u, 22 * u), (60 * u, 8 * u)], fill=DARK, width=int(4 * u))
    d.ellipse([53 * u, 2 * u, 67 * u, 16 * u], fill=body, outline=DARK, width=int(3 * u))
    d.rounded_rectangle([14 * u, 20 * u, 106 * u, 108 * u], radius=30 * u, fill=body, outline=DARK, width=int(4 * u))
    d.rounded_rectangle([24 * u, 32 * u, 96 * u, 94 * u], radius=20 * u, fill=(245, 248, 250, 255))

    def eye(cx, cy):
        if state == "idle":
            d.ellipse([cx - 7 * u, cy - 9 * u, cx + 7 * u, cy + 9 * u], fill=DARK)
            d.ellipse([cx - 2 * u, cy - 6 * u, cx + 3 * u, cy - 1 * u], fill=WHITE)
        elif state == "working":                          # looking down at the work
            d.ellipse([cx - 7 * u, cy - 3 * u, cx + 7 * u, cy + 9 * u], fill=DARK)
            d.line([(cx - 9 * u, cy - 5 * u), (cx + 9 * u, cy - 5 * u)], fill=DARK, width=int(3 * u))
        elif state == "done":                              # happy ^ ^
            d.arc([cx - 9 * u, cy - 6 * u, cx + 9 * u, cy + 10 * u], 200, 340, fill=DARK, width=int(4 * u))
        else:                                              # error: x x
            for a, b in (((-7, -7), (7, 7)), ((-7, 7), (7, -7))):
                d.line([(cx + a[0] * u, cy + a[1] * u), (cx + b[0] * u, cy + b[1] * u)], fill=DARK, width=int(4 * u))

    eye(45 * u, 55 * u)
    eye(75 * u, 55 * u)
    if state == "idle":
        d.arc([46 * u, 64 * u, 74 * u, 84 * u], 20, 160, fill=DARK, width=int(4 * u))
    elif state == "working":                               # "thinking" dots
        for x in (48, 60, 72):
            d.ellipse([(x - 3) * u, 77 * u, (x + 3) * u, 83 * u], fill=DARK)
    elif state == "done":
        d.chord([42 * u, 64 * u, 78 * u, 90 * u], 0, 180, fill=DARK)
    else:
        d.arc([46 * u, 74 * u, 74 * u, 94 * u], 200, 340, fill=DARK, width=int(4 * u))
    return img.resize((size, size), Image.LANCZOS)


def tray_template(size: int) -> Image.Image:
    """Black head silhouette with two eye holes; macOS recolors it for light and dark menu bars."""
    s = size * OVERSAMPLE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = s / 18
    black, clear = (0, 0, 0, 255), (0, 0, 0, 0)
    d.rounded_rectangle([2 * u, 4 * u, 16 * u, 16 * u], radius=4 * u, fill=black)
    d.rectangle([8.3 * u, 1 * u, 9.7 * u, 4 * u], fill=black)
    d.ellipse([5 * u, 8 * u, 7.6 * u, 10.6 * u], fill=clear)
    d.ellipse([10.4 * u, 8 * u, 13 * u, 10.6 * u], fill=clear)
    return img.resize((size, size), Image.LANCZOS)


def save(img: Image.Image, path: Path, force: bool) -> bool:
    if path.exists() and not force:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG")
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Draw placeholder mascot and tray images.")
    parser.add_argument("--force", action="store_true", help="overwrite existing images")
    args = parser.parse_args(argv)
    written = 0
    for state in BODY:
        written += save(face(state, 120), MASCOT / f"{state}.png", args.force)
        written += save(face(state, 240), MASCOT / f"{state}@2x.png", args.force)
    written += save(face("idle", 32), TRAY / "tray.png", args.force)
    written += save(face("idle", 64), TRAY / "tray@2x.png", args.force)
    written += save(tray_template(18), TRAY / "trayTemplate.png", args.force)
    written += save(tray_template(36), TRAY / "trayTemplate@2x.png", args.force)
    print(f"wrote {written} image(s) to {MASCOT.relative_to(ROOT)} and {TRAY.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
