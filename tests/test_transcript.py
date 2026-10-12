"""Caption-vs-audio check, with fake Whisper transcripts (no model, no ffmpeg, no audio)."""

import json
from pathlib import Path

import pytest

from src.audio import transcribe
from src.checks import transcript
from src.checks.context import CheckContext
from src.main import main
from src.models import CAPTION_EARLY, EXTRA_WORD, MISSING_WORD, WRONG_WORD, Caption, SpokenWord
from src.utils import system
from tests.helpers import CONFIG

SETTINGS = {**CONFIG["checks"]["transcript"], "enabled": True}
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def said(text: str, start: float, step: float = 0.3, prob: float = 0.95, low: tuple = ()) -> list[SpokenWord]:
    """Fake Whisper words, one every `step` seconds. Words listed in `low` get probability 0.3."""
    out = []
    for i, word in enumerate(text.split()):
        t = start + i * step
        out.append(SpokenWord(word, t, t + step * 0.9, 0.3 if word in low else prob))
    return out


def run(caps, words, **overrides):
    return transcript.check(caps, {**SETTINGS, **overrides}, CheckContext(transcript=words))


def cap(id, start, end, text):
    return Caption(id, start, end, text)


def kinds(issues):
    return [(i.caption_id, i.type) for i in issues]


# --- real examples from data/examples.csv ---------------------------------------------

def test_wrong_word_are_for_or():                          # E3JB 00:01:47:19: "Or," not "Are."
    [issue] = run([cap(1, 0.0, 2.0, "Are you just want to rest?")], said("Or you just want to rest?", 0.1))
    assert issue.type == WRONG_WORD and issue.severity == "high"
    assert issue.message == 'Wrong word: caption says "Are you", audio sounds like "Or you".'
    assert issue.suggestion == 'Audio: "Or you just want to rest?"'
    assert issue.confidence == 0.95


def test_missing_word_but():                               # E3JB 00:00:40:29: missing a "but"
    [issue] = run([cap(1, 0.0, 3.0, "It was hard, it was worth it.")], said("It was hard, but it was worth it.", 0.1))
    assert issue.type == MISSING_WORD
    assert issue.message == 'Missing word "but": caption says "hard, it", audio sounds like "hard, but it".'


def test_duplicated_word_to():                             # E3JB 00:01:33:16: delete duplicate "to"
    [issue] = run([cap(1, 0.0, 2.0, "We need to to talk about it")], said("We need to talk about it", 0.1))
    assert issue.type == EXTRA_WORD
    # Either "to" can be the extra one; the alignment keeps the second.
    assert issue.message == ('Extra word "to" (repeated): caption says "need to to", '
                             'audio sounds like "need to".')


def test_extra_word_is():                                  # E2CS 00:00:08:09: "one is" -> "one"
    [issue] = run([cap(1, 0.0, 2.0, "Rule number one is never lose money")],
                  said("Rule number one never lose money", 0.1))
    assert issue.type == EXTRA_WORD
    assert issue.message == 'Extra word "is": caption says "one is never", audio sounds like "one never".'


def test_picked_up_for_peaked_up():                        # E2CS 00:00:07:50: "peaked up" not "picked up"
    [issue] = run([cap(1, 0.0, 2.0, "Sales picked up in summer")], said("Sales peaked up in summer", 0.1))
    assert issue.message == 'Wrong word: caption says "Sales picked up", audio sounds like "Sales peaked up".'


def test_only_same_shown_for_in_the_same_direction():      # E2CS 00:00:37:02
    caps = [cap(1, 0.0, 1.2, "They are all moving"), cap(2, 1.3, 2.6, "same")]
    words = said("They are all moving", 0.0) + said("in the same direction", 1.35)
    issues = run(caps, words)
    assert kinds(issues) == [(2, MISSING_WORD), (2, MISSING_WORD)]
    assert issues[0].message == 'Missing words "in the": caption says "same", audio sounds like "in the same".'
    assert issues[1].message == 'Missing word "direction": caption says "same", audio sounds like "same direction".'


def test_caption_appears_before_the_word_is_spoken():     # E2JG 00;00;13;49: "bright" came early
    [issue] = run([cap(1, 10.0, 13.0, "bright colors everywhere")], said("bright colors everywhere", 11.2))
    assert issue.type == CAPTION_EARLY
    assert issue.message == ('Caption appears 1.2s before "bright" is spoken '
                             '(caption at 00:10.000, word at 00:11.200).')
    assert issue.suggestion == "Start the caption at 00:11.200."


def test_small_lead_is_fine():
    assert run([cap(1, 10.0, 13.0, "bright colors everywhere")], said("bright colors everywhere", 10.4)) == []


# --- only flag what Whisper heard clearly --------------------------------------------

