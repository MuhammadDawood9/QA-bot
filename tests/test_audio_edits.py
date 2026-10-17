"""Phase 4: audio-edit checks for podcasts (client E2CL).

Fast tests use speech-like audio made with numpy (tests/audio_synth.py) with planted mistakes,
and stand-in transcripts. The slow test (--run-slow) makes real text-to-speech samples with
scripts/make_audio_samples.py (ffmpeg splicing) and runs the real Whisper. Neither is real
podcast audio: see the plan file for what that leaves untested.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import wave
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from src.audio import edits  # noqa: E402
from src.audio.edits import check_audio, effective_settings, transcript_repeats  # noqa: E402
from src.audio.media import is_audio_only  # noqa: E402
from src.config import load_client, load_config  # noqa: E402
from src.engine import QCOptions, audio_settings, run_qc, write_report  # noqa: E402
from src.models import SpokenWord  # noqa: E402
from tests import audio_synth as S  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONFIG = load_config()
E2CL = load_client(CONFIG, "E2CL")
SETTINGS = audio_settings(CONFIG, E2CL, audio_only=True)


def words(*spec):
    """("I", 1.0, 1.1), ... -> SpokenWords (probability 0.95 unless given)."""
    return [SpokenWord(t, s, e, p[0] if p else 0.95) for t, s, e, *p in spec]


def planted(seed: int) -> S.Planter:
    """Every kind of mistake once, planted from the end of the file backwards."""
    p = S.Planter(S.make_speech(seed, n_sentences=12))
    sp = p.speech
    a, b = S.sentence_pause(sp, 8), S.sentence_pause(sp, 10)
    p.plant("jump_cut", S.change_room_tone, a, b)
    p.truth.append(("jump_cut", b))                       # the room changes back: also an edit
    p.plant("dead_air", S.insert_silence, S.sentence_pause(p.speech, 6), 4.0)
    p.plant("abrupt_cut", S.cut_mid_word, p.speech.sentences[4][0] + 2)
    p.plant("repeated_phrase", S.repeat_phrase, p.speech.sentences[3][0] + 1, 2)
    p.plant("repeated_phrase", S.retake, 0, 1)
    p.plant("stutter", S.repeat_word, 3)
    return p


# --- settings ----------------------------------------------------------------------------------

def test_e2cl_holds_every_audio_threshold_and_three_sensitivities():
    block = E2CL["audio"]
    assert block["enabled"] is True and set(block["levels"]) == {"low", "medium", "high"}
    assert set(block) >= set(CONFIG["audio"]) - {"enabled"}          # every default is spelled out
    low, high = (effective_settings({**block, "sensitivity": s}) for s in ("low", "high"))
    assert low["acoustic_similarity"] > high["acoustic_similarity"] and low["cut_score"] > high["cut_score"]
    assert low["dead_air_sec"] > high["dead_air_sec"]


def test_audio_checks_switch_on_for_audio_files_only_unless_the_client_says():
    assert audio_settings(CONFIG, {}, audio_only=True)["enabled"] is True
    assert audio_settings(CONFIG, {}, audio_only=False)["enabled"] is False
    assert audio_settings(CONFIG, E2CL, audio_only=False)["enabled"] is True
    assert audio_settings(CONFIG, {"audio": {"enabled": False}}, audio_only=True)["enabled"] is False


def test_audio_files_are_recognized(tmp_path):
    for name in ("ep.mp3", "ep.WAV", "ep.m4a", "ep.aac", "ep.flac", "ep.ogg"):
        assert is_audio_only(tmp_path / name)
    from src.app.choices import file_dialog_filter, is_video

    assert is_video(Path("ep.mp3")) and is_video(Path("clip.mov")) and "*.flac" in file_dialog_filter()


# --- transcript-based repeats ---------------------------------------------------------------------

def test_repeated_words_phrases_false_starts_and_legit_repeats():
    s = effective_settings(SETTINGS)
    found = transcript_repeats(words(
        ("I", 0.0, 0.1), ("think", 0.15, 0.4), ("the", 0.45, 0.55), ("the", 0.6, 0.7), ("market", 0.75, 1.1),
        ("is", 1.2, 1.3), ("very", 1.35, 1.5), ("very", 1.55, 1.7), ("strong.", 1.75, 2.0),
        ("We", 3.0, 3.1), ("think", 3.15, 3.4), ("about", 3.45, 3.7), ("think", 3.75, 4.0), ("about", 4.05, 4.3),
        ("I", 5.0, 5.1), ("wa-", 5.15, 5.3), ("I", 5.35, 5.45), ("wanted", 5.5, 5.9),
        ("rate", 7.0, 7.2, 0.6), ("rates", 7.25, 7.6),
        ("we", 9.0, 9.1), ("week", 9.15, 9.4),                         # two words, said clearly: fine
        ("um", 10.0, 10.2), ("um", 10.3, 10.5),                        # fillers are a separate check
    ), s)
    got = [(f.kind, round(f.start, 2)) for f in found]
    assert got == [("stutter", 0.45), ("repeated_phrase", 3.15), ("stutter", 5.15), ("stutter", 7.0)]
    assert "False start" in found[2].message and "rate rates" in found[3].message


def test_long_retake_in_the_transcript():
    sp = S.make_speech(31, n_sentences=6)
    p = S.Planter(sp)
    at = p.plant("repeated_phrase", S.retake, 1, 2)
    sig = edits.analyze(p.speech.samples, p.speech.sr, effective_settings(SETTINGS))
    takes = edits.repeated_takes(sig, p.speech.words, effective_settings(SETTINGS))
    assert len(takes) == 1 and abs(takes[0].start - at) < 0.1
    assert "Repeated take" in takes[0].message and takes[0].confidence >= 0.85       # it sounds the same too


def test_words_written_into_silence_are_ignored():
    """Whisper sometimes writes "Thank you. Thank you." into a long pause: that isn't speech."""
    sp = S.make_speech(32, n_sentences=4)
    at = sp.words[-1].end + 0.2
    sp, _ = S.insert_silence(sp, at, 4.0)
    ghost = words(("Thank", at + 1.0, at + 1.3), ("you.", at + 1.3, at + 1.6), ("Thank", at + 2.0, at + 2.3),
                  ("you.", at + 2.3, at + 2.6))
    issues, _ = check_audio(sp.samples, sp.sr, sorted(sp.words + ghost, key=lambda w: w.start), SETTINGS)
    assert not [i for i in issues if i.type in ("stutter", "repeated_phrase")]
    assert [i.type for i in issues] == ["dead_air"]


