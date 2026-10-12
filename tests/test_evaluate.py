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
