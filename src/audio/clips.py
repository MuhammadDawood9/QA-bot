"""For the report: a short MP3 of the moment an audio issue is about, and a picture of its waveform
with the flagged point marked (instead of the screenshot a video issue gets).

    write_clip(ffmpeg, source, at, out_mp3)              ~6 s around `at`, mono 64 kbit/s (~50 KB)
    write_waveform(ffmpeg, source, at, out_png)          the same 6 s drawn, a red line at `at`
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger("qcbot.report")

CLIP_SECONDS = 6.0


def clip_window(at: float, seconds: float = CLIP_SECONDS) -> tuple[float, float]:
    start = max(0.0, at - seconds / 2)
    return start, seconds


def write_clip(ffmpeg: str, source: Path, at: float, out: Path, seconds: float = CLIP_SECONDS) -> bool:
    out.parent.mkdir(parents=True, exist_ok=True)
    start, length = clip_window(at, seconds)
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{length:.3f}",
           "-i", str(source), "-vn", "-ac", "1", "-ar", "22050", "-codec:a", "libmp3lame", "-b:a", "64k",
           "-af", "afade=t=in:d=0.05,areverse,afade=t=in:d=0.05,areverse", str(out)]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0 or not out.is_file():
        log.warning("no audio clip at %.2f s: %s", at, result.stderr.strip()[-200:])
        return False
    return True


def write_waveform(ffmpeg: str, source: Path, at: float, out: Path, width: int = 640, height: int = 110,
                   seconds: float = CLIP_SECONDS) -> bool:
    """A grey waveform (min/max per pixel column) of the clip's 6 s, a red line at the flagged
    point, and the start and end times. PIL only (no plotting library)."""
    import numpy as np
    from PIL import Image, ImageDraw

    from src.audio.media import load_audio
    from src.utils.timecode import format_timestamp

    start, length = clip_window(at, seconds)
    try:
        x = load_audio(source, ffmpeg, 8000, start=start, duration=length)
    except RuntimeError as exc:
        log.warning("no waveform at %.2f s: %s", at, exc)
        return False
    img = Image.new("RGB", (width, height), (250, 250, 250))
    draw = ImageDraw.Draw(img)
    mid, scale = (height - 16) / 2, (height - 20) / 2
    peak = max(float(np.max(np.abs(x))) if len(x) else 0.0, 0.05)
    if len(x):
        cols = np.array_split(x, width)
        for i, col in enumerate(cols):
            if len(col):
                lo, hi = float(col.min()) / peak, float(col.max()) / peak
                draw.line([(i, mid - hi * scale), (i, mid - lo * scale)], fill=(90, 110, 140))
    mark = int((at - start) / length * width) if length else 0
    draw.line([(mark, 0), (mark, height - 16)], fill=(200, 0, 0), width=2)
    draw.text((2, height - 14), format_timestamp(start), fill=(80, 80, 80))
    end_label = format_timestamp(start + length)
    draw.text((width - 6 * len(end_label) - 2, height - 14), end_label, fill=(80, 80, 80))
    draw.text((min(max(mark - 30, 0), width - 64), height - 14), format_timestamp(at), fill=(200, 0, 0))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG", optimize=True)
    return True
