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


_ffmpeg_timeout: float | None = None       # machine.ffmpeg_timeout_sec, set by load_config


def set_ffmpeg_timeout(seconds) -> None:
    """How long one ffmpeg call may run (machine.ffmpeg_timeout_sec). None or 0 = no limit."""
    global _ffmpeg_timeout
    _ffmpeg_timeout = float(seconds) if seconds and float(seconds) > 0 else None


def run_ffmpeg(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run, stopped after machine.ffmpeg_timeout_sec: a damaged video can make ffmpeg
    hang. A timeout comes back as a failed run (returncode -1, the reason in stderr), so callers
    handle it like any other ffmpeg failure."""
    try:
        return subprocess.run(cmd, timeout=_ffmpeg_timeout, **kwargs)
    except subprocess.TimeoutExpired:
        reason = f"ffmpeg was stopped after {_ffmpeg_timeout:g} s (machine.ffmpeg_timeout_sec)"
        if kwargs.get("text") or kwargs.get("encoding"):
            return subprocess.CompletedProcess(cmd, -1, "", reason)
        return subprocess.CompletedProcess(cmd, -1, b"", reason.encode("utf-8"))


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


def stream_ffmpeg(cmd: list[str], chunk_bytes: int):
    """ffmpeg's output (stdout) in pieces of chunk_bytes, for files too long to hold in memory.
    Stopped after machine.ffmpeg_timeout_sec like run_ffmpeg. Raises RuntimeError if ffmpeg fails
    or is stopped."""
    import tempfile
    import threading

    with tempfile.TemporaryFile() as err:                  # stderr to a file: a full pipe can't block ffmpeg
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
        timed_out = threading.Event()

        def stop():
            timed_out.set()
            proc.kill()                                    # the blocked read below then ends

        timer = threading.Timer(_ffmpeg_timeout, stop) if _ffmpeg_timeout else None
        if timer is not None:
            timer.daemon = True
            timer.start()
        try:
            while True:
                chunk = proc.stdout.read(chunk_bytes)
                if not chunk:
                    break
                yield chunk
            code = proc.wait()
            if timed_out.is_set():
                raise RuntimeError(f"ffmpeg was stopped after {_ffmpeg_timeout:g} s (machine.ffmpeg_timeout_sec)")
            if code != 0:
                err.seek(0)
                message = err.read().decode("utf-8", errors="replace").strip()[-400:]
                raise RuntimeError(f"ffmpeg failed: {message}")
        finally:
            if timer is not None:
                timer.cancel()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()


def ffmpeg_version(configured: str | None = None) -> str:
    """First line of `ffmpeg -version`."""
    cmd = [find_ffmpeg(configured), "-version"]
    result = run_ffmpeg(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    return result.stdout.splitlines()[0] if result.stdout else ""


def extract_audio(video: Path, wav: Path, ffmpeg: str, sample_rate: int = 16000) -> Path:
    """Write the video's audio as mono 16 kHz 16-bit WAV, which is what Whisper expects."""
    wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
           "-i", str(video), "-vn", "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(wav)]
    result = run_ffmpeg(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
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
