"""Reading burned-in captions: grouping OCR readings into captions, filtering non-caption text,
change detection, OCR doubt on issues, and the CLI path without an SRT.

The OCR engine and the frames are faked here; the slow test at the bottom runs the real OCR
on the test videos from scripts/make_burned_in_samples.py.
"""

import shutil
from pathlib import Path

import numpy as np
import pytest

from src import main as cli
from src.config import ocr_settings
from src.inputs.caption_builder import Reading, TextBox, build_captions, continues, final_text, group_boxes
from src.inputs.srt_loader import format_srt, load_srt, parse_srt, write_srt
from src.inputs.video_ocr import (OcrError, ReadingFilter, changed, crop, parse_region, read_frames,
                                  thumbnail, to_frame_boxes)
from src.models import PUNCTUATION, SPELLING, TOO_SHORT, Caption, Issue
from tests.helpers import CONFIG

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"


def box(text, left=0.3, top=0.8, right=0.7, bottom=0.85, score=0.99):
    return TextBox(text=text, score=score, left=left, top=top, right=right, bottom=bottom)


def reading(text, score=0.99, top=0.8):
    return Reading(text=text, score=score, top=top, bottom=top + 0.05)


def frames(*texts_per_frame, step=0.2):
    """[["We"], ["We need"], []] -> [(0.0, [Reading]), (0.2, [Reading]), (0.4, [])]"""
    out = []
    for n, texts in enumerate(texts_per_frame):
        out.append((round(n * step, 3), [r if isinstance(r, Reading) else reading(r) for r in texts]))
    return out


# --- boxes -> blocks -------------------------------------------------------------------------

def test_highlighted_word_in_its_own_box_joins_its_line():
    readings = group_boxes([box("budget", left=0.6, right=0.75), box("We need to talk about", left=0.1, right=0.55)])
    assert [r.text for r in readings] == ["We need to talk about budget"]


def test_two_close_lines_are_one_caption_and_a_far_title_is_separate():
    readings = group_boxes([
        box("DAY 07", left=0.02, top=0.03, right=0.12, bottom=0.07),
        box("first line", top=0.80, bottom=0.85),
        box("second line", top=0.86, bottom=0.91),
    ])
    assert [r.text for r in readings] == ["DAY 07", "first line\nsecond line"]


def test_reading_score_is_weighted_by_text_length():
    (r,) = group_boxes([box("a", left=0.1, right=0.15, score=0.5), box("long words here", left=0.2, score=1.0)])
    assert r.score > 0.9


# --- readings -> captions --------------------------------------------------------------------

def test_static_caption_becomes_one_caption_with_its_times():
    caps = build_captions(frames([], ["Hello there"], ["Hello there"], ["Hello there"], []), 0.2)
    assert len(caps) == 1
    c = caps[0]
    assert (c.start, c.end, c.text, c.source) == (0.2, 0.8, "Hello there", "ocr")
    assert c.ocr_confidence == pytest.approx(0.99)


def test_word_by_word_animation_is_one_caption_with_the_full_text():
    caps = build_captions(frames(["We"], ["We need"], ["We need to"], ["We need to"],
                                 ["talk about"], ["talk about"]), 0.2)
    assert [(c.text, c.start) for c in caps] == [("We need to", 0.0), ("talk about", 0.8)]


def test_growth_still_found_when_the_ocr_drops_a_space():
    caps = build_captions(frames(["It"], ["Itwas"], ["It was hard,"], ["It was hard,"]), 0.2)
    assert [c.text for c in caps] == ["It was hard,"]


def test_next_caption_starting_with_the_same_word_is_a_new_caption():
    caps = build_captions(frames(["the best"], ["the best"], ["the"], ["the end"], ["the end"]), 0.2)
    assert [c.text for c in caps] == ["the best", "the end"]


def test_ocr_noise_in_one_frame_is_outvoted():
    caps = build_captions(frames(["Sales picked up"], ["Sa1es picked up"], ["Sales picked up"]), 0.2)
    assert caps[0].text == "Sales picked up"
    assert caps[0].ocr_confidence == pytest.approx(2 / 3 * 0.99, abs=0.01)


