"""Captions out of sync, late captions, speech with no caption, and caption names vs the
video's own graphics. Fake Whisper words and fake OCR, modelled on the first real client video
(captions 3 s late, captions stopping after 8 s, "Meyer" under a "MAIER" logo)."""

from pathlib import Path

import numpy as np

from src.checks import screen_text, transcript
from src.checks.context import CheckContext
from src.inputs.caption_builder import TextBox
from src.inputs.video_ocr import drop_long_text, read_screen_text
from src.models import (CAPTION_EARLY, CAPTION_LATE, CAPTION_SYNC, MISSING_WORD, SCREEN_TEXT_MISMATCH,
                        UNCAPTIONED_SPEECH, WRONG_WORD, Caption, Issue)
from src.report.builder import ReportInfo, build_report
from tests.helpers import CONFIG, context
from tests.test_transcript import said

SETTINGS = {**CONFIG["checks"]["transcript"], "enabled": True}
SCREEN = {**CONFIG["checks"]["screen_text"], "enabled": True}


def run(caps, words, **overrides):
    return transcript.check(caps, {**SETTINGS, **overrides}, CheckContext(transcript=words))


def types(issues):
    return [(i.caption_id, i.type) for i in issues]


# The real video, simplified: four phrases, and the captions show them 3 s late.
SPEECH = (said("Hi I'm Elizabeth Meyer", 0.0) + said("I'm a wife I'm a mom of four", 1.5)
          + said("I'm a neighbor", 4.0))
LATE = [Caption(1, 3.0, 4.4, "Hi, I'm Elizabeth Meyer,"),
        Caption(2, 4.5, 6.9, "I'm a wife, I'm a mom of four,"),
        Caption(3, 7.0, 8.0, "I'm a neighbor,")]


# --- whole-video sync ------------------------------------------------------------------------

def test_captions_all_late_are_reported_once_as_out_of_sync():
    issues = run(LATE, SPEECH)
    sync = [i for i in issues if i.type == CAPTION_SYNC]
    assert len(sync) == 1
    assert "about 3.0s late" in sync[0].message and sync[0].severity == "high"
    assert "Move every caption 3.0s earlier" in sync[0].suggestion
    assert not [i for i in issues if i.type in (CAPTION_LATE, CAPTION_EARLY, MISSING_WORD, WRONG_WORD)]


def test_wrong_words_are_still_found_in_late_captions_and_point_at_the_real_time():
    caps = [*LATE[:2], Caption(3, 7.0, 8.0, "I'm a neighbour friend,")]
    words = SPEECH[:-3] + said("I'm a neighbor", 4.0)
    issues = run(caps, words)
    assert CAPTION_SYNC in [i.type for i in issues]
    # "neighbour" is the same word ("neighbor") to the fuzzy match; "friend" isn't said
    extra = [i for i in issues if i.caption_id == 3 and i.type != CAPTION_SYNC]
    assert extra and all(i.timestamp == 7.0 for i in extra)


def test_captions_in_sync_report_no_shift():
    caps = [Caption(1, 0.0, 1.4, "Hi, I'm Elizabeth Meyer,"), Caption(2, 1.5, 3.9, "I'm a wife, I'm a mom of four,")]
    assert CAPTION_SYNC not in [i.type for i in run(caps, SPEECH[:-3])]


def test_sync_can_be_switched_off():
    issues = run(LATE, SPEECH, max_offset_sec=0)
    assert CAPTION_SYNC not in [i.type for i in issues]


def test_captions_all_early_are_reported_as_early():
    early = [Caption(c.id, c.start - 5.0, c.end - 5.0, c.text) for c in LATE]   # 2 s early
    sync = [i for i in run(early, SPEECH) if i.type == CAPTION_SYNC]
    assert sync and "about 2.0s early" in sync[0].message and "later" in sync[0].suggestion


# --- one late caption ------------------------------------------------------------------------

