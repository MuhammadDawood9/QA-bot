"""Turn a round avatar (open eyes + closed eyes, any size) into a mascot skin.

    python scripts/add_avatar_skin.py "mascot/Avatar.png" "mascot/Avatar closed eyes.png"
    python scripts/add_avatar_skin.py OPEN CLOSED --key avatar --name Avatar --force

For each image it finds the opaque disc (art often has a soft or ragged fringe around the circle,
and the disc isn't always centered) and crops a square just inside it. The closed-eyes crop is
then moved and scaled until its drawing lines up with the open one, so a blink doesn't make the
face jump. It saves 120 x 120 and
240 x 240 copies as every state's image (<state>.png, @2x) and blink frame (<state>_blink.png).
The app masks them to a circle and draws the glow and ring itself, so nothing here is baked in.
Writes skin.json with "shape": "circle" and the glow colors and timings, ready to edit.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.app.assets import DEFAULT_GLOW, STATES  # noqa: E402

SIZES = {"": 120, "@2x": 240}
OPAQUE = 200          # alpha that counts as part of the disc
MARGIN = 0.015        # crop this much further in, so no fringe pixel survives


def disc(image: Image.Image) -> tuple[float, float, float]:
    """Center and radius of the opaque disc: the bounding box's center, and the nearest point
    (over 180 directions) where the alpha drops."""
    alpha = image.getchannel("A")
    box = alpha.point(lambda a: 255 if a >= OPAQUE else 0).getbbox()
    if box is None:
        raise SystemExit("the image is fully transparent")
    cx, cy = (box[0] + box[2] - 1) / 2, (box[1] + box[3] - 1) / 2
    w, h = image.size
    px = alpha.load()
    nearest = min(w, h) / 2
    for step in range(180):
        angle = 2 * math.pi * step / 180
        r = 1.0
        while True:
            x, y = round(cx + r * math.cos(angle)), round(cy + r * math.sin(angle))
            if not (0 <= x < w and 0 <= y < h) or px[x, y] < OPAQUE:
                break
            r += 1
        nearest = min(nearest, r)
    return cx, cy, nearest * (1 - MARGIN)


def square(image: Image.Image, cx: float, cy: float, r: float, size: int | None = None) -> Image.Image:
    crop = image.crop((round(cx - r), round(cy - r), round(cx + r), round(cy + r)))
    return crop.resize((size, size), Image.Resampling.BILINEAR) if size else crop


def align(open_img: Image.Image, open_disc, closed_img: Image.Image, closed_disc) -> tuple[float, float, float]:
    """The crop of the closed-eyes image whose drawing lines up best with the open one (the two
    are often separate drawings, a little shifted or scaled). Compares everything inside the
    circle except the eye band, coarse to fine."""
    import numpy as np

    size = 96
    target = np.asarray(square(open_img.convert("L"), *open_disc, size), dtype=float)
    yy, xx = np.mgrid[0:size, 0:size]
    middle = size / 2
    keep = ((xx - middle) ** 2 + (yy - middle) ** 2 < (0.44 * size) ** 2) & ~((yy > 0.34 * size) & (yy < 0.62 * size))
    gray = closed_img.convert("L")

    def score(cx, cy, r):
        return float(np.abs(target - np.asarray(square(gray, cx, cy, r, size), dtype=float))[keep].mean())

    cx, cy, r = closed_disc
    best = (score(cx, cy, r), cx, cy, r)
    for step in (16, 8, 4, 2, 1):                 # move and resize the crop while it helps
        improved = True
        while improved:
            improved = False
            _, bx, by, br = best
            for dx, dy, dr in ((step, 0, 0), (-step, 0, 0), (0, step, 0), (0, -step, 0), (0, 0, step), (0, 0, -step)):
                s = score(bx + dx, by + dy, br + dr)
                if s < best[0] - 0.01:
                    best, improved = (s, bx + dx, by + dy, br + dr), True
    print(f"blink frame aligned: difference {score(*closed_disc):.1f} -> {best[0]:.1f}")
    return best[1:]


def fit_inside(crop, found) -> float:
    """How much a crop circle must shrink (about its center) to stay inside the clean disc."""
    (cx, cy, r), (dx, dy, dr) = crop, found
    room = dr - math.hypot(cx - dx, cy - dy)
    return min(1.0, room / r)


def crop_frames(open_path: Path, closed_path: Path) -> tuple[Image.Image, Image.Image]:
    images, discs = [], []
    for path in (open_path, closed_path):
        image = Image.open(path).convert("RGBA")
        found = disc(image)
        print(f"{path.name}: {image.size[0]} x {image.size[1]}, disc at ({found[0]:.0f}, {found[1]:.0f}) "
              f"radius {found[2]:.0f}")
        images.append(image)
        discs.append(found)
    crops = [discs[0], align(images[0], discs[0], images[1], discs[1])]
    k = min(fit_inside(c, d) for c, d in zip(crops, discs))      # same shrink for both: they stay aligned
    frames = [square(img, cx, cy, r * k) for img, (cx, cy, r) in zip(images, crops)]
    for frame in frames:
        frame.putalpha(255)           # the disc is meant to be solid (art often says 253); the app cuts the corners
    return tuple(frames)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("open", type=Path, help="the avatar with open eyes")
    parser.add_argument("closed", type=Path, help="the same avatar with closed eyes (the blink frame)")
    parser.add_argument("--key", default="avatar", help="skin folder name (default: avatar)")
    parser.add_argument("--name", default="Avatar", help="name in the Skin menu (default: Avatar)")
    parser.add_argument("--force", action="store_true", help="replace an existing skin folder's files")
    args = parser.parse_args()

    folder = ROOT / "assets" / "mascot" / args.key
    if (folder / "skin.json").exists() and not args.force:
        raise SystemExit(f"{folder} already exists; add --force to replace it")
    folder.mkdir(parents=True, exist_ok=True)
    opened, closed = crop_frames(args.open, args.closed)
    frames = {"": opened, "_blink": closed}
    for suffix, image in frames.items():
        for scale, size in SIZES.items():
            small = image.resize((size, size), Image.Resampling.LANCZOS)
            for state in STATES:
                small.save(folder / f"{state}{suffix}{scale}.png", optimize=True)
    meta = {"name": args.name, "eyes": "frames", "shape": "circle", "glow": DEFAULT_GLOW}
    (folder / "skin.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {folder}")


if __name__ == "__main__":
    main()
