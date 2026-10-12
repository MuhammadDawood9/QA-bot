from pathlib import Path

import pytest

from src.inputs.srt_loader import SrtError, load_srt, parse_srt

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample_mistakes.srt"


def test_loads_fixture():
    captions = load_srt(FIXTURE)
    assert len(captions) == 20
    first = captions[0]
    assert (first.id, first.start, first.end, first.text, first.source) == (
        1, 0.5, 2.4, "So the market picked up", "srt")


def test_formatting_tags_are_removed():
    captions = load_srt(FIXTURE)
    assert captions[18].text == "in the realM of august, leo."


def test_multiline_caption_keeps_line_break():
    [caption] = parse_srt("1\n00:00:01,000 --> 00:00:03,000\nfirst line\n<b>second</b> line\n")
    assert caption.text == "first line\nsecond line"
    assert caption.flat_text == "first line second line"
    assert len(caption.words) == 4


def test_crlf_bom_and_period_milliseconds(tmp_path):
    path = tmp_path / "windows.srt"
    path.write_bytes("﻿1\r\n00:00:01.5 --> 00:00:02.25\r\nHello there\r\n\r\n".encode("utf-8"))
    [caption] = load_srt(path)
    assert (caption.id, caption.start, caption.end, caption.text) == (1, 1.5, 2.25, "Hello there")


def test_non_ascii_text(tmp_path):
    path = tmp_path / "unicode.srt"
    path.write_text("1\n00:00:01,000 --> 00:00:02,000\nIt’s “fine” — café\n",
                    encoding="utf-8")
    assert load_srt(path)[0].text == "It’s “fine” — café"


def test_malformed_blocks_are_skipped_with_warning():
    content = (
        "1\n00:00:01,000 --> 00:00:02,000\nGood one\n\n"
        "2\nno timing here\n\n"
        "3\n00:00:05,000 --> 00:00:04,000\nBackwards\n\n"
        "00:00:06,000 --> 00:00:07,000\nMissing index\n"
    )
    with pytest.warns(UserWarning):
        captions = parse_srt(content)
    assert [(c.id, c.text) for c in captions] == [(1, "Good one"), (2, "Missing index")]


def test_missing_file():
    with pytest.raises(SrtError):
        load_srt(Path("does-not-exist.srt"))