def test_one_caption_coming_up_late_is_flagged():
    words = said("Welcome back to the channel", 0.0) + said("We need to talk about the budget", 3.0)
    caps = [Caption(1, 0.0, 2.0, "Welcome back to the channel"),
            Caption(2, 4.3, 6.0, "We need to talk about the budget")]
    issues = run(caps, words, max_offset_sec=0)
    late = [i for i in issues if i.type == CAPTION_LATE]
    assert types(late) == [(2, CAPTION_LATE)]
    assert "1.3s after" in late[0].message and late[0].severity == "medium"
    assert "Start the caption at 00:03.000" in late[0].suggestion


# --- speech with no caption ------------------------------------------------------------------

def test_speech_after_the_captions_stop_is_flagged_with_its_words():
    words = said("Hi there", 0.0) + said("and I'm someone who believes the best leaders listen", 2.0)
    caps = [Caption(1, 0.0, 0.7, "Hi there")]
    [issue] = [i for i in run(caps, words) if i.type == UNCAPTIONED_SPEECH]
    assert issue.caption_id == -1 and issue.original == "(no caption)"
    assert issue.timestamp == 2.0 and issue.severity == "medium"           # 2.7 s of speech
    assert "9 spoken words with no caption" in issue.message
    assert "and I'm someone who believes the best leaders listen" in issue.suggestion


def test_the_next_uncaptioned_word_is_not_also_a_missing_word():
    words = said("I'm a neighbor", 0.0) + said("And I'm someone who believes in listening", 0.95)
    caps = [Caption(1, 0.0, 0.9, "I'm a neighbor,")]
    issues = run(caps, words)
    assert [i.type for i in issues] == [UNCAPTIONED_SPEECH]
    assert issues[0].suggestion.startswith('Add captions: "And')


def test_short_uncaptioned_bits_are_not_flagged():
    words = said("Hi there", 0.0) + said("oh well", 3.0)          # 2 words, 0.6 s
    assert not [i for i in run([Caption(1, 0.0, 0.7, "Hi there")], words) if i.type == UNCAPTIONED_SPEECH]


def test_separate_stretches_get_separate_negative_ids():
    words = said("one two three four five six", 0.0) + said("seven eight nine ten eleven twelve", 10.0)
    issues = [i for i in run([Caption(1, 5.0, 6.0, "unrelated title")], words) if i.type == UNCAPTIONED_SPEECH]
    assert [i.caption_id for i in issues] == [-1, -2]


def test_unclear_speech_is_not_flagged():
    words = said("mumble mumble mumble mumble mumble mumble", 0.0, prob=0.3)
    assert not run([Caption(1, 5.0, 6.0, "Hello")], words)


# --- names vs on-screen graphics -------------------------------------------------------------

def screen_run(captions, texts, **overrides):
    ctx = context()
    ctx.screen_text = texts
    return screen_text.check(captions, {**SCREEN, **overrides}, ctx)


LOGO = ["ELIZABETH\nMAIER\nfor STATE REPRESENTATIVE"]


def test_name_spelled_differently_from_the_logo_is_flagged():
    [issue] = screen_run([Caption(1, 3.2, 4.6, "Hi, I'm Elizabeth Meyer,")], LOGO)
    assert issue.type == SCREEN_TEXT_MISMATCH and issue.severity == "high"   # "Elizabeth" matches the logo
    assert '"Meyer" is spelled "MAIER"' in issue.message
    assert issue.suggestion == "Hi, I'm Elizabeth Maier,"


def test_name_alone_is_medium():
    [issue] = screen_run([Caption(1, 0, 1, "Vote for Meyer today")], LOGO)
    assert issue.severity == "medium" and issue.suggestion == "Vote for Maier today"


def test_matching_names_and_common_words_are_not_flagged():
    caps = [Caption(1, 0, 1, "Hi, I'm Elizabeth Maier, for State Representative"),
            Caption(2, 1, 2, "The State fair and the Stats were great")]
    assert screen_run(caps, LOGO) == []


def test_lowercase_words_and_unrelated_names_are_not_flagged():
    caps = [Caption(1, 0, 1, "i met meyer"), Caption(2, 1, 2, "Hi, I'm Jennifer Lopez")]
    assert screen_run(caps, LOGO) == []
    assert len(screen_run([Caption(1, 0, 1, "i met meyer")], LOGO, require_capital=False)) == 1


