"""One decode for every pass over the picture: each pass gets the frames it would pick on its own,
results are the same as the separate passes, caches are filled and used, and the engine falls
back to the separate passes if the shared one fails."""

from pathlib import Path

import numpy as np
import pytest

from src.engine import QCOptions, run_qc
from src.inputs.video_ocr import (read_burned_in_captions, read_screen_text, sample_frames, shared_frames)
from src.inputs.video_pass import read_video_once
from src.inputs.caption_builder import TextBox
from src.video.color import read_color

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NO_AUDIO = frozenset({"transcript"})              # the test video has no sound


def make_video(path: Path, seconds: float = 3.0, fps: int = 30) -> Path:
    """A small video whose picture changes every half second (grey levels and a colored half)."""
    import cv2

    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (96, 64))
    for n in range(int(seconds * fps)):
        level = 40 + 30 * (n // (fps // 2) % 6)
        frame = np.full((64, 96, 3), level, dtype=np.uint8)
        frame[:, 48:, (n // fps) % 3] = 200
        writer.write(frame)
    writer.release()
    return path


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    return make_video(tmp_path_factory.mktemp("vids") / "clip été.mp4")


class FakeOcr:
    """Reads a fixed text everywhere, and counts the calls."""

    def __init__(self):
        self.calls = 0

    def __call__(self, image):
        self.calls += 1
        return [TextBox("HELLO THERE", 0.95, 2, 2, 40, 12)]


OCR = {"sample_fps": 5, "region": "full", "screen_text_every_sec": 1.0}
COLOR = {"enabled": True, "sample_fps": 4, "skin_tones": False, "measure_every_n_frames": 2,
         "face_every_n_frames": 4, "cut_threshold": 0.35, "min_shot_sec": 0.5}


def test_each_pass_gets_the_frames_it_would_pick_alone(video):
    rates = {"captions": 5.0, "color": 4.0, "screen": 0.5}
    seen = {name: [] for name in rates}
    for t, _, _, due in shared_frames(video, rates):
        for name in due:
            seen[name].append(t)
    for name, fps in rates.items():
        assert seen[name] == [t for t, _, _ in sample_frames(video, fps)], name


def test_one_pass_gives_the_same_results_and_fills_the_caches(video, tmp_path):
    separate, shared = tmp_path / "separate", tmp_path / "shared"
    caps = read_burned_in_captions(video, OCR, separate / "ocr", FakeOcr())
    screen = read_screen_text(video, OCR, caps, separate / "ocr", FakeOcr())
    shots = read_color(video, COLOR, None, separate / "color")

    engine = FakeOcr()
    ran = read_video_once(video, ocr=OCR, ocr_cache=shared / "ocr", read_captions=True, read_screen=True,
                          color=COLOR, color_cache=shared / "color", engine=engine)
    assert ran == ["captions", "screen", "color"] and engine.calls > 0
    no_ocr = FakeOcr()
    assert read_burned_in_captions(video, OCR, shared / "ocr", no_ocr) == caps            # from the cache
    assert read_screen_text(video, OCR, caps, shared / "ocr", no_ocr) == screen
    assert [(s.start, s.end, s.frames) for s in read_color(video, COLOR, None, shared / "color")] == \
        [(s.start, s.end, s.frames) for s in shots]
    assert no_ocr.calls == 0
    again = read_video_once(video, ocr=OCR, ocr_cache=shared / "ocr", read_captions=True, read_screen=True,
                            color=COLOR, color_cache=shared / "color", engine=no_ocr)
    assert again == [] and no_ocr.calls == 0                                                # nothing decoded


def test_full_frame_mode_reads_each_frame_once(video, tmp_path):
    """With no caption region, the caption pass already reads the whole frame: the on-screen text
    pass reuses that reading on the frames both use, with the same result."""
    alone_engine = FakeOcr()
    read_burned_in_captions(video, OCR, tmp_path / "a", alone_engine)
    caps = read_burned_in_captions(video, OCR, tmp_path / "a", alone_engine)
    screen = read_screen_text(video, OCR, caps, tmp_path / "a", alone_engine)
    shared_engine = FakeOcr()
    read_video_once(video, ocr=OCR, ocr_cache=tmp_path / "b", read_captions=True, read_screen=True,
                    engine=shared_engine)
    assert read_screen_text(video, OCR, caps, tmp_path / "b", FakeOcr()) == screen
    assert shared_engine.calls < alone_engine.calls


def test_only_what_is_asked_for(video, tmp_path):
    ran = read_video_once(video, ocr=OCR, ocr_cache=tmp_path / "ocr", read_captions=False, read_screen=False,
                          color=COLOR, color_cache=tmp_path / "color", engine=FakeOcr())
    assert ran == ["color"]
    ran = read_video_once(video, ocr={**OCR, "screen_text_every_sec": 0}, ocr_cache=tmp_path / "ocr",
                          read_captions=True, read_screen=True, color=None, engine=FakeOcr())
    assert ran == ["captions"]


def test_engine_reads_the_video_once_with_the_right_passes(monkeypatch, video):
    calls = []
    monkeypatch.setattr("src.engine.read_video_once", lambda *a, **kw: calls.append(kw) or [])
    run_qc(video, None, options=QCOptions(srt_path=FIXTURES / "sample_mistakes.srt", use_llm=False, disabled_checks=NO_AUDIO))
    kw, = calls
    assert kw["read_captions"] is False and kw["known_captions"]          # captions from the file
    assert kw["color"] is not None
    calls.clear()
    run_qc(video, None, options=QCOptions(srt_path=FIXTURES / "sample_mistakes.srt", use_llm=False, disabled_checks=NO_AUDIO,
                                          check_color=False))
    assert calls[0]["color"] is None


def test_engine_falls_back_when_the_single_pass_fails(monkeypatch, video):
    def broken(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr("src.engine.read_video_once", broken)
    result = run_qc(video, None, options=QCOptions(srt_path=FIXTURES / "sample_mistakes.srt", use_llm=False, disabled_checks=NO_AUDIO))
    assert len(result.captions) == 20                                    # the run carries on