def test_a_stray_extra_word_in_one_frame_does_not_win():
    text, _ = final_text([reading("in the summer"), reading("in the summer"), reading("in the summer ii")])
    assert text == "in the summer"


def test_short_dropout_inside_a_caption_is_bridged_but_a_long_one_splits():
    bridged = build_captions(frames(["Hello there"], [], ["Hello there"]), 0.2, {"max_gap_sec": 0.5})
    assert len(bridged) == 1
    split = build_captions(frames(["Hello there"], [], [], [], [], ["Hello there"]), 0.2, {"max_gap_sec": 0.5})
    assert len(split) == 2


def test_title_and_caption_on_screen_together_are_tracked_separately():
    title = reading("DAY 07", top=0.03)
    caps = build_captions(frames([title, "first caption"], [title, "first caption"],
                                 [title, "second one"], [title, "second one"]), 0.2)
    assert sorted(c.text for c in caps) == ["DAY 07", "first caption", "second one"]
    assert next(c for c in caps if c.text == "DAY 07").end == pytest.approx(0.8)


def test_one_unsure_frame_is_noise_but_one_sure_frame_is_a_flash_caption():
    assert build_captions(frames([reading("Hi", score=0.6)]), 0.2) == []
    (flash,) = build_captions(frames([reading("Hi", score=0.95)]), 0.2)
    assert flash.duration == pytest.approx(0.2)        # the too_short check flags this one


def test_continues_scores():
    assert continues("We need", "We need", 85) == 100
    assert 0 < continues("We need", "We need to talk", 85) < 100
    assert continues("We need to talk", "We need", 85) == 0      # shrinking is not growth
    assert continues("Hello", "Goodbye", 85) == 0


# --- filtering what isn't a caption ----------------------------------------------------------

def test_filter_drops_a_lone_unsure_letter_but_keeps_a_sure_one():
    keep = ReadingFilter({})
    assert keep([box("O", score=0.55)]) == []
    assert [r.text for r in keep([box("I", score=0.95)])] == ["I"]


def test_filter_ignore_patterns_match_with_or_without_spaces():
    keep = ReadingFilter({"ignore_patterns": [r"day \d+"]})
    assert keep([box("DAY 07")]) == []
    assert keep([box("DAY07")]) == []
    assert [r.text for r in keep([box("Day one was hard")])] == ["Day one was hard"]


def test_filter_ignore_regions():
    keep = ReadingFilter({"ignore_regions": [{"left": 0.0, "top": 0.0, "width": 0.3, "height": 0.2}]})
    assert keep([box("p21", left=0.05, top=0.05, right=0.1, bottom=0.08)]) == []
    assert len(keep([box("caption text")])) == 1


def test_filter_drops_unsure_boxes_and_punctuation_only():
    keep = ReadingFilter({"min_box_score": 0.5})
    assert keep([box("faded text", score=0.3), box("...", top=0.5, bottom=0.55)]) == []


def test_bad_ignore_pattern_is_a_clear_error():
    with pytest.raises(OcrError, match="ignore_patterns"):
        ReadingFilter({"ignore_patterns": ["day ("]})


# --- regions, cropping, change detection -----------------------------------------------------

def test_parse_region():
    assert parse_region("full") == {"left": 0.0, "top": 0.0, "width": 1.0, "height": 1.0}
    assert parse_region({"left": 0.1, "top": 0.6, "width": 0.8, "height": 0.4})["top"] == 0.6
    with pytest.raises(OcrError, match="outside the frame"):
        parse_region({"left": 0.5, "top": 0.6, "width": 0.8, "height": 0.4})
    with pytest.raises(OcrError, match="left, top, width and height"):
        parse_region({"left": 0.1})
    with pytest.raises(OcrError):
        parse_region("bottom")


