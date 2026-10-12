import csv
from pathlib import Path

import pytest

from src.utils.timecode import (
    TimecodeError,
    format_timestamp,
    infer_fps,
    parse_range,
    parse_timestamp,
)

EXAMPLES_CSV = Path(__file__).resolve().parent.parent / "data" / "examples.csv"


# Real values from the QC review spreadsheet.
@pytest.mark.parametrize("text, fps, expected", [
    ("00:00:20:27", 30, 20.9),                 # 2CS Episode 316
    ("00:00:14:32", 60, 14 + 32 / 60),         # E2CS "the" came for few frames
    ("00:00:07:50", 60, 7 + 50 / 60),          # E2CS "picked up"
    ("000:00:25:11", 30, 25 + 11 / 30),        # E3AC, three-digit hours
    ("00:06:01:1", 30, 361 + 1 / 30),          # E3RP, single-digit frame
    ("00:01:47:19", 30, 107 + 19 / 30),        # E3JB "Are" -> "Or"
    ("00:02:12", 30, 132.0),                   # 2CS, HH:MM:SS
    ("0:05", 30, 5.0),                         # E2AB, M:SS
    ("13:06", 30, 786.0),                      # 2CS, MM:SS
    ("42: 34", 30, 2554.0),                    # E3HW, stray space
    ("1:02.5", 30, 62.5),
])
def test_parse_timestamp(text, fps, expected):
    assert parse_timestamp(text, fps) == pytest.approx(expected)


@pytest.mark.parametrize("text, fps, expected", [
    ("00;00;15;07", 30, 457 * 1001 / 30000),     # E8C "hormonal development", 29.97 DF
    ("00;00;13;49", 60, 829 * 1001 / 60000),     # E2JG "bright" came early, 59.94 DF
    ("00;01;00;02", 30, 1800 * 1001 / 30000),    # first label after the minute's dropped frames
    ("00;10;00;00", 30, 17982 * 1001 / 30000),   # tenth minute: nothing dropped
    ("00;30;40;36", 60, 110328 * 1001 / 60000),  # E2CS Episode 317
])
def test_parse_drop_frame(text, fps, expected):
    assert parse_timestamp(text, fps) == pytest.approx(expected)


def test_drop_frame_tracks_wall_clock():
    # Drop-frame exists so labels stay close to real time at 59.94 fps: after 30 minutes the
    # label is off by well under a frame, whereas counting frames at 60 fps would be ~1.8 s off.
    naive = 30 * 60 + 40 + 36 / 60
    assert parse_timestamp("00;30;40;36", 60) - naive == pytest.approx(0.0388, abs=1e-3)


@pytest.mark.parametrize("text, fps", [
    ("00:00:07:50", 30),   # frame 50 can't exist at 30 fps
    ("00;01;00;00", 30),   # label skipped by drop-frame
    ("00;00;15;07", 25),   # drop-frame only exists at 29.97 / 59.94
    ("35", 30),            # bare number: seconds or minutes?
    ("00:61:00", 30),
    ("0:75", 30),
    ("00;02;12", 30),      # ';' only valid in four-part timecode
    ("", 30),
    ("abc", 30),
    ("00:00:01:02.5", 30),
])
def test_parse_timestamp_rejects(text, fps):
    with pytest.raises(TimecodeError):
        parse_timestamp(text, fps)


@pytest.mark.parametrize("text, fps, expected", [
    ("13:20-21", 30, (800.0, 801.0)),                                    # 2CS shorthand
    ("1:42-1:48", 30, (102.0, 108.0)),                                   # E3SO
    ("0:46 -0:55", 30, (46.0, 55.0)),                                    # E3AC
    ("0:02:34:14 — 0:02:41:02", 30, (154 + 14 / 30, 161 + 2 / 30)),  # em dash
    ("00:02:13:41–00:03:10:13", 60, (133 + 41 / 60, 190 + 13 / 60)),  # en dash
    ("00:00:42:09 to 00:00:42:12", 30, (42.3, 42.4)),                    # E3ME
    ("00;13;49;17-00;13;50;26", 30, (24863 * 1001 / 30000, 24902 * 1001 / 30000)),  # E3AC
    ("0:05", 30, (5.0, 5.0)),                                            # single value
])
def test_parse_range(text, fps, expected):
    start, end = parse_range(text, fps)
    assert start == pytest.approx(expected[0])
    assert end == pytest.approx(expected[1])


def test_parse_range_onwards():
    assert parse_range("2:45 onwards") == (165.0, None)    # ECRS


@pytest.mark.parametrize("text", ["35-36", "1:00-0:30", "0:05 - ", "1:00-2:00-3:00"])
def test_parse_range_rejects(text):
    with pytest.raises(TimecodeError):
        parse_range(text)


@pytest.mark.parametrize("text, expected", [
    ("00:00:07:50", 60),
    ("00:00:20:27", 30),
    ("00;00;13;49", 60),
    ("00:02:13:41–00:03:10:13", 60),
    ("0:05", 30),
])
def test_infer_fps(text, expected):
    assert infer_fps(text, 30) == expected


@pytest.mark.parametrize("seconds, expected", [
    (14.5333, "00:14.533"),
    (0, "00:00.000"),
    (786, "13:06.000"),
    (3725.5, "1:02:05.500"),
])
def test_format_timestamp(seconds, expected):
    assert format_timestamp(seconds) == expected


@pytest.mark.skipif(not EXAMPLES_CSV.exists(), reason="run scripts/prepare_data.py first")
def test_every_example_timestamp_parses():
    with EXAMPLES_CSV.open(encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["video_timestamp"]]
    assert rows
    for row in rows:
        start, _ = parse_range(row["video_timestamp"], infer_fps(row["video_timestamp"], 30))
        assert start == pytest.approx(float(row["start_sec"]), abs=1e-3)