# --- the sound -------------------------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(1, 9))
def test_clean_speech_gives_no_flags(seed):
    sp = S.make_speech(seed, n_sentences=10)
    issues, stats = check_audio(sp.samples, sp.sr, sp.words, SETTINGS)
    assert issues == [], [(i.timestamp, i.type, i.message) for i in issues]
    assert stats["words"] == len(sp.words) and stats["fillers"] == {}


def test_planted_mistakes_are_found_with_their_type():
    """Six files, each with a stutter Whisper wrote down once, a repeated phrase, a retake, a word
    cut off mid-word, a room tone change (and back) and 4 s of dead air. A room tone change on its
    own is not a cut any more (two mics or rooms in one podcast change it at every turn)."""
    found, total, false_alarms = {}, {}, 0
    for seed in range(21, 27):
        p = planted(seed)
        issues, _ = check_audio(p.speech.samples, p.speech.sr, p.speech.words, SETTINGS)
        for kind, t in p.truth:
            total[kind] = total.get(kind, 0) + 1
            found[kind] = found.get(kind, 0) + any(abs(i.timestamp - t) <= 1.5 and i.type == kind for i in issues)
        false_alarms += sum(1 for i in issues if all(abs(i.timestamp - t) > 1.5 for _, t in p.truth))
    assert false_alarms == 0
    for kind in ("stutter", "repeated_phrase", "dead_air"):
        assert found[kind] == total[kind], (kind, found, total)
    assert found["jump_cut"] == 0                                  # room tone alone: not flagged
    assert found["abrupt_cut"] >= total["abrupt_cut"] - 1, found               # 5 of 6 at least


def test_sensitivity_changes_what_is_flagged():
    sp = S.make_speech(40, n_sentences=6)
    sp, _ = S.insert_silence(sp, S.sentence_pause(sp, 2), 2.0)                # a 2.5-2.7 s pause
    def dead(level):
        return [i for i in check_audio(sp.samples, sp.sr, sp.words, {**SETTINGS, "sensitivity": level})[0]
                if i.type == "dead_air"]
    assert dead("high") and dead("medium") and not dead("low")                 # limits 1.8 / 2.5 / 3.5 s