def test_crop_and_box_positions_back_in_frame_fractions():
    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    region = {"left": 0.5, "top": 0.5, "width": 0.5, "height": 0.5}
    part = crop(frame, region)
    assert part.shape == (50, 100, 3)
    (b,) = to_frame_boxes([TextBox("x y", 1.0, left=0, top=0, right=100, bottom=25)], region, 100, 50)
    assert (b.left, b.top, b.right, b.bottom) == (0.5, 0.5, 1.0, 0.75)


def test_one_new_word_counts_as_a_change_even_in_a_full_frame():
    a = np.full((1080, 1920, 3), 40, dtype=np.uint8)
    b = a.copy()
    b[900:960, 900:1040] = 255                      # one word-sized white patch
    noise = a.copy()
    noise[::2, ::2] += 2                            # compression-like noise
    assert changed(thumbnail(a), thumbnail(b), 6.0)
    assert not changed(thumbnail(a), thumbnail(noise), 6.0)
    assert changed(None, thumbnail(a), 6.0)


def test_read_frames_runs_the_ocr_only_when_the_picture_changes():
    blank = np.zeros((90, 160, 3), dtype=np.uint8)
    with_text = blank.copy()
    with_text[60:80, 40:120] = 255
    calls = []

    def engine(image):
        calls.append(image.shape)
        return [TextBox("Hello there", 0.99, 40, 60, 120, 80)] if image.max() > 0 else []

    video_frames = [(n * 0.2, 2.0, img) for n, img in
                    enumerate([blank, blank, with_text, with_text, with_text, blank])]
    caps = read_frames(Path("x.mp4"), {"sample_fps": 5, "region": "full"}, engine, iter(video_frames))
    assert len(calls) == 3                          # blank, text appears, text goes
    assert [(c.text, c.start, c.end) for c in caps] == [("Hello there", 0.4, 1.0)]


# --- OCR doubt on issues ---------------------------------------------------------------------

def _issue(kind=SPELLING, confidence=0.9):
    return Issue(caption_id=1, timestamp=0.0, type=kind, severity="medium", original="helth",
                 message="Possible misspelling.", suggestion="health", confidence=confidence)


def test_issue_on_an_unsure_ocr_caption_is_marked_doubtful():
    cap = Caption(1, 0, 1, "good helth", source="ocr", ocr_confidence=0.6)
    out = cli.mark_ocr_doubt(_issue(), cap, 0.75)
    assert (out.severity, out.confidence) == ("low", 0.6)
    assert "OCR wasn't sure" in out.message


def test_issue_on_a_sure_ocr_caption_or_an_srt_caption_is_untouched():
    issue = _issue()
    assert cli.mark_ocr_doubt(issue, Caption(1, 0, 1, "good helth", source="ocr", ocr_confidence=0.95), 0.75) is issue
    assert cli.mark_ocr_doubt(issue, Caption(1, 0, 1, "good helth"), 0.75) is issue


def test_ocr_weak_spot_types_are_always_doubtful_and_timing_issues_never():
    cap = Caption(1, 0, 1, "Thanks for watching.", source="ocr", ocr_confidence=0.99)
    out = cli.mark_ocr_doubt(_issue(PUNCTUATION), cap, 0.75, ("punctuation", "spacing"))
    assert (out.severity, out.confidence) == ("low", 0.9)
    assert "commas and full stops" in out.message
    unsure = Caption(1, 0, 1, "Hi", source="ocr", ocr_confidence=0.3)
    timing = _issue(TOO_SHORT)
    assert cli.mark_ocr_doubt(timing, unsure, 0.75, ("punctuation",)) is timing


def test_client_ocr_block_overrides_the_defaults():
    settings = ocr_settings(CONFIG, {"ocr": {"region": {"left": 0, "top": 0.7, "width": 1, "height": 0.3}}})
    assert settings["region"]["top"] == 0.7
    assert settings["sample_fps"] == CONFIG["ocr"]["sample_fps"]
    assert ocr_settings(CONFIG, {})["region"] == "full"


