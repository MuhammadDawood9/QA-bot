"""Audio files as input, and the audio of any file as numbers.

    is_audio_only(path)                      an .mp3/.wav/.m4a/.aac/.flac/.ogg, or a file with no picture
    load_audio(path, ffmpeg, sample_rate)    mono float32 samples (-1..1), decoded by ffmpeg

Silences are found from these samples (src/audio/edits.py silences_in), not with ffmpeg's
silencedetect: that judges every sample on its own, so the peaks of a room's hiss count as sound
and a long pause in a podcast with room tone is never "silent".
"""

from __future__ import annotations

from pathlib import Path

from src.utils.system import run_ffmpeg

AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wma", ".aif", ".aiff")


def has_picture(path: Path) -> bool | None:
    """Whether the file has a video stream (cover art in an mp3 doesn't count). None if unreadable."""
    try:
        import av

        with av.open(str(path)) as container:
            for stream in container.streams.video:
                try:
                    cover_art = bool(stream.disposition & av.stream.Disposition.attached_pic)
                except (AttributeError, TypeError):
                    cover_art = False
                if not cover_art:
                    return True
            return False
    except Exception:                    # not a media file av can open: decide by the extension
        return None


def has_audio(path: Path) -> bool | None:
    """Whether the file has an audio stream (a video without one has nothing to compare captions
    with, so the comparison skips itself - not an error). None if the file can't be opened at all,
    so the caller still reports the real error instead of hiding it as "no audio"."""
    try:
        import av

        with av.open(str(path)) as container:
            return any(stream.type == "audio" for stream in container.streams)
    except Exception:                    # not a media file av can open: let the caller decide
        return None


def media_duration(path: Path) -> float | None:
    """The file's total length in seconds, or None if it can't be read. One cheap container open:
    it turns spoken time into coverage, so a cached transcript is never transcribed again."""
    try:
        import av

        with av.open(str(path)) as container:
            seconds = (container.duration or 0) / 1_000_000      # av reports microseconds
            if seconds > 0:
                return seconds
            for stream in list(container.streams.video) + list(container.streams.audio):
                if stream.duration:
                    return float(stream.duration * stream.time_base)
            return None
    except Exception:
        return None


def is_audio_only(path: Path) -> bool:
    path = Path(path)
    if path.suffix.lower() in AUDIO_EXTENSIONS:
        return True
    return has_picture(path) is False


def load_audio(path: Path, ffmpeg: str, sample_rate: int = 16000, start: float | None = None,
               duration: float | None = None):
    """The file's audio (all channels mixed to mono) as float32 numpy samples at `sample_rate`,
    straight from ffmpeg's output (no temporary file). start/duration: only that part, in seconds."""
    import numpy as np

    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", f"{max(0.0, start):.3f}"]
    cmd += ["-i", str(path)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(int(sample_rate)), "-f", "f32le", "-acodec", "pcm_f32le", "-"]
    result = run_ffmpeg(cmd, capture_output=True)
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()[-400:]
        raise RuntimeError(f"ffmpeg could not read the audio of {Path(path).name}: {message}")
    return np.frombuffer(result.stdout, dtype="<f4").astype(np.float32)
