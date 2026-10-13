"""scripts/evaluate.py: reading the ground truth, matching flags to notes, and the scores.
Plus the color-space and HDR handling the color measurements rely on."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import evaluate  # noqa: E402
from src.models import Issue, shot_issue_id  # noqa: E402


def flag(video, shot, kind, start, end):
    issue = Issue(shot_issue_id(shot), (start + end) / 2, kind, "medium", f"Shot {shot}", "msg", "", 0.6)
    return evaluate.Flag(video, issue, start, end)


def truth(tmp_path, rows):
    path = tmp_path / "gt.csv"
    path.write_text("video,timestamp,client,issue_type,note_text\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def test_reading_the_ground_truth(tmp_path):
    notes, problems = evaluate.read_truth(truth(tmp_path, [
        "a.mp4,00:00:19:59,E3AC,overexposed,too bright",
        "a.mp4,0:29,e3ac,,fix color grade",                 # empty type = any color
        "a.mp4,2:04-2:15,E3AC,skin_tone,skin",              # a range
        "a.mp4,,E3AC,color_grading,fix the grade overall",  # no time: the whole video
        "a.mp4,soon,E3AC,skin_tone,bad stamp",
        "a.mp4,0:40,E3AC,stabilize,not a color type",
        ",0:10,,overexposed,no video",
    ]), fps=30)
    assert [n.kind for n in notes] == ["overexposed", "color_grading", "skin_tone", "color_grading",
                                       "skin_tone", "color_grading"]
    assert notes[0].start == pytest.approx(19 + 59 / 60)                # frame 59: a 60 fps timeline
    assert (notes[1].start, notes[1].client) == (29.0, "E3AC")
    assert (notes[2].start, notes[2].end) == (124.0, 135.0)
    assert notes[3].start is None and notes[4].start is None
    assert len(problems) == 3


def test_missing_columns_are_an_error(tmp_path):
    path = tmp_path / "gt.csv"
    path.write_text("video,timestamp\na.mp4,0:10\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="missing the column"):
        evaluate.read_truth(path, 30)


def test_matching_uses_the_shot_span_plus_tolerance():
    notes = [evaluate.Note("a.mp4", "0:11", "", "overexposed", "", 11.0, 11.0),
             evaluate.Note("a.mp4", "0:30", "", "skin_tone", "", 30.0, 30.0)]
    flags = [flag("a.mp4", 1, "overexposed", 4.0, 9.5), flag("a.mp4", 2, "skin_tone", 40.0, 50.0)]
    evaluate.match(notes, flags, tolerance=2.0)
    assert notes[0].matched_by and not notes[1].matched_by              # 11 is within 9.5 + 2
    assert flags[0].matches and not flags[1].matches


def test_scores_per_type_and_overall():
    notes = [evaluate.Note("a", "", "", "overexposed", "", 10, 10), evaluate.Note("a", "", "", "skin_tone", "", 30, 30),
             evaluate.Note("a", "", "", "color_grading", "", 50, 50), evaluate.Note("a", "", "", "color_cast", "", 70, 70)]
    flags = [flag("a", 1, "overexposed", 8, 12), flag("a", 2, "color_cast", 28, 32),     # wrong type at 30
             flag("a", 3, "saturation", 48, 52), flag("a", 4, "overexposed", 90, 95)]   # generic note; nothing
    evaluate.match(notes, flags, tolerance=0)
    rows = {r["type"]: r for r in evaluate.score(notes, flags, minutes=2.0)}
    assert (rows["overexposed"]["recall"], rows["overexposed"]["precision"]) == (1.0, 0.5)
    assert rows["overexposed"]["fp_per_min"] == 0.5
    assert rows["skin_tone"]["recall"] == 0.0                                 # only a color_cast flag there
    assert rows["color_cast"]["precision"] == 0.0 and rows["color_cast"]["recall"] == 0.0
    assert rows["saturation"]["precision"] == 1.0                             # backed by the generic note
    overall = rows[evaluate.ALL]
    assert (overall["notes"], overall["found"], overall["flags"], overall["true"]) == (4, 3, 4, 3)


def caption_flag(video, caption_id, kind, start, end):
    issue = Issue(caption_id, start, kind, "medium", "text", "msg", "", 0.8)
    return evaluate.Flag(video, issue, start, end)


def test_caption_note_types_and_aliases(tmp_path):
    notes, problems = evaluate.read_truth(truth(tmp_path, [
        "a.mp4,0:07,E2CS,spelling,peaked up",
        "a.mp4,0:08,E2CS,capitalization,the bot's own type name",
        "a.mp4,0:14,E2CS,too_short_caption,the spreadsheet's category name",
        "a.mp4,0:20,E2CS,caption_late,a bot timing type",
        "a.mp4,0:30,E2CS,caption,any caption problem",
    ]), fps=30)
    assert [n.kind for n in notes] == ["spelling", "punctuation", "too_short", "timing", "caption"]
    assert {n.area for n in notes} == {"caption"} and not problems


def test_caption_notes_match_only_caption_flags():
    notes = [evaluate.Note("a", "0:10", "", "spelling", "", 10, 10),
             evaluate.Note("a", "0:10", "", "overexposed", "", 10, 10)]
    flags = [caption_flag("a", 3, "wrong_word", 9.5, 11.0), flag("a", 1, "overexposed", 8, 12)]
    evaluate.match(notes, flags, tolerance=0)
    assert [i.type for i in notes[0].matched_by] == ["wrong_word"]            # not the color flag
    assert [i.type for i in notes[1].matched_by] == ["overexposed"]


def test_caption_scores_by_family():
    notes = [evaluate.Note("a", "", "", "spelling", "", 10, 10), evaluate.Note("a", "", "", "punctuation", "", 20, 20),
             evaluate.Note("a", "", "", "caption", "", 30, 30), evaluate.Note("a", "", "", "too_short", "", 40, 40)]
    flags = [caption_flag("a", 1, "missing_word", 9, 11),                     # a spelling-family type
             caption_flag("a", 2, "spelling", 19, 21),                        # right moment, other family
             caption_flag("a", 3, "capitalization", 29, 31),                  # backed by the generic note
             caption_flag("a", 4, "phrase_break", 60, 61)]                    # nothing there
    evaluate.match(notes, flags, tolerance=0)
    rows = {r["type"]: r for r in evaluate.score(notes, flags, minutes=1.0)}
    assert rows["spelling"]["recall"] == 1.0 and rows["spelling"]["precision"] == 0.5
    assert rows["punctuation"]["recall"] == 0.0 and rows["punctuation"]["precision"] == 1.0
    assert rows["too_short"]["recall"] == 0.0 and rows["too_short"]["precision"] == 0.0
    everything = rows[evaluate.ALL_CAPTIONS]
    assert (everything["notes"], everything["found"], everything["flags"], everything["true"]) == (4, 3, 4, 3)
    assert rows[evaluate.ALL]["notes"] == 0 and rows[evaluate.ALL]["flags"] == 0       # color untouched


def test_template_has_caption_and_color_notes_and_a_video_list(tmp_path):
    xlsx = ROOT / "data" / "QC_Review_Analysis_Jun-Oct_2026.xlsx"
    if not xlsx.is_file():
        pytest.skip("the QC spreadsheet is client data and isn't in the repository")
    out = tmp_path / "ground_truth.csv"
    evaluate.make_template(xlsx, out)
    notes, problems = evaluate.read_truth(out, fps=30)
    kinds = {n.kind for n in notes}
    assert {"spelling", "punctuation", "too_short"} <= kinds and "color_grading" in kinds and not problems
    audio = [n for n in notes if n.area == "audio"]
    assert {"stutter", "repeated_phrase", "abrupt_cut", "dead_air"} <= {n.kind for n in audio}
    assert {n.client for n in audio} <= {"E2CL", "E2CS"}                       # the podcast clients
    videos = (tmp_path / "ground_truth_videos.csv").read_text(encoding="utf-8-sig").splitlines()
    assert videos[0].startswith("task_name,client,caption_notes,color_notes,audio_notes,timed_notes")
    assert len(videos) > 10
    with pytest.raises(SystemExit, match="already exists"):
        evaluate.make_template(xlsx, out)                                      # never overwrites your edits


def test_videos_are_found_by_name_in_subfolders(tmp_path):
    (tmp_path / "E3AC" / "wedding").mkdir(parents=True)
    video = tmp_path / "E3AC" / "wedding" / "Elvia & Santizo – ceremonia.mp4"
    video.write_bytes(b"x")
    assert evaluate.find_video("Elvia & Santizo – ceremonia.mp4", [tmp_path]) == video
    assert evaluate.find_video("missing.mp4", [tmp_path]) is None


# --- color space and HDR (real ffmpeg-made files) --------------------------------------------

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is not installed")


def make_clip(path: Path, *args: str) -> Path:
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc2=s=1280x720:d=0.2", *args, str(path)], check=True)
    return path


@needs_ffmpeg
def test_untagged_hd_is_read_as_bt709(tmp_path):
    import av
    import numpy as np

    from src.inputs.video_ocr import frame_to_rgb

    tagged = make_clip(tmp_path / "tagged.mp4", "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
                       "-colorspace", "bt709", "-color_range", "tv", "-c:v", "libx264")
    untagged = make_clip(tmp_path / "untagged.mp4", "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
                         "-c:v", "libx264")
    frames = {}
    for name, path in (("tagged", tagged), ("untagged", untagged)):
        with av.open(str(path)) as c:
            frames[name] = frame_to_rgb(next(c.decode(c.streams.video[0]))).astype(int)
    assert float(np.abs(frames["tagged"] - frames["untagged"]).mean()) < 0.5


@needs_ffmpeg
def test_hdr_is_detected(tmp_path):
    from src.video.color import hdr_transfer

    hlg = make_clip(tmp_path / "hlg.mp4", "-pix_fmt", "yuv420p10le", "-c:v", "libx265", "-x265-params",
                    "log-level=none:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc")
    sdr = make_clip(tmp_path / "sdr.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p")
    assert hdr_transfer(hlg) == "HLG"
    assert hdr_transfer(sdr) is None


def test_audio_notes_are_matched_within_one_and_a_half_seconds():
    from src.models import Issue

    def flag(kind, at):
        return evaluate.Flag("ep.mp3", Issue(-3001, at, kind, "medium", "x", "y"), at, at)

    notes = [evaluate.Note("ep.mp3", "0:10", "E2CL", "stutter", "Stutter on word got", 10.0, 10.0),
             evaluate.Note("ep.mp3", "0:30", "E2CL", "dead_air", "Silence not removed", 30.0, 30.0),
             evaluate.Note("ep.mp3", "0:50", "E2CL", "audio_edit", "fix the edit", 50.0, 50.0)]
    flags = [flag("stutter", 11.4), flag("dead_air", 32.0), flag("abrupt_cut", 50.5), flag("jump_cut", 70.0)]
    evaluate.match(notes, flags, tolerance=2.0, audio_tolerance=1.5)
    rows = {r["type"]: r for r in evaluate.score(notes, flags, minutes=2.0)}
    assert rows["stutter"]["recall"] == 1.0 and rows["stutter"]["precision"] == 1.0
    assert rows["dead_air"]["recall"] == 0.0                                  # 2 s away: outside +-1.5 s
    assert rows["abrupt_cut"]["precision"] == 1.0                             # matched the generic note
    assert rows["all audio"]["recall"] == 2 / 3 and rows["all audio"]["flags"] == 4
    assert evaluate.audio_kind("Stutter & word repeat \"for\"") == "stutter"
    assert evaluate.audio_kind("Sentence repeat") == "repeated_phrase"
    assert evaluate.audio_kind("Abrupt pause") == "abrupt_cut"
    assert evaluate.audio_kind("Silence not removed") == "dead_air"
    assert evaluate.audio_kind("Change the font") is None
