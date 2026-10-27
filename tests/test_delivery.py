"""The delivery checks (src/delivery/): file format against the client's spec, loudness, clipping,
voice under music, black / frozen / flash frames and the safe area. The rules are tested on made-up
numbers; a short clip made with ffmpeg checks the whole path (skipped when ffmpeg isn't installed)."""

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from src.config import load_config
from src.delivery import check_delivery, delivery_settings
from src.delivery.picture import (FrameScanner, black_runs, flash_frames, frame_cache_file, frozen_runs,
                                  load_frame_stats, picture_issues, save_frame_stats)
from src.delivery.safe_area import applies, overlap_share, safe_area_issues
from src.delivery.sound import ClipScanner, clipping_issues, loudness_issues, parse_loudnorm, voice_under_music
from src.delivery.spec import MediaInfo, parse_aspect, spec_issues
from src.inputs.caption_builder import Reading, build_captions
from src.models import (BLACK_FRAMES, CLIPPING, DELIVERY_SPEC, FLASH_FRAME, FROZEN_PICTURE, LOUDNESS, SAFE_AREA,
                        TRUE_PEAK, VOICE_UNDER_MUSIC, Caption, SpokenWord, audio_issue_id, audio_number,
                        delivery_issue_id, delivery_number)

CONFIG = load_config()
DEFAULTS = CONFIG["delivery"]
FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="needs ffmpeg")


def test_every_threshold_is_in_config_and_a_client_can_change_it():
    for block in ("spec", "loudness", "clipping", "voice_under_music", "picture", "safe_area"):
        assert block in DEFAULTS
    assert DEFAULTS["voice_under_music"]["enabled"] is False                 # experimental: a client opts in
    client = {"client": "X", "delivery": {"spec": {"aspect": "9:16"}, "loudness": {"target_lufs": -16}}}
    merged = delivery_settings(CONFIG, client)
    assert merged["spec"]["aspect"] == "9:16" and merged["loudness"]["target_lufs"] == -16
    assert merged["loudness"]["tolerance_lu"] == DEFAULTS["loudness"]["tolerance_lu"]       # the rest stays


def test_delivery_ids_are_their_own_range():
    assert delivery_number(delivery_issue_id(3)) == 3 and audio_number(delivery_issue_id(3)) is None
    assert audio_number(audio_issue_id(7)) == 7 and delivery_number(audio_issue_id(7)) is None


# --- the file against the spec --------------------------------------------------------------------

REEL = MediaInfo(duration=75.0, width=1080, height=1920, fps=29.97, video_kbps=3000, sample_rate=44100, channels=2)


def test_nothing_ordered_means_nothing_flagged():
    assert spec_issues(REEL, DEFAULTS["spec"]) == []


def test_each_difference_from_the_order_is_its_own_issue():
    spec = {**DEFAULTS["spec"], "width": 1080, "height": 1920, "aspect": "16:9", "fps": [24, 25],
            "max_duration_sec": 60, "min_video_kbps": 8000, "audio_sample_rate": 48000, "audio_channels": 1}
    issues = spec_issues(REEL, spec)
    text = " ".join(i.message for i in issues)
    assert {i.type for i in issues} == {DELIVERY_SPEC} and len(issues) == 6        # the resolution is right
    assert "9:16" in text and "29.970 fps" in text and "75.0 s" in text and "3000 kbit/s" in text
    assert "44100 Hz" in text and "stereo" in text and all(i.timestamp == 0.0 for i in issues)


def test_frame_rate_and_aspect_tolerances():
    assert spec_issues(REEL, {"fps": 30, "fps_tolerance": 0.01, "aspect": "9:16"}) == []    # 29.97 is within 1%
    assert len(spec_issues(REEL, {"fps": 30, "fps_tolerance": 0.0001})) == 1
    anamorphic = MediaInfo(width=1440, height=1080, pixel_aspect=4 / 3, fps=25)              # shows as 16:9
    assert spec_issues(anamorphic, {"aspect": "16:9"}) == []
    assert parse_aspect("9:16") == pytest.approx(0.5625) and parse_aspect("16/9") == pytest.approx(16 / 9)
    assert parse_aspect(None) is None and parse_aspect(1.0) == 1.0


