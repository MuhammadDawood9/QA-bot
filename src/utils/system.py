"""External tools and file helpers. ffmpeg is called with argument lists, never a shell string."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

FFMPEG_INSTALL_HELP = (
    "ffmpeg was not found. Install it, then open a new terminal:\n"
    "  Windows: winget install ffmpeg\n"
    "  macOS:   brew install ffmpeg\n"
    "Or set machine.ffmpeg_path in config.yaml to the ffmpeg program."
)


class MissingToolError(RuntimeError):
    pass


def bundled_ffmpeg() -> str | None:
    """The ffmpeg that ships inside the app (the imageio-ffmpeg package: a static build for Windows,
    macOS Intel and Apple Silicon), or None."""
    try:
        import imageio_ffmpeg

        path = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:                       # not installed, or its binary is missing from the build
        return None
    return path if path and Path(path).is_file() else None


def find_ffmpeg(configured: str | None = None) -> str:
    """The ffmpeg program: machine.ffmpeg_path from config if set; then, in the packaged app, the
    bundled one (so every floor PC uses the same build, no install needed); then the one on PATH;
    then the bundled one."""
    import sys

    if configured:
        path = Path(configured)
        if path.is_file():
            return str(path)
        raise MissingToolError(f"machine.ffmpeg_path is set to {configured}, but that file doesn't exist.\n"
                               + FFMPEG_INSTALL_HELP)
    if getattr(sys, "frozen", False):
        found = bundled_ffmpeg()
        if found:
            return found
    found = shutil.which("ffmpeg") or bundled_ffmpeg()
    if found is None:
        raise MissingToolError(FFMPEG_INSTALL_HELP)
    return found


def ffmpeg_version(configured: str | None = None) -> str:
    """First line of `ffmpeg -version`."""
    result = subprocess.run([find_ffmpeg(configured), "-version"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", check=True)
    return result.stdout.splitlines()[0] if result.stdout else ""


def extract_audio(video: Path, wav: Path, ffmpeg: str, sample_rate: int = 16000) -> Path:
    """Write the video's audio as mono 16 kHz 16-bit WAV, which is what Whisper expects."""
    wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
           "-i", str(video), "-vn", "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(wav)]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg could not extract audio from {video.name}: {result.stderr.strip()[-500:]}")
    return wav


def file_sha256(path: Path, chunk_size: int = 1 << 20) -> str:
    """Hash of the file's contents, read in 1 MB pieces so large videos don't fill memory."""
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()
