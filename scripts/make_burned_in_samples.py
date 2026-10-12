"""Make test videos with captions burned into the picture, in the caption styles clients use.

    python scripts/make_sample_video.py          (first, once: speech audio and synthetic_sample.srt)
    python scripts/make_burned_in_samples.py

Each style is drawn with Pillow over a moving background (so the "has the frame changed?" test
gets real work) and gets the audio of synthetic_sample.mp4:

  subtitle     white text with a dark outline, bottom-center; a "DAY 07" title stays in the top
               corner the whole time (like E2CI)
  box          white text on a black box, a bit higher up (like E7SS)
  wordbyword   2-3 words at a time, each word popping in when it's said, the newest word yellow
               (like E8C / E8D); vertical 9:16

Writes data/samples/burned_<style>.mp4 and burned_<style>_truth.srt: the captions exactly as
drawn, which is what reading the video should give back.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from src.config import load_config  # noqa: E402
from src.inputs.srt_loader import load_srt, write_srt  # noqa: E402
from src.models import Caption  # noqa: E402
from src.utils.system import find_ffmpeg  # noqa: E402

FPS = 25
STYLES = ("subtitle", "box", "wordbyword")
YELLOW = (255, 214, 0)


def chunks(captions: list[Caption], size: int = 3) -> list[tuple[Caption, list[float]]]:
    """Each caption cut into pieces of up to `size` words, timed by share of letters; with the
    moment each word appears inside its piece."""
    out = []
    for cap in captions:
        words = cap.flat_text.split()
        total = sum(len(w) + 1 for w in words)
        t = cap.start
        for i in range(0, len(words), size):
            piece = words[i:i + size]
            length = cap.duration * sum(len(w) + 1 for w in piece) / total
            appear = [t + length * 0.6 * k / max(1, len(piece)) for k in range(len(piece))]
            out.append((Caption(id=len(out) + 1, start=round(t, 3), end=round(t + length, 3),
                                text=" ".join(piece), source="srt"), appear))
            t += length
    return out


def background(draw: ImageDraw.ImageDraw, w: int, h: int, n: int) -> None:
    """A slowly moving gradient with a drifting shape, so frames are never identical."""
    shade = 40 + int(20 * ((n % 100) / 100))
    draw.rectangle([0, 0, w, h], fill=(shade, 50, 70))
    x = (n * 4) % (w + 200) - 100
    draw.ellipse([x, h // 3, x + 160, h // 3 + 160], fill=(90, 110, 140))


def centered(draw, text, font, y, w, fill, **kw):
    width = draw.textlength(text, font=font)
    draw.text(((w - width) / 2, y), text, font=font, fill=fill, **kw)


def render(style: str, captions: list[Caption], duration: float, ffmpeg: str, audio_from: Path,
           out: Path) -> list[Caption]:
    vertical = style == "wordbyword"
    w, h = (720, 1280) if vertical else (1280, 720)
    font = ImageFont.load_default(size=64 if vertical else 44)
    small = ImageFont.load_default(size=34)
    if style == "wordbyword":
        timed = chunks(captions)
        truth = [c for c, _ in timed]
    else:
        timed = [(c, [c.start]) for c in captions]
        truth = list(captions)

    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(FPS), "-i", "-",
           "-i", str(audio_from), "-map", "0:v", "-map", "1:a", "-shortest",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "-c:a", "copy", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        for n in range(int(duration * FPS) + 1):
            t = n / FPS
            img = Image.new("RGB", (w, h))
            draw = ImageDraw.Draw(img)
            background(draw, w, h, n)
            if style == "subtitle":
                draw.text((40, 30), "DAY 07", font=small, fill=(255, 255, 255))
            for cap, appear in timed:
                if not cap.start <= t < cap.end:
                    continue
                if style == "subtitle":
                    centered(draw, cap.flat_text, font, h - 120, w, "white", stroke_width=3, stroke_fill="black")
                elif style == "box":
                    width = draw.textlength(cap.flat_text, font=font)
                    x0, y0 = (w - width) / 2 - 20, h - 230
                    draw.rectangle([x0, y0, x0 + width + 40, y0 + 70], fill="black")
                    centered(draw, cap.flat_text, font, y0 + 10, w, "white")
                else:
                    shown = [word for word, at in zip(cap.text.split(), appear) if t >= at]
                    x = (w - draw.textlength(cap.text, font=font)) / 2   # words land where they'll stay
                    for k, word in enumerate(shown):
                        fill = YELLOW if k == len(shown) - 1 and len(shown) > 1 else "white"
                        draw.text((x, h * 0.7), word, font=font, fill=fill, stroke_width=4, stroke_fill="black")
                        x += draw.textlength(word + " ", font=font)
            proc.stdin.write(img.tobytes())
    finally:
        proc.stdin.close()
        if proc.wait() != 0:
            raise SystemExit(f"error: ffmpeg could not write {out.name}")
    return truth


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Make test videos with burned-in captions.")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "samples")
    parser.add_argument("--style", choices=STYLES, action="append", help="default: all styles")
    args = parser.parse_args(argv)

    srt, audio = args.out / "synthetic_sample.srt", args.out / "synthetic_sample.mp4"
    if not (srt.is_file() and audio.is_file()):
        raise SystemExit("error: run python scripts/make_sample_video.py first")
    ffmpeg = find_ffmpeg(load_config().get("machine", {}).get("ffmpeg_path"))
    captions = load_srt(srt)
    duration = captions[-1].end + 0.8
    for style in args.style or STYLES:
        video = args.out / f"burned_{style}.mp4"
        truth = render(style, captions, duration, ffmpeg, audio, video)
        write_srt(truth, args.out / f"burned_{style}_truth.srt")
        print(f"wrote {video.name} and burned_{style}_truth.srt ({len(truth)} captions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
