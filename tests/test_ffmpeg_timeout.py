"""machine.ffmpeg_timeout_sec: every ffmpeg call is stopped after it, and a stopped call is an
ordinary ffmpeg failure (no ffmpeg needed: subprocess.run is faked)."""

import subprocess
from pathlib import Path

import pytest

from src.audio import clips, media
from src.machine import apply_machine_defaults
from src.report import screenshots
from src.utils import system


@pytest.fixture(autouse=True)
def reset_timeout():
    yield
    system.set_ffmpeg_timeout(None)


def hangs(cmd, timeout=None, **kwargs):
    raise subprocess.TimeoutExpired(cmd, timeout)


def test_the_configured_timeout_is_passed_to_ffmpeg(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    system.set_ffmpeg_timeout(30)
    system.run_ffmpeg(["ffmpeg", "-version"], capture_output=True)
    assert seen["timeout"] == 30.0


@pytest.mark.parametrize("value", [None, 0, "0"])
def test_no_timeout_means_no_limit(value):
    system.set_ffmpeg_timeout(value)
    assert system._ffmpeg_timeout is None


def test_load_config_sets_the_timeout_from_the_machine_block():
    apply_machine_defaults({"machine": {"whisper_model_size": "tiny", "cpu_threads": 1, "ffmpeg_timeout_sec": 45}})
    assert system._ffmpeg_timeout == 45.0


def test_a_timeout_is_a_failed_run_in_text_and_bytes(monkeypatch):
    monkeypatch.setattr(subprocess, "run", hangs)
    system.set_ffmpeg_timeout(5)
    text = system.run_ffmpeg(["ffmpeg"], capture_output=True, text=True, encoding="utf-8")
    assert text.returncode == -1 and "stopped after 5 s" in text.stderr and text.stdout == ""
    raw = system.run_ffmpeg(["ffmpeg"], capture_output=True)
    assert raw.returncode == -1 and b"stopped after 5 s" in raw.stderr and raw.stdout == b""


def test_a_hung_screenshot_is_skipped(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", hangs)
    system.set_ffmpeg_timeout(5)
    assert screenshots.grab_frame("ffmpeg", Path("v.mp4"), 1.0, tmp_path / "f.jpg") is False


def test_a_hung_clip_is_skipped(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", hangs)
    system.set_ffmpeg_timeout(5)
    assert clips.write_clip("ffmpeg", Path("v.mp4"), 3.0, tmp_path / "c.mp3") is False


def test_hung_audio_reading_is_an_error_that_says_why(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", hangs)
    system.set_ffmpeg_timeout(5)
    with pytest.raises(RuntimeError, match="ffmpeg_timeout_sec"):
        system.extract_audio(Path("v.mp4"), tmp_path / "a.wav", "ffmpeg")
    with pytest.raises(RuntimeError, match="ffmpeg_timeout_sec"):
        media.load_audio(Path("v.mp4"), "ffmpeg")


def test_streamed_output_comes_in_pieces_and_a_failure_is_an_error():
    import sys

    chunks = list(system.stream_ffmpeg([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 10)"], 4))
    assert b"".join(chunks) == b"x" * 10 and len(chunks) >= 3
    with pytest.raises(RuntimeError, match="failed: boom"):
        list(system.stream_ffmpeg([sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(1)"], 4))


def test_a_hung_stream_is_stopped_at_the_timeout():
    import sys
    import time

    system.set_ffmpeg_timeout(0.5)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="ffmpeg_timeout_sec"):
        list(system.stream_ffmpeg([sys.executable, "-c", "import time; time.sleep(30)"], 4))
    assert time.monotonic() - started < 10