def test_a_missing_audio_track_is_flagged_only_when_audio_was_ordered():
    silent = MediaInfo(duration=10, width=1920, height=1080, fps=25)
    assert spec_issues(silent, {}) == []
    assert "no audio track" in spec_issues(silent, {"audio_sample_rate": 48000})[0].message


# --- loudness and clipping --------------------------------------------------------------------------

LOUDNORM_OUTPUT = """[Parsed_loudnorm_0 @ 000001]
{
	"input_i" : "-19.24",
	"input_tp" : "-0.40",
	"input_lra" : "6.10",
	"input_thresh" : "-29.50",
	"output_i" : "-24.00",
	"target_offset" : "0.00"
}
"""


def test_loudnorm_output_is_read():
    assert parse_loudnorm(LOUDNORM_OUTPUT) == {"integrated": -19.24, "true_peak": -0.4, "lra": 6.1}
    assert parse_loudnorm("no json here") is None


def test_loudness_off_target_and_true_peak_are_flagged():
    settings = DEFAULTS["loudness"]                                     # -14 LUFS +-2, true peak -1 dBTP
    issues = loudness_issues({"integrated": -19.24, "true_peak": -0.4, "lra": 6.1}, settings)
    assert [i.type for i in issues] == [LOUDNESS, TRUE_PEAK]
    assert "too quiet" in issues[0].message and "Raise the mix by about 5 dB" in issues[0].suggestion
    assert loudness_issues({"integrated": -15.0, "true_peak": -2.0, "lra": 5}, settings) == []
    assert "too loud" in loudness_issues({"integrated": -9.0, "true_peak": -3.0, "lra": 5}, settings)[0].message
    assert loudness_issues({"integrated": float("-inf"), "true_peak": float("-inf"), "lra": 0}, settings) == []
    assert loudness_issues({"integrated": -30, "true_peak": 0.5, "lra": 1},
                           {"target_lufs": None, "max_true_peak_dbtp": None}) == []


def test_clips_are_runs_of_full_scale_samples_even_across_blocks():
    scanner = ClipScanner(sample_rate=10, channels=2, level=0.999, min_run=3)
    left = [0, 1, 0, 0, 0, 0, 0, 0, 1, 1]           # a lone peak at 0.1 s; a run starting at 0.8 s ...
    right = [0, 0, 0, 0, -1, -1, -1, 0, 0, 0]       # a negative run of 3 at 0.4 s
    scanner.feed(np.array(list(zip(left, right)), dtype=np.float32))
    scanner.feed(np.array([[1, 0], [0, 0]], dtype=np.float32))             # ... that ends here: 3 samples
    scanner.feed(np.array([[1, 0], [1, 0]], dtype=np.float32))             # still open at the end of the file
    assert scanner.finish() == pytest.approx([0.4, 0.8])                    # the last run is only 2 samples


def test_clips_close_together_are_one_flag_and_only_the_worst_are_kept():
    times = [1.0, 1.2, 1.5, 10.0, 30.0, 30.1]
    issues = clipping_issues(times, {"merge_gap_sec": 1.0, "max_flags": 2})
    assert [(i.type, i.timestamp) for i in issues] == [(CLIPPING, 1.0), (CLIPPING, 30.0)]
    assert "3 times" in issues[0].message and issues[0].severity == "high" and issues[1].severity == "medium"
    assert clipping_issues([], {}) == []


# --- voice under music --------------------------------------------------------------------------------

def speech_over_bed(voice_db: float, bed_db: float, seconds: float = 10.0, sr: int = 8000):
    """Noise "music" at bed_db all through, plus "speech" at voice_db in 0.6 s words with 0.4 s pauses."""
    rng = np.random.default_rng(0)
    n = int(seconds * sr)
    x = rng.standard_normal(n) * 10 ** (bed_db / 20)
    words = []
    for k in range(int(seconds)):
        a, b = k + 0.1, k + 0.7
        x[int(a * sr):int(b * sr)] += rng.standard_normal(int(b * sr) - int(a * sr)) * 10 ** (voice_db / 20)
        words.append(SpokenWord("word", a, b, 0.9))
    return x.astype(np.float32), words, sr