def test_checks_can_be_switched_off_and_fillers_on():
    sp = S.make_speech(41, n_sentences=5)
    sp, _ = S.insert_silence(sp, S.sentence_pause(sp, 2), 4.0)
    filler = words(("um,", sp.words[3].start, sp.words[3].end))[0]
    w = sp.words[:3] + [filler] + sp.words[4:]
    on = {**SETTINGS, "checks": {**SETTINGS["checks"], "dead_air": False, "fillers": True}}
    issues, stats = check_audio(sp.samples, sp.sr, w, on)
    assert [i.type for i in issues] == ["filler"] and stats["fillers"] == {"um": 1}
    issues, stats = check_audio(sp.samples, sp.sr, w, SETTINGS)
    assert [i.type for i in issues] == ["dead_air"] and stats["fillers"] == {"um": 1}      # counted, not flagged


def test_silences_from_the_level_not_single_samples():
    """Room tone at -50 dBFS has sample peaks over -40: still silence, judged on 20 ms levels."""
    rng = np.random.default_rng(3)
    sr = 16000
    x = np.concatenate([S._syllable(rng, sr, 0.3, -20), S.room_tone(rng, 3 * sr, -50.0),
                        S._syllable(rng, sr, 0.3, -20)]).astype(np.float32)
    assert float(np.max(np.abs(x[int(0.4 * sr):int(3 * sr)]))) > 10 ** (-40 / 20)
    sig = edits.analyze(x, sr, effective_settings(SETTINGS))
    [(start, end)] = edits.silences_in(x, sr, -60.0, 2.0, sig.floor, 8.0)
    assert 0.25 < start < 0.4 and 3.2 < end < 3.4


def test_a_skipped_stretch_is_filled_from_the_plain_transcript():
    sp = S.make_speech(42, n_sentences=8)
    i, j = sp.sentences[2][0], sp.sentences[5][1]
    skipped = sp.words[:i] + sp.words[j:]                                      # Whisper dropped ~10 s
    asked = []

    def plain():
        asked.append(True)
        return sp.words

    _, stats = check_audio(sp.samples, sp.sr, skipped, SETTINGS, backup_words=plain)
    assert asked and stats["transcript_gaps"] == 1 and stats["words"] == len(sp.words)
    _, stats = check_audio(sp.samples, sp.sr, sp.words, SETTINGS, backup_words=plain)
    assert stats["transcript_gaps"] == 0 and len(asked) == 1                    # not asked when nothing's missing


# --- through the engine and the report ----------------------------------------------------------------

def write_wav(path: Path, x: np.ndarray, sr: int) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    return path


@pytest.fixture
def podcast(tmp_path, monkeypatch):
    """An E2CL-style file with planted mistakes, and Whisper replaced by its known words."""
    p = planted(25)
    path = write_wav(tmp_path / "E2CL episode 7.wav", p.speech.samples, p.speech.sr)
    asked = []

    def fake_transcript(video, machine, settings, terms, cache_dir, models_dir=None, transcriber=None, progress=None,
                        need_segments=False):
        asked.append(dict(settings))
        return p.speech.words

    monkeypatch.setattr("src.engine.get_transcript", fake_transcript)
    return path, p, asked


def test_audio_file_runs_only_the_audio_checks(podcast):
    path, p, asked = podcast
    result = run_qc(path, "E2CL", config=CONFIG)
    assert result.audio_only and result.captions == []
    types = {i.type for i in result}
    assert {"stutter", "repeated_phrase", "dead_air"} <= types
    assert all(i.caption_id <= -3000 for i in result)
    assert asked and asked[0]["initial_prompt_prefix"].startswith("Umm") and asked[0]["condition_on_previous_text"] is False
    assert asked[0]["vad_filter"] is False
    text = "\n".join(result.checks)
    assert "Audio edits (medium sensitivity)" in text and "not run (audio file)" in text
    assert "Caption checks: not run" in text and "Color" not in text.replace("color)", "")