def test_unclear_audio_is_not_used_as_evidence():
    caps = [cap(1, 0.0, 2.0, "Are you just want to rest")]
    assert run(caps, said("Or you just want to rest", 0.1, low=("Or",))) == []
    assert len(run(caps, said("Or you just want to rest", 0.1, low=("Or",)), min_word_probability=0.2)) == 1


def test_extra_word_needs_clear_neighbours():
    caps = [cap(1, 0.0, 2.0, "We need to to talk")]
    assert run(caps, said("We need to talk", 0.1, prob=0.5)) == []


def test_extra_word_uses_the_average_of_its_neighbours():
    # From the synthetic sample: "one," was heard at 0.64 and "never" at 0.91 around the extra "is".
    caps = [cap(1, 0.0, 3.0, "Rule number one is, never lose money.")]
    words = said("Rule number one, never lose money.", 0.1)
    words[2] = SpokenWord("one,", words[2].start, words[2].end, 0.64)
    [issue] = run(caps, words)
    assert issue.type == EXTRA_WORD and issue.confidence == round((0.64 + 0.95) / 2, 2)
    words[3] = SpokenWord("never", words[3].start, words[3].end, 0.5)
    assert run(caps, words) == []                    # (0.64 + 0.5) / 2 is below 0.7


# --- things that are not mistakes ---------------------------------------------------

def test_exact_match_has_no_issues():
    caps = [cap(1, 0.0, 1.5, "Welcome back to the channel."), cap(2, 1.6, 3.0, "Today we talk money.")]
    words = said("Welcome back to the channel.", 0.1) + said("Today we talk money.", 1.7)
    assert run(caps, words) == []


def test_numbers_written_differently():
    caps = [cap(1, 0.0, 2.0, "We grew 40% in the 3rd quarter")]
    assert run(caps, said("We grew forty percent in the third quarter", 0.1)) == []
    assert run(caps, said("We grew 40% in the third quarter", 0.1)) == []
    assert run(caps, said("We grew 40 % in the third quarter", 0.1)) == []    # Whisper's spacing


def test_fillers_and_stutters_are_ignored():
    caps = [cap(1, 0.0, 3.0, "We need to talk about the budget")]
    assert run(caps, said("Um, we need to to talk about uh the budget", 0.1)) == []
    assert len(run(caps, said("we need to to talk about the budget", 0.1), ignore_spoken_repeats=False)) == 1


def test_near_identical_spellings_count_as_the_same_word():
    assert run([cap(1, 0.0, 2.0, "Mark McGwire hit seventy")], said("Mark McGuire hit seventy", 0.1)) == []
    assert run([cap(1, 0.0, 2.0, "We work twenty hourse a week")], said("We work twenty hours a week", 0.1)) == []


def test_split_or_joined_words_and_equivalents():
    assert run([cap(1, 0.0, 2.0, "I train every day, okay")], said("I train everyday, OK", 0.1)) == []
    assert run([cap(1, 0.0, 2.0, "we're gonna win")], said("we're going to win", 0.1)) == []


def test_neighbouring_captions_are_not_blamed():
    caps = [cap(1, 0.0, 1.0, "First we plan,"), cap(2, 1.05, 2.0, "then we build,"), cap(3, 2.05, 3.0, "and then we ship.")]
    words = said("First we plan, then we build, and then we ship.", 0.0, step=0.3)
    assert run(caps, words) == []


def test_text_that_is_not_spoken_is_skipped():
    caps = [cap(1, 0.0, 2.0, "CHAPTER ONE: THE BEGINNING"), cap(2, 2.1, 4.0, "So let me tell you")]
    words = said("So let me tell you", 2.2)
    assert run(caps, words) == []


def test_skipped_without_a_video_or_when_disabled():
    caps = [cap(1, 0.0, 2.0, "Are you sure")]
    assert transcript.check(caps, SETTINGS, CheckContext()) == []
    assert run(caps, said("Or you sure", 0.1), enabled=False) == []


# --- transcription, caching and ffmpeg ----------------------------------------------

def test_initial_prompt_lists_glossary_terms():
    assert transcribe.build_initial_prompt(["Mark McGwire", "Danja"]) == "Glossary: Mark McGwire, Danja."
    assert transcribe.build_initial_prompt([]) == ""
    long = transcribe.build_initial_prompt([f"Name{i}" for i in range(200)], max_chars=100)
    assert len(long) <= 100 and long.endswith(".")


