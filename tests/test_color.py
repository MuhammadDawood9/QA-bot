"""Color grading checks: measurements on made-up frames, the rules that turn them into issues,
shot detection, and (slow) the planted mistakes in the color test video."""

import json
from pathlib import Path

import numpy as np
import pytest

from src.config import color_settings
from src.models import (COLOR_CAST, CRUSHED_BLACKS, FLAT_CONTRAST, OVEREXPOSED, SATURATION, SHOT_MISMATCH,
                        SKIN_TONE, UNDEREXPOSED, Issue, shot_issue_id, shot_number)
from src.report.builder import ReportInfo, build_report, caption_anchor, caption_title
from src.video.color import FrameStats, Shot, analysis_mask, analyze, cast_name, color_issues, frame_stats
from tests.helpers import CONFIG

SETTINGS = color_settings(CONFIG, {})
SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"


def gradient(h=90, w=160, low=10, high=240, tint=(0, 0, 0)):
    """A grey ramp from `low` to `high`, plus an RGB tint."""
    ramp = np.linspace(low, high, w, dtype=np.float32)
    img = np.repeat(ramp[None, :, None], h, axis=0).repeat(3, axis=2) + np.array(tint, dtype=np.float32)
    return np.clip(img, 0, 255).astype(np.uint8)


def stats(img, **kw):
    return frame_stats(0.0, img, np.ones(img.shape[:2], dtype=bool), **kw)


def shot(n=1, start=0.0, end=4.0, **values):
    base = dict(t=0.0, luma_median=128, black_level=10, white_level=230, clipped_white=0.0, clipped_black=0.0,
                chroma=15, cast_a=0.0, cast_b=1.0, skin_hue=None, skin_chroma=None)
    base.update(values)
    return Shot(n, start, end, [FrameStats(**base)])


def kinds(issues):
    return sorted({i.type for i in issues})


# --- measuring frames ------------------------------------------------------------------------

def test_neutral_well_exposed_frame():
    s = stats(gradient())
    assert s.black_level < 20 and s.white_level > 220 and s.clipped_white == 0
    assert abs(s.cast_a) < 1 and abs(s.cast_b) < 1 and s.chroma < 2


def test_tinted_frame_has_a_cast():
    s = stats(gradient(low=40, high=200, tint=(18, -10, 18)))          # magenta: more red and blue
    assert s.cast_a > 5


def test_strong_cast_is_measured_from_the_brightest_parts():
    s = stats(gradient(low=40, high=220, tint=(60, -40, 60)))          # nothing near neutral is left
    assert s.cast_a is not None and s.cast_a > 15


def test_clipped_and_crushed_shares():
    img = gradient()
    img[:, :40] = 0
    img[:, -40:] = 255
    s = stats(img)
    assert s.clipped_white == pytest.approx(0.25, abs=0.02) and s.clipped_black >= 0.25


def test_caption_region_is_left_out_of_the_measurements():
    img = gradient(low=60, high=180)
    img[60:80, 20:140] = 255                                             # white caption text
    region = {"left": 0.0, "top": 0.6, "width": 1.0, "height": 0.3}
    assert stats(img).clipped_white > 0.1
    assert frame_stats(0.0, img, analysis_mask(90, 160, region)).clipped_white == 0


# --- turning measurements into issues --------------------------------------------------------

def test_a_well_graded_shot_has_no_issues():
    assert color_issues([shot(skin_hue=35, skin_chroma=22)], SETTINGS) == []


@pytest.mark.parametrize("values, expected", [
    (dict(clipped_white=0.12, white_level=255), OVEREXPOSED),
    (dict(luma_median=25, white_level=120), UNDEREXPOSED),
    (dict(clipped_black=0.3), CRUSHED_BLACKS),
    (dict(black_level=70, white_level=200), FLAT_CONTRAST),
    (dict(chroma=3), SATURATION),
    (dict(chroma=50), SATURATION),
    (dict(cast_a=9, cast_b=-6), COLOR_CAST),
    (dict(skin_hue=78, skin_chroma=25), SKIN_TONE),
    (dict(skin_hue=5, skin_chroma=25), SKIN_TONE),
    (dict(skin_hue=40, skin_chroma=45), SKIN_TONE),
])
def test_each_problem_is_flagged(values, expected):
    issues = color_issues([shot(**values)], SETTINGS)
    assert expected in kinds(issues)
    assert all(i.caption_id == shot_issue_id(1) and i.original.startswith("Shot 1, ") for i in issues)
    assert all(i.timestamp == 2.0 for i in issues)                       # middle of the shot


def test_overexposure_severity_and_no_extra_flat_flag():
    [issue] = color_issues([shot(clipped_white=0.5, black_level=50, white_level=255)], SETTINGS)
    assert issue.type == OVEREXPOSED and issue.severity == "high"


def test_black_screen_is_not_graded():
    assert color_issues([shot(luma_median=0, white_level=0, black_level=0, clipped_black=1.0, chroma=0,
                              cast_a=None, cast_b=None)], SETTINGS) == []


def test_skipped_types_and_switching_off():
    s = {**SETTINGS, "skip": ["saturation"]}
    assert color_issues([shot(chroma=3)], s) == []
    assert color_issues([shot(chroma=3)], {**SETTINGS, "enabled": False}) == []


def test_cast_names():
    assert cast_name(10, 0) == "magenta" and cast_name(-10, 0) == "green"
    assert cast_name(0, 10) == "warm/yellow" and cast_name(0, -10) == "cool/blue"
    assert cast_name(10, -9) == "cool/blue and magenta"


# --- shots that don't match ------------------------------------------------------------------

