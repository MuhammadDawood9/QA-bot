"""Frames from the video at the moments the report talks about (one ffmpeg call per frame)."""

from __future__ import annotations

import logging
from pathlib import Path

from src.utils.system import run_ffmpeg

log = logging.getLogger("qcbot.report")


def grab_frame(ffmpeg: str, video: Path, seconds: float, out: Path, width: int = 640) -> bool:
    """Save one JPEG at `seconds`. Returns False (and logs) instead of failing the whole report."""
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{max(0.0, seconds):.3f}", "-i", str(video),        # -ss before -i: fast seek
           "-frames:v", "1", "-vf", f"scale={int(width)}:-2", "-q:v", "3", str(out)]
    result = run_ffmpeg(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0 or not out.is_file():
        log.warning("no screenshot at %.3f s: %s", seconds, (result.stderr or "no frame there").strip()[-200:])
        return False
    return make_word_compatible(out)


def make_word_compatible(path: Path) -> bool:
    """Re-save the JPEG with a standard JFIF header. Some ffmpeg versions (9.x) leave it out
    depending on the video, and python-docx then refuses the image (UnrecognizedImageError)."""
    from PIL import Image

    try:
        with Image.open(path) as img:
            img.load()
            img.convert("RGB").save(path, "JPEG", quality=88)
    except OSError as exc:
        log.warning("screenshot %s is unreadable: %s", path.name, exc)
        return False
    return True