def test_a_voice_barely_above_the_music_is_flagged():
    settings = DEFAULTS["voice_under_music"]
    samples, words, sr = speech_over_bed(voice_db=-20, bed_db=-21)          # only ~2.5 dB above the bed
    issues = voice_under_music(samples, sr, words, settings)
    assert [i.type for i in issues] == [VOICE_UNDER_MUSIC]                   # the windows merge into one flag
    assert issues[0].timestamp == 0.0 and "dB louder than the sound in its pauses" in issues[0].message


def test_a_clear_voice_or_a_quiet_room_is_not():
    settings = DEFAULTS["voice_under_music"]
    samples, words, sr = speech_over_bed(voice_db=-15, bed_db=-35)
    assert voice_under_music(samples, sr, words, settings) == []
    samples, words, sr = speech_over_bed(voice_db=-55, bed_db=-56)          # nothing to bury it: below -45 dBFS
    assert voice_under_music(samples, sr, words, settings) == []
    assert voice_under_music(np.zeros(100, np.float32), 8000, [], settings) == []


# --- black, frozen and flash frames ----------------------------------------------------------------------

def frames_from(levels, noise=0.0, size=(8, 8)):
    """A FrameStats from a list of grey levels (one frame each, noise added to all but black), 10 a second."""
    rng = np.random.default_rng(1)
    scanner = FrameScanner({"analysis_width": 8, "black_pixel_luma": 24})
    for i, level in enumerate(levels):
        frame = np.full(size, float(level)) + (rng.standard_normal(size) * noise if noise and level else 0)
        scanner.feed(i / 10, frame)
    return scanner.result()


PICTURE = {**DEFAULTS["picture"], "min_black_sec": 0.1, "edge_black_sec": 1.0, "min_freeze_sec": 1.0}


def test_black_in_the_middle_is_flagged_but_a_fade_at_either_end_is_not():
    stats = frames_from([0] * 5 + [120] * 10 + [0] * 3 + [120] * 10 + [0] * 8, noise=20)
    assert black_runs(stats, PICTURE) == [pytest.approx((1.5, 1.8))]
    long_start = frames_from([0] * 15 + [120] * 10, noise=20)                # 1.5 s of black at the start
    assert black_runs(long_start, PICTURE) == [pytest.approx((0.0, 1.5))]


def test_a_picture_that_stops_changing_is_frozen():
    moving = [100 + 30 * (i % 2) for i in range(10)]
    stats = frames_from(moving + [140] * 15 + moving)
    [(start, end)] = frozen_runs(stats, PICTURE)
    assert start == pytest.approx(1.0) and end == pytest.approx(2.5)
    assert frozen_runs(frames_from([0] * 30), PICTURE) == []                 # black isn't "frozen"
    assert frozen_runs(stats, {**PICTURE, "min_freeze_sec": 2.0}) == []


def test_flash_frames_differ_from_both_neighbours_that_match_each_other():
    one = frames_from([100] * 5 + [250] + [100] * 5)
    assert flash_frames(one, PICTURE) == [pytest.approx((0.5, 1))]
    two = frames_from([100] * 5 + [250, 240] + [100] * 5)
    assert flash_frames(two, PICTURE) == [pytest.approx((0.5, 2))]
    cut = frames_from([100] * 5 + [250] * 6)                                 # a cut: the new shot stays
    assert flash_frames(cut, PICTURE) == []
    three = frames_from([100] * 5 + [250] * 3 + [100] * 5)
    assert flash_frames(three, PICTURE) == []                                # longer than max_flash_frames


def test_picture_issues_and_the_frame_cache(tmp_path):
    stats = frames_from([120] * 5 + [0] * 3 + [120] * 5 + [250] + [120] * 5, noise=3)     # moving, not frozen
    issues = picture_issues(stats, PICTURE)
    assert [(i.type, round(i.timestamp, 1)) for i in issues] == [(BLACK_FRAMES, 0.5), (FLASH_FRAME, 1.3)]
    video = tmp_path / "v.mp4"
    video.write_bytes(b"not really a video")
    cache = frame_cache_file(video, PICTURE, tmp_path)
    save_frame_stats(cache, video, stats)
    again = load_frame_stats(cache)
    assert again.times == stats.times and again.black == stats.black and again.diffs == stats.diffs
    assert frame_cache_file(video, {**PICTURE, "analysis_width": 32}, tmp_path) != cache
    assert frame_cache_file(video, {**PICTURE, "min_freeze_sec": 9}, tmp_path) == cache   # judging only