def test_skin_jump_between_cameras():
    shots = [shot(1, 0, 4, skin_hue=35, skin_chroma=22), shot(2, 4, 8, skin_hue=55, skin_chroma=22)]
    [issue] = color_issues(shots, SETTINGS)
    assert issue.type == SHOT_MISMATCH and "shot 1" in issue.message and "35° -> 55°" in issue.message


def test_a_wrong_shot_is_not_the_reference_for_the_next():
    shots = [shot(1, 0, 4, skin_hue=35, skin_chroma=22), shot(2, 4, 8, skin_hue=80, skin_chroma=22),
             shot(3, 8, 12, skin_hue=36, skin_chroma=22)]
    issues = color_issues(shots, SETTINGS)
    assert [(shot_number(i.caption_id), i.type) for i in issues] == [(2, SKIN_TONE)]


def test_shot_far_from_the_rest():
    shots = [shot(n, 4 * n, 4 * n + 4, cast_a=2, cast_b=4) for n in range(1, 5)]
    shots.append(shot(5, 20, 24, cast_a=-3, cast_b=-3))                 # below the cast threshold alone
    issues = color_issues(shots, SETTINGS)
    assert [(shot_number(i.caption_id), i.type) for i in issues] == [(5, SHOT_MISMATCH)]
    assert "rest" in issues[0].message


def test_short_shots_are_not_compared():
    shots = [shot(n, 4 * n, 4 * n + 4, cast_a=2, cast_b=4) for n in range(1, 5)]
    shots.append(shot(5, 20, 20.5, cast_a=-3, cast_b=-3))
    assert color_issues(shots, SETTINGS) == []


# --- finding shots ---------------------------------------------------------------------------

def test_cuts_split_shots():
    a, b = gradient(), gradient(low=20, high=120, tint=(40, 0, -30))
    frames = [(n * 0.25, 3.0, img) for n, img in enumerate([a] * 4 + [b] * 4 + [a] * 4)]
    shots = analyze(iter(frames), {**SETTINGS, "sample_fps": 4, "measure_every_n_frames": 1})
    assert [(s.number, s.start, s.end) for s in shots] == [(1, 0.0, 1.0), (2, 1.0, 2.0), (3, 2.0, 3.0)]
    assert all(len(s.frames) == 4 for s in shots)


class CountingDetector:
    """Stands in for YuNet: counts the frames it's asked about, finds no face."""

    def __init__(self):
        self.times = 0

    def setInputSize(self, size):
        pass

    def detect(self, image):
        self.times += 1
        return None, None


def test_every_shot_is_measured_and_searched_for_faces_from_its_first_frame():
    a, b = gradient(), gradient(low=20, high=120, tint=(40, 0, -30))
    # shot 1: 7 frames, shot 2: a single frame, shot 3: 3 frames
    frames = [(n * 0.25, 3.0, img) for n, img in enumerate([a] * 7 + [b] + [a] * 3)]
    detector = CountingDetector()
    shots = analyze(iter(frames), {**SETTINGS, "sample_fps": 4, "min_shot_sec": 0.2, "measure_every_n_frames": 2,
                                   "face_every_n_frames": 4}, face_detector=detector)
    assert [len(s.frames) for s in shots] == [4, 1, 2]                 # frames 0, 2, 4, 6 | 0 | 0, 2
    assert [f.t for f in shots[1].frames] == [1.75]                     # the one-frame shot is still measured
    assert detector.times == 2 + 1 + 1                                   # frames 0 and 4 | 0 | 0


def test_sampling_settings_are_in_the_config():
    assert SETTINGS["sample_fps"] == 4 and SETTINGS["measure_every_n_frames"] == 2
    assert SETTINGS["face_every_n_frames"] == 4


# --- report ----------------------------------------------------------------------------------

def test_report_titles_for_shots():
    assert shot_number(shot_issue_id(7)) == 7 and shot_number(-3) is None and shot_number(5) is None
    assert caption_title(shot_issue_id(7)) == "shot 7" and caption_anchor(shot_issue_id(7)) == "shot-7"
    assert caption_title(-2) == "no caption" and caption_title(4) == "caption #4"


def test_report_with_a_color_issue(tmp_path):
    issue = Issue(caption_id=shot_issue_id(2), timestamp=6.0, type=OVEREXPOSED, severity="high",
                  original="Shot 2, 00:04.000 - 00:08.000", message="Overexposed: 58% clipped.",
                  suggestion="Bring the highlights down.", confidence=0.8)
    settings = {**CONFIG["report"], "output_dir": str(tmp_path), "formats": ["md", "docx"]}
    written = build_report([], [issue], ReportInfo(srt=Path("c.srt")), settings)
    md = written[0].read_text(encoding="utf-8")
    assert "### 00:06.000, shot 2" in md and "Overexposed (color)" in md and '<a id="shot-2"></a>' in md


# --- the color test video (slow) -------------------------------------------------------------

@pytest.mark.slow
def test_planted_grading_mistakes_are_found():
    video, expected_file = SAMPLES / "color_test.mp4", SAMPLES / "color_test_expected.json"
    if not video.exists():
        pytest.skip("run: python scripts/make_color_samples.py --source <a well-graded video with a face>")
    from src.video.color import read_color

    shots = read_color(video, SETTINGS, None, None, Path(CONFIG["_root"]) / "models")
    expected = json.loads(expected_file.read_text(encoding="utf-8"))
    assert len(shots) == len(expected)
    found: dict[int, set[str]] = {}
    for i in color_issues(shots, SETTINGS):
        found.setdefault(shot_number(i.caption_id), set()).add(i.type)
    for e in expected:
        if e["expected"] is None:
            assert e["shot"] not in found, (e, found.get(e["shot"]))
        else:
            assert e["expected"] in found.get(e["shot"], set()), (e, found.get(e["shot"]))