def test_possessive_is_checked_on_the_name():
    [issue] = screen_run([Caption(1, 0, 1, "Elizabeth Meyer's plan")], LOGO)
    assert issue.suggestion == "Elizabeth Maier's plan"


def test_no_screen_text_means_no_check():
    assert screen_run([Caption(1, 0, 1, "Elizabeth Meyer")], []) == []
    ctx = context()
    assert screen_text.check([Caption(1, 0, 1, "Elizabeth Meyer")], SCREEN, ctx) == []


# --- reading on-screen text, and long text that isn't a caption ------------------------------

def test_text_on_screen_for_the_whole_video_is_not_a_caption():
    caps = [Caption(1, 0.0, 29.4, "ELIZABETH MAIER", source="ocr"), Caption(2, 3.2, 4.6, "Hi there", source="ocr")]
    assert [(c.id, c.text) for c in drop_long_text(caps, {"max_caption_sec": 12})] == [(1, "Hi there")]
    assert len(drop_long_text(caps, {"max_caption_sec": 0})) == 2


def test_read_screen_text_skips_the_caption_region_and_the_captions():
    frame = np.zeros((1920, 1080, 3), dtype=np.uint8)

    def engine(image):
        return [TextBox("ELIZABETH MAIER", 0.99, 100, 40, 900, 120),          # logo, top
                TextBox("Hi, I'm Elizabeth Meyer,", 0.98, 200, 1530, 880, 1580),  # caption, in the region
                TextBox("PAID FOR BY MAIER", 0.95, 300, 1850, 800, 1890)]      # small print, below it

    region = {"left": 0.0, "top": 0.72, "width": 1.0, "height": 0.16}
    caps = [Caption(1, 3.2, 4.6, "Hi, I'm Elizabeth Meyer,", source="ocr")]
    frames = iter([(0.0, 30.0, frame), (2.0, 30.0, frame)])
    texts = read_screen_text(Path("del.mp4"), {"region": region, "screen_text_every_sec": 2.0}, caps,
                             engine=engine, frames=frames)
    assert texts == ["ELIZABETH MAIER", "PAID FOR BY MAIER"]                 # each once


def test_read_screen_text_in_full_frame_mode_drops_text_that_is_a_caption():
    frame = np.zeros((100, 100, 3), dtype=np.uint8)

    def engine(image):
        return [TextBox("ELIZABETH MAIER", 0.99, 10, 5, 90, 12), TextBox("Hi there friend", 0.99, 10, 80, 90, 88)]

    caps = [Caption(1, 0, 1, "Hi there friend", source="ocr")]
    texts = read_screen_text(Path("x.mp4"), {"region": "full"}, caps, engine=engine,
                             frames=iter([(0.0, 1.0, frame)]))
    assert texts == ["ELIZABETH MAIER"]


def test_read_screen_text_off():
    assert read_screen_text(Path("x.mp4"), {"screen_text_every_sec": 0}, []) == []


# --- the report with an issue that has no caption ---------------------------------------------

def test_report_shows_speech_with_no_caption(tmp_path):
    caps = [Caption(1, 0.0, 1.0, "Hi there")]
    issues = [Issue(caption_id=-1, timestamp=5.0, type=UNCAPTIONED_SPEECH, severity="high",
                    original="(no caption)", message="12 spoken words with no caption.",
                    suggestion='Add captions: "we deserve more"', confidence=0.9)]
    settings = {**CONFIG["report"], "output_dir": str(tmp_path), "formats": ["md", "docx"]}
    written = build_report(caps, issues, ReportInfo(srt=Path("c.srt")), settings)
    md = written[0].read_text(encoding="utf-8")
    assert "### 00:05.000, no caption" in md and '<a id="no-caption-1"></a>' in md
    assert "Speech with no caption" in md and "#-1" not in md
    from docx import Document
    issues_table = Document(str(written[1])).tables[1]
    row = [c.text for c in issues_table.rows[1].cells]
    assert row[0] == "00:05.000" and row[2] == "(no caption)" and row[3].startswith("Speech with no caption")