def test_audio_is_extracted_as_mono_16k_with_an_argument_list(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        return type("R", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(system.subprocess, "run", fake_run)
    system.extract_audio(Path("my video.mp4"), tmp_path / "a.wav", "ffmpeg")
    cmd = seen["cmd"]
    assert isinstance(cmd, list) and "shell" not in seen["kwargs"]
    assert cmd[cmd.index("-i") + 1] == "my video.mp4"
    assert cmd[cmd.index("-ac") + 1] == "1" and cmd[cmd.index("-ar") + 1] == "16000"


def test_ffmpeg_failure_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(system.subprocess, "run",
                        lambda cmd, **kw: type("R", (), {"returncode": 1, "stderr": "Invalid data found"})())
    with pytest.raises(RuntimeError, match="Invalid data found"):
        system.extract_audio(Path("broken.mp4"), tmp_path / "a.wav", "ffmpeg")


def test_configured_ffmpeg_path_must_exist(tmp_path):
    with pytest.raises(system.MissingToolError, match="doesn't exist"):
        system.find_ffmpeg(str(tmp_path / "nope" / "ffmpeg"))
    real = tmp_path / "ffmpeg.exe"
    real.write_bytes(b"")
    assert system.find_ffmpeg(str(real)) == str(real)


@pytest.fixture
def fake_pipeline(monkeypatch):
    """No ffmpeg, no Whisper: count how often a transcription really happens."""
    calls = []
    monkeypatch.setattr(transcribe, "find_ffmpeg", lambda configured=None: "ffmpeg")
    monkeypatch.setattr(transcribe, "extract_audio", lambda video, wav, ffmpeg: wav)

    def fake_transcriber(wav, ws, prompt):
        calls.append((ws, prompt))
        return said("hello there", 0.5)
    return calls, fake_transcriber


def test_transcript_is_cached_per_video(tmp_path, fake_pipeline):
    calls, fake = fake_pipeline
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video bytes")
    cache_dir = tmp_path / "transcripts"
    machine = {"whisper_model_size": "base", "cpu_threads": 4}

    first = transcribe.get_transcript(video, machine, SETTINGS, ["Danja"], cache_dir, transcriber=fake)
    again = transcribe.get_transcript(video, machine, SETTINGS, ["Danja"], cache_dir, transcriber=fake)
    assert len(calls) == 1 and again == first
    ws, prompt = calls[0]
    assert ws == {"model_size": "base", "cpu_threads": 4, "device": "cpu", "compute_type": "int8",
                  "language": "en", "beam_size": 1, "vad_filter": True}
    assert prompt == "Glossary: Danja."

    renamed = tmp_path / "renamed.mp4"
    renamed.write_bytes(b"video bytes")                  # same content, other name: still cached
    transcribe.get_transcript(renamed, {**machine, "cpu_threads": 8}, SETTINGS, ["Danja"], cache_dir, transcriber=fake)
    assert len(calls) == 1

    transcribe.get_transcript(video, {**machine, "whisper_model_size": "small"}, SETTINGS, ["Danja"], cache_dir, transcriber=fake)
    transcribe.get_transcript(video, machine, SETTINGS, ["Danja", "=Mark McGwire"], cache_dir, transcriber=fake)
    video.write_bytes(b"edited video bytes")
    transcribe.get_transcript(video, machine, SETTINGS, ["Danja"], cache_dir, transcriber=fake)
    assert len(calls) == 4                               # model, glossary and content changes re-transcribe

    saved = json.loads(next(cache_dir.glob("*-base-*.json")).read_text(encoding="utf-8"))
    assert saved["words"][0] == ["hello", 0.5, 0.77, 0.95]


def test_damaged_cache_file_is_redone(tmp_path, fake_pipeline):
    calls, fake = fake_pipeline
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    cache_dir = tmp_path / "t"
    transcribe.get_transcript(video, {}, SETTINGS, [], cache_dir, transcriber=fake)
    next(cache_dir.glob("*.json")).write_text("{broken", encoding="utf-8")
    transcribe.get_transcript(video, {}, SETTINGS, [], cache_dir, transcriber=fake)
    assert len(calls) == 2


def test_missing_video(tmp_path):
    with pytest.raises(FileNotFoundError):
        transcribe.get_transcript(tmp_path / "none.mp4", {}, SETTINGS, [], tmp_path)


# --- command line --------------------------------------------------------------------

def test_cli_with_video(monkeypatch, capsys, tmp_path):
    srt = tmp_path / "c.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nAre you just want to rest\n", encoding="utf-8")
    monkeypatch.setattr("src.main.get_transcript", lambda *a, **k: said("Or you just want to rest", 0.1))
    assert main(["--srt", str(srt), "--video", str(tmp_path / "v.mp4"), "--no-llm"]) == 0
    out = capsys.readouterr().out
    assert "wrong_word" in out and 'audio sounds like "Or you"' in out


def test_cli_reports_a_missing_video(capsys, tmp_path):
    assert main(["--srt", str(FIXTURES / "sample_mistakes.srt"), "--video", str(tmp_path / "nope.mp4"),
                 "--no-llm"]) == 2
    assert "video not found" in capsys.readouterr().err
