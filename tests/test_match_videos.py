"""scripts/match_videos.py: filling the ground-truth "video" column from a folder of media files."""

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import match_videos  # noqa: E402


def write(path, header, rows):
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return path


def test_words_normalizes_loose_names():
    assert match_videos.words("Willy _ Mitzie Wedding - Rev 02") == \
        match_videos.words("Willy & Mitzie Wedding Video (Revision 02)")
    assert match_videos.words("David's") == match_videos.words("Davids") == {"david"}
    assert "video" not in match_videos.words("Promo Video")          # a word that says nothing
    assert "revision" in match_videos.words("rev")                   # rev == revision


def test_task_name_strips_the_todo_placeholder():
    assert match_videos.task_name("TODO #E3AC Wedding (Revision 01)") == "#E3AC Wedding (Revision 01)"
    assert match_videos.task_name("#E3AC Wedding") == "#E3AC Wedding"  # already a file name?


def test_matches_files_by_name_and_leaves_the_rest(tmp_path):
    media = tmp_path / "in"
    media.mkdir()
    for name in ["Willy _ Mitzie Wedding - Rev 02.mp4",
                 "kdl_gherabel_interview_4_reel_rev1.mp4",
                 "random_unrelated_clip.mp4"]:
        (media / name).write_bytes(b"")

    truth = write(tmp_path / "truth.csv",
                  ["video", "timestamp", "client", "issue_type", "note_text"], [
                      ["TODO #E3AC Willy & Mitzie Wedding Video | July 31, 2026 (Revision 02)",
                       "0:05", "E3AC", "color_grading", "x"],
                      ["TODO #E2AB KDL - Gherabel Interview #4 | Reel (Revision 01)",
                       "0:10", "E2AB", "color_grading", "x"],
                      ["TODO #E3SO Christina and Brendan 8.15.26 (Internal Revision 01)",
                       "1:42", "E3SO", "skin_tone", "x"],
                  ])
    videos = write(tmp_path / "videos.csv",
                   ["task_name", "client", "color_notes", "video_file"], [
                       ["#E3AC Willy & Mitzie Wedding Video | July 31, 2026 (Revision 02)",
                        "E3AC", 29, ""],
                   ])

    out, vout = tmp_path / "filled.csv", tmp_path / "filled_videos.csv"
    assert match_videos.match(truth, videos, [media], out, vout, 0.5) == 0

    filled = list(csv.DictReader(out.open(encoding="utf-8")))
    assert [r["video"] for r in filled] == [
        "Willy _ Mitzie Wedding - Rev 02.mp4",
        "kdl_gherabel_interview_4_reel_rev1.mp4",
        "TODO #E3SO Christina and Brendan 8.15.26 (Internal Revision 01)",   # no file collected
    ]
    assert list(csv.DictReader(vout.open(encoding="utf-8")))[0]["video_file"] == \
        "Willy _ Mitzie Wedding - Rev 02.mp4"


def test_revision_numbers_do_not_collide(tmp_path):
    media = tmp_path / "in"
    media.mkdir()
    for name in ["Willy _ Mitzie Wedding - Rev 01.mp4", "Willy _ Mitzie Wedding - Rev 02.mp4"]:
        (media / name).write_bytes(b"")

    truth = write(tmp_path / "truth.csv",
                  ["video", "timestamp", "client", "issue_type", "note_text"], [
                      ["TODO #E3AC Willy & Mitzie Wedding Video (Revision 01)",
                       "0:05", "E3AC", "color_grading", "x"],
                      ["TODO #E3AC Willy & Mitzie Wedding Video (Revision 02)",
                       "0:06", "E3AC", "color_grading", "x"],
                  ])
    out = tmp_path / "filled.csv"
    assert match_videos.match(truth, tmp_path / "missing.csv", [media], out, tmp_path / "v.csv", 0.5) == 0

    filled = list(csv.DictReader(out.open(encoding="utf-8")))
    assert [r["video"] for r in filled] == [
        "Willy _ Mitzie Wedding - Rev 01.mp4",
        "Willy _ Mitzie Wedding - Rev 02.mp4",
    ]


def test_a_bare_episode_number_names_one_task(tmp_path):
    media = tmp_path / "in"
    media.mkdir()
    for name in ["316.mp3", "318.mp3"]:
        (media / name).write_bytes(b"")

    truth = write(tmp_path / "truth.csv",
                  ["video", "timestamp", "client", "issue_type", "note_text"], [
                      ["TODO #2CS Episode 316 - Market - FX (INTERNAL REVISION)",
                       "0:20", "E2CS", "stutter", "x"],
                      ["TODO #E2CS Episode 318 - Warren Buffett - FX (Revision 01)",
                       "0:52", "E2CS", "stutter", "x"],
                  ])
    out = tmp_path / "filled.csv"
    assert match_videos.match(truth, tmp_path / "missing.csv", [media], out, tmp_path / "v.csv", 0.5) == 0

    filled = list(csv.DictReader(out.open(encoding="utf-8")))
    assert [r["video"] for r in filled] == ["316.mp3", "318.mp3"]


def test_macos_junk_from_a_zip_is_ignored(tmp_path):
    media = tmp_path / "in"
    (media / "__MACOSX").mkdir(parents=True)
    (media / "__MACOSX" / "._Episode 316 - Market - FX.mp3").write_bytes(b"junk")
    (media / "Episode 316 - Market - FX.mp3").write_bytes(b"")

    truth = write(tmp_path / "truth.csv",
                  ["video", "timestamp", "client", "issue_type", "note_text"], [
                      ["TODO #2CS Episode 316 - Market - FX (INTERNAL REVISION)",
                       "0:20", "E2CS", "stutter", "x"],
                  ])
    out = tmp_path / "filled.csv"
    assert match_videos.match(truth, tmp_path / "missing.csv", [media], out, tmp_path / "v.csv", 0.5) == 0

    filled = list(csv.DictReader(out.open(encoding="utf-8")))
    assert [r["video"] for r in filled] == ["Episode 316 - Market - FX.mp3"]


def test_two_revisions_of_one_episode_share_the_same_audio(tmp_path):
    media = tmp_path / "in"
    media.mkdir()
    (media / "E2CS_318.mp3").write_bytes(b"")

    truth = write(tmp_path / "truth.csv",
                  ["video", "timestamp", "client", "issue_type", "note_text"], [
                      ["TODO #E2CS Episode 318 - Warren Buffett - FX (Revision 01)",
                       "0:52", "E2CS", "stutter", "x"],
                      ["TODO #E2CS Episode 318 - Warren Buffett - FX (Revision Reel 5)",
                       "1:00", "E2CS", "stutter", "x"],
                  ])
    out = tmp_path / "filled.csv"
    assert match_videos.match(truth, tmp_path / "missing.csv", [media], out, tmp_path / "v.csv", 0.5) == 0

    filled = list(csv.DictReader(out.open(encoding="utf-8")))
    assert [r["video"] for r in filled] == ["E2CS_318.mp3", "E2CS_318.mp3"]