def test_speech_skip_never_touches_the_audio_edit_checks(podcast, tmp_path, monkeypatch):
    """The gate only sets a flag. The stutter / repeated-phrase / cut checks keep their own
    transcript, so their issues are identical whether the caption comparison runs or skips."""
    path, p, _ = podcast

    def fake(video, machine, settings, terms, cache_dir, models_dir=None, transcriber=None, progress=None,
             need_segments=False):
        # The audio-edit pass (disfluent transcript) is full; the caption comparison's is a word.
        return p.speech.words if settings.get("initial_prompt_prefix") else [SpokenWord("Yeah", 0.1, 0.3, 0.9)]

    monkeypatch.setattr("src.engine.get_transcript", fake)
    srt = tmp_path / "episode.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello there\n", encoding="utf-8")
    without = run_qc(path, "E2CL", config=CONFIG)
    skipped = run_qc(path, "E2CL", options=QCOptions(srt_path=srt), config=CONFIG)
    audio = lambda r: [(i.type, round(i.timestamp, 2)) for i in r if i.caption_id <= -3000]
    assert audio(without) and audio(skipped) == audio(without)
    assert (skipped.context.stats.get("speech") or {}).get("reason")


def test_audio_checks_can_be_switched_off_from_the_menu(podcast):
    path, _, _ = podcast
    result = run_qc(path, "E2CL", options=QCOptions(disabled_checks=frozenset({"audio"})), config=CONFIG)
    assert list(result) == [] and any("Audio edits: switched off" in line for line in result.checks)


def test_report_has_a_clip_and_a_waveform_per_audio_issue(podcast, tmp_path):
    from docx import Document

    path, _, _ = podcast
    config = {**CONFIG, "report": {**CONFIG["report"], "output_dir": str(tmp_path / "reports")}}
    result = run_qc(path, "E2CL", config=config)
    written = write_report(result, formats=["md", "docx"])
    folder = written[0].parent
    [clips] = [d for d in folder.iterdir() if d.is_dir() and d.name.endswith("_clips")]
    mp3s, pngs = sorted(clips.glob("*.mp3")), sorted(clips.glob("*.png"))
    assert len(mp3s) == len(pngs) == len(result)
    assert all(m.stat().st_size > 5000 for m in mp3s)                         # ~6 s of 64 kbit/s audio
    md = (folder / "report.md").read_text(encoding="utf-8")
    assert "- **Audio:** E2CL episode 7.wav" in md and "[Play the clip (6 s)](" in md and "Appendix" not in md
    import re

    assert re.search(r"### \d\d:\d\d\.\d{3}, audio", md)                       # mm:ss.ms timestamps
    docx = next(p for p in written if p.suffix == ".docx")
    doc = Document(str(docx))
    links = [r.target_ref for r in doc.part.rels.values() if r.reltype.endswith("/hyperlink")]
    assert len(links) == len(result) and all(link.startswith(clips.name.replace(" ", "%20") + "/") for link in links)
    headers = [c.text for c in doc.tables[1].rows[0].cells]
    assert headers[1] == "Waveform and clip" and headers[2] == "What was said"
    assert len(doc.inline_shapes) == len(result)                              # one waveform each


def test_shared_copy_keeps_the_clip_links_working(podcast, tmp_path):
    from src.shared import copy_report_to_shared

    path, _, _ = podcast
    config = {**CONFIG, "report": {**CONFIG["report"], "output_dir": str(tmp_path / "reports")}}
    written = write_report(run_qc(path, "E2CL", config=config), formats=["docx"])
    docx = written[0]
    clips = next(d for d in docx.parent.iterdir() if d.name.endswith("_clips"))
    copied = copy_report_to_shared(docx, tmp_path / "shared", "E2CL", "sara", path, clips)
    assert (copied.parent / clips.name).is_dir() and len(list((copied.parent / clips.name).glob("*.mp3"))) > 3


# --- real speech (text-to-speech), real Whisper, ffmpeg splicing ---------------------------------------------

@pytest.mark.slow
def test_text_to_speech_samples_with_real_whisper(tmp_path):
    """scripts/make_audio_samples.py: TTS speech, room tone, Whisper word times, mistakes spliced in
    with ffmpeg, MP3. The control must give no flags; most planted mistakes must be found."""
    out = tmp_path / "samples"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "make_audio_samples.py"), "--out", str(out)], check=True)
    config = load_config()
    assert run_qc(out / "e2cl_control.mp3", "E2CL", config=config).issues == []
    truth = json.loads((out / "e2cl_planted_truth.json").read_text(encoding="utf-8"))["mistakes"]
    result = run_qc(out / "e2cl_planted.mp3", "E2CL", config=config)
    hits = [t for t in truth if any(abs(i.timestamp - t["time"]) <= 1.5 and i.type == t["type"] for i in result)]
    assert len(hits) >= len(truth) - 1, ([(t["type"], t["time"]) for t in truth if t not in hits],
                                         [(i.timestamp, i.type) for i in result])