# --- the safe area --------------------------------------------------------------------------------------

def ocr_caption(n, box):
    return Caption(n, n * 2.0, n * 2.0 + 1.5, f"caption {n}", source="ocr", ocr_confidence=0.9, box=box)


def test_captions_under_the_platform_buttons_are_flagged():
    settings = DEFAULTS["safe_area"]
    captions = [ocr_caption(1, (0.1, 0.55, 0.9, 0.65)),          # middle of the frame: fine
                ocr_caption(2, (0.1, 0.84, 0.9, 0.92)),          # in the bottom bar
                ocr_caption(3, (0.5, 0.50, 0.95, 0.56)),         # reaches under the right-hand buttons
                ocr_caption(4, None),                            # read before boxes were kept
                Caption(5, 10, 11, "from an SRT")]               # no position on the picture
    issues, unknown = safe_area_issues(captions, settings)
    assert [(i.caption_id, i.type) for i in issues] == [(2, SAFE_AREA), (3, SAFE_AREA)]
    assert "bottom bar" in issues[0].message and "right buttons" in issues[1].message and unknown == 1


def test_the_safe_area_applies_to_vertical_videos_unless_set():
    assert applies({"enabled": "auto"}, vertical=True) and not applies({"enabled": "auto"}, vertical=False)
    assert applies({"enabled": True}, vertical=False) and not applies({"enabled": False}, vertical=True)
    assert overlap_share((0.0, 0.0, 0.5, 0.5), {"left": 0.25, "top": 0.0, "width": 1, "height": 1}) == 0.5


def test_ocr_captions_keep_where_their_text_was():
    a = Reading("Hello there", 0.9, 0.80, 0.85, left=0.30, right=0.70)
    b = Reading("Hello there", 0.9, 0.81, 0.86, left=0.28, right=0.72)
    [caption] = build_captions([(0.0, [a]), (0.2, [b])], 0.2)
    assert caption.box == (0.28, 0.8, 0.72, 0.86)
    [unknown] = build_captions([(0.0, [Reading("Hi you", 0.9, 0.8, 0.85)])] * 2, 0.2)
    assert unknown.box is None


def test_ocr_cache_keeps_boxes_and_still_reads_old_files(tmp_path):
    from src.inputs.video_ocr import _load, save_captions

    path, video = tmp_path / "c.json", tmp_path / "v.mp4"
    save_captions(path, video, {}, [ocr_caption(1, (0.1, 0.8, 0.9, 0.9)), ocr_caption(2, None)])
    assert [c.box for c in _load(path)] == [(0.1, 0.8, 0.9, 0.9), None]
    path.write_text(json.dumps({"captions": [[0.0, 1.0, "old", 0.9]]}), encoding="utf-8")
    assert _load(path)[0].box is None