def test_every_client_ocr_block_is_valid():
    from src.config import load_client
    for path in sorted((Path(CONFIG["_root"]) / "clients").glob("*.yaml")):
        settings = ocr_settings(CONFIG, load_client(CONFIG, path.stem))
        parse_region(settings.get("region"))
        ReadingFilter(settings)


# --- SRT writing and the CLI without an SRT --------------------------------------------------

def test_srt_round_trip():
    caps = [Caption(1, 0.2, 1.4, "Hello there", source="ocr"), Caption(2, 61.05, 62.5, "two\nlines", source="ocr")]
    text = format_srt(caps)
    assert "00:01:01,050 --> 00:01:02,500" in text
    back = parse_srt(text)
    assert [(c.start, c.end, c.text) for c in back] == [(0.2, 1.4, "Hello there"), (61.05, 62.5, "two\nlines")]


def test_cli_reads_captions_from_the_video_without_an_srt(tmp_path, monkeypatch, capsys):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"not really a video")
    caps = [Caption(1, 0.0, 2.0, "Thank", source="ocr", ocr_confidence=0.98),
            Caption(2, 2.1, 4.0, "good helth habits", source="ocr", ocr_confidence=0.6)]
    seen = {}

    def fake_read(path, settings, cache_dir, **kwargs):
        seen["path"], seen["settings"] = path, settings
        return caps

    monkeypatch.setattr("src.engine.read_burned_in_captions", fake_read)
    monkeypatch.setattr("src.engine.get_transcript", lambda *a, **kw: [])      # no Whisper on the fake video
    saved = tmp_path / "out.srt"
    code = cli.main(["--video", str(video), "--no-llm", "--save-srt", str(saved)])
    out = capsys.readouterr().out
    assert code == 0
    assert seen["path"] == video and "region" in seen["settings"]
    assert "clip.mp4 (captions read from the video)" in out
    assert "OCR wasn't sure" in out                     # the misspelling on the unsure caption
    assert [c.text for c in load_srt(saved)] == ["Thank", "good helth habits"]


def test_cli_needs_a_video_or_an_srt(capsys):
    with pytest.raises(SystemExit):
        cli.main([])
    assert "--video" in capsys.readouterr().err


def test_cli_reports_an_unreadable_video(tmp_path, capsys):
    assert cli.main(["--video", str(tmp_path / "missing.mp4"), "--no-llm"]) == 2
    assert "file not found" in capsys.readouterr().err.lower()


# --- the real OCR on the test videos ---------------------------------------------------------

@pytest.mark.slow
@pytest.mark.parametrize("style, settings", [
    ("box", {"region": {"left": 0.0, "top": 0.55, "width": 1.0, "height": 0.4}}),
    ("subtitle", {"ignore_patterns": [r"day \d+"]}),
    ("wordbyword", {"region": {"left": 0.0, "top": 0.6, "width": 1.0, "height": 0.25}}),
])
def test_real_ocr_on_the_burned_in_test_videos(style, settings):
    video = SAMPLES / f"burned_{style}.mp4"
    if not video.exists():
        pytest.skip("run: python scripts/make_sample_video.py, then python scripts/make_burned_in_samples.py")
    from src.inputs.video_ocr import read_burned_in_captions

    caps = read_burned_in_captions(video, ocr_settings(CONFIG, {"ocr": settings}), None)
    truth = load_srt(SAMPLES / f"burned_{style}_truth.srt")
    norm = lambda t: " ".join(t.lower().replace(",", "").replace(".", "").split())   # noqa: E731
    found = {norm(c.text) for c in caps}
    matched = [t for t in truth if norm(t.text) in found]
    assert len(matched) >= 0.8 * len(truth), [c.text for c in caps]
    assert len(caps) <= len(truth) + 2              # few made-up extra captions
    for t in matched:                               # timing within two sampled frames
        c = next(c for c in caps if norm(c.text) == norm(t.text))
        assert abs(c.start - t.start) <= 0.45
