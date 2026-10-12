"""Make a test video with known color grading mistakes, from any real video.

    python scripts/make_color_samples.py --source data/samples/del.mp4

The source is cut into 4-second shots and each shot gets one deliberate grading mistake with an
ffmpeg filter (the first and last shots are left clean). Writes data/samples/color_test.mp4 and
color_test_expected.json: for each shot, its time and the issue type the color check should report.
Use a source that is well graded itself, with a face in it (for the skin tone shot).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import load_config  # noqa: E402
from src.utils.system import find_ffmpeg  # noqa: E402

SHOT_SEC = 4.0
# (filter, expected issue type or None for a clean shot, what it imitates from the QC notes)
SHOTS = [
    (None, None, "clean"),
    ("curves=all='0/0 0.55/1 1/1'", "overexposed", "E3RN: too bright / E3AC: highlights clipped"),
    ("colorbalance=rs=0.2:bs=0.2:gs=-0.2:rm=0.2:bm=0.2:gm=-0.2:rh=0.15:bh=0.15:gh=-0.15", "color_cast",
     "E3AC: the magenta is going too much"),
    ("hue=s=0.1", "saturation", "E3AC: colors are dull"),
    ("curves=all='0/0.3 1/0.8'", "flat_contrast", "E3SO: flat contrast with lifted black levels"),
    ("eq=brightness=-0.32:gamma=0.8", "underexposed", "E2EW: too much dark"),
    ("hue=h=28", "skin_tone", "E2OCA: skin color yellow"),
    (None, None, "clean"),
]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Make a test video with known color grading mistakes.")
    parser.add_argument("--source", type=Path, required=True, help="a well-graded video, at least 29 s long (shots are taken every 3.5 s)")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "samples")
    args = parser.parse_args(argv)
    ffmpeg = find_ffmpeg(load_config().get("machine", {}).get("ffmpeg_path"))
    args.out.mkdir(parents=True, exist_ok=True)

    expected = []
    with tempfile.TemporaryDirectory(prefix="qcbot-color-") as tmp:
        parts = []
        for n, (vf, issue, note) in enumerate(SHOTS):
            part = Path(tmp) / f"part{n}.mp4"
            filters = "scale=720:-2" + (f",{vf}" if vf else "")
            subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                            "-ss", f"{n * 3.5:.2f}", "-t", f"{SHOT_SEC:.2f}", "-i", str(args.source),
                            "-vf", filters, "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
                            str(part)], check=True)
            parts.append(part)
            expected.append({"shot": n + 1, "start": n * SHOT_SEC, "end": (n + 1) * SHOT_SEC,
                             "expected": issue, "imitates": note})
        listing = Path(tmp) / "parts.txt"
        listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
        video = args.out / "color_test.mp4"
        subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
                        "-safe", "0", "-i", str(listing), "-c", "copy", str(video)], check=True)
    (args.out / "color_test_expected.json").write_text(json.dumps(expected, indent=2), encoding="utf-8")
    print(f"wrote {video.name} ({len(SHOTS)} shots of {SHOT_SEC:.0f} s) and color_test_expected.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