# --- the whole path on a made-up clip ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    """3 s of 90x160 (9:16) test pattern at 30 fps, black from 1.0 to 1.5 s, and a tone at twice
    full scale, stored as 16-bit PCM so it is clipped flat."""
    if FFMPEG is None:
        pytest.skip("needs ffmpeg")
    path = tmp_path_factory.mktemp("delivery") / "reel.mkv"
    subprocess.run([FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=s=90x160:r=30:d=3",
                    "-f", "lavfi", "-i", "aevalsrc=2*sin(2*PI*440*t)|2*sin(2*PI*440*t):s=48000:d=3",
                    "-vf", "drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='between(t,1,1.5)'",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", "-shortest", str(path)],
                   check=True)
    return path


@needs_ffmpeg
def test_check_delivery_on_a_real_file(clip, tmp_path):
    settings = delivery_settings(CONFIG, {"delivery": {"spec": {"aspect": "16:9", "max_duration_sec": 2},
                                                       "voice_under_music": {"enabled": True}}})
    issues, stats = check_delivery(clip, settings, ffmpeg=FFMPEG, picture_cache=tmp_path)
    kinds = [i.type for i in issues]
    assert kinds.count(DELIVERY_SPEC) == 2                                   # 9:16 not 16:9; 3 s not 2 s
    assert BLACK_FRAMES in kinds and CLIPPING in kinds and LOUDNESS in kinds and TRUE_PEAK in kinds
    black = next(i for i in issues if i.type == BLACK_FRAMES)
    assert black.timestamp == pytest.approx(1.0, abs=0.05)
    assert all(delivery_number(i.caption_id) for i in issues)                # none is tied to a caption
    assert "90x160" in stats["file"] and stats["frames"] == 90 and stats["clips"] > 0 and not stats["errors"]
    assert stats["voice_under_music"] == "skipped: no speech transcript"
    assert list(tmp_path.glob("*.json"))                                     # the frames are cached


@needs_ffmpeg
def test_the_frames_are_measured_during_the_one_read_of_the_video(clip, tmp_path):
    from src.inputs.video_pass import read_video_once

    picture = DEFAULTS["picture"]
    ran = read_video_once(clip, ocr=CONFIG["ocr"], ocr_cache=tmp_path / "ocr", read_captions=False,
                          read_screen=False, picture=picture, picture_cache=tmp_path / "frames")
    assert ran == ["picture"]
    stats = load_frame_stats(frame_cache_file(clip, picture, tmp_path / "frames"))
    assert stats.frame_count == 90
    assert read_video_once(clip, ocr=CONFIG["ocr"], ocr_cache=tmp_path / "ocr", read_captions=False,
                           read_screen=False, picture=picture, picture_cache=tmp_path / "frames") == []


@needs_ffmpeg
def test_the_engine_runs_the_delivery_checks_and_can_skip_them(clip, tmp_path):
    from src.engine import QCOptions, run_qc

    srt = tmp_path / "c.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello there friends\n", encoding="utf-8")
    config = {**CONFIG, "checks": {**CONFIG["checks"], "transcript": {**CONFIG["checks"]["transcript"], "enabled": False},
                                   "screen_text": {**CONFIG["checks"]["screen_text"], "enabled": False}},
              "paths": {**CONFIG["paths"], "frames_cache_dir": str(tmp_path / "frames")}}
    result = run_qc(clip, None, options=QCOptions(srt_path=srt, check_color=False), config=config)
    assert {BLACK_FRAMES, CLIPPING, LOUDNESS} <= {i.type for i in result}
    assert any(line.startswith("Delivery: 90x160") for line in result.checks)
    off = run_qc(clip, None, config=config,
                 options=QCOptions(srt_path=srt, check_color=False, disabled_checks=frozenset({"delivery"})))
    assert not any(i.type in (BLACK_FRAMES, CLIPPING, LOUDNESS) for i in off)
    assert "Delivery: switched off for this run" in off.checks


def test_the_app_menu_can_switch_delivery_off():
    from src.app.choices import CHECK_TOGGLES, qc_options

    assert "delivery" in {k for k, _ in CHECK_TOGGLES}
    assert "delivery" in qc_options(["delivery"]).disabled_checks


def test_every_client_delivery_block_uses_known_keys():
    from src.config import load_client

    def unknown(block: dict, defaults: dict, where: str) -> list[str]:
        out = []
        for key, value in block.items():
            if key not in defaults:
                out.append(f"{where}.{key}")
            elif isinstance(value, dict) and isinstance(defaults[key], dict):
                out += unknown(value, defaults[key], f"{where}.{key}")
        return out

    for path in sorted((Path(CONFIG["_root"]) / "clients").glob("*.yaml")):
        client = load_client(CONFIG, path.stem)
        block = client.get("delivery") or {}
        assert not unknown(block, DEFAULTS, f"{path.name} delivery"), path.name
        settings = delivery_settings(CONFIG, client)
        if settings["spec"].get("aspect"):
            assert parse_aspect(settings["spec"]["aspect"]) > 0
