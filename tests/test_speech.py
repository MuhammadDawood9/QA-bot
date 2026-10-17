"""The speech skip rule (src/audio/speech.py): when there is too little speech to compare
captions with, and the measured numbers behind that decision."""

from __future__ import annotations

from src.audio.speech import summarize
from src.models import SpokenWord

SETTINGS = {"min_words": 10, "min_speech_coverage": 0.05, "min_mean_word_probability": 0.5,
            "no_speech_probability": 0.6, "no_speech_share": 0.5}


def words(n: int, probability: float = 0.9, start: float = 0.0, step: float = 0.5,
          length: float = 0.3) -> list[SpokenWord]:
    return [SpokenWord(f"word{i}", start + i * step, start + i * step + length, probability) for i in range(n)]


def reason(spoken, segments=None, duration=27.0, settings=None):
    return summarize(spoken, segments, duration, settings or SETTINGS).reason(settings or SETTINGS)


def test_a_word_or_two_is_too_little():
    got = reason([SpokenWord("Yeah", 0.5, 0.7, 0.9)])
    assert got.startswith("only 1 word in 27 s")
    assert "coverage 1%" in got and "mean probability 0.90" in got


def test_enough_clear_speech_runs_normally():
    assert reason(words(20)) is None


def test_the_word_limit_is_a_boundary():
    assert reason(words(9)) is not None                     # 9 < 10: skipped
    assert reason(words(10)) is None                        # 10: runs


def test_speech_covering_too_little_of_the_audio_is_skipped():
    short = [SpokenWord(f"w{i}", i * 0.05, i * 0.05 + 0.04, 0.9) for i in range(15)]   # 0.75 s in 60 s
    got = reason(short, duration=60.0)
    assert got is not None and got.startswith("speech covers only 1% of the audio")


def test_a_silent_track_is_skipped():
    got = reason([], segments=[(0.0, 27.0, 0.9)])
    assert got.startswith("no speech was found in the audio")
    assert "no-speech share 100%" in got


def test_low_probability_words_look_like_guesses_over_music():
    got = reason(words(40, probability=0.2))
    assert got is not None and got.startswith("the words Whisper heard look like guesses over music")


def test_high_no_speech_share_is_music_not_speech():
    """The positive half of the guard on the no-speech rule: measured 100% against a 50% limit
    must fire. (Compare the measured value with itself and this test can never pass.)"""
    got = reason(words(40), segments=[(0.0, 27.0, 0.9)])
    assert got is not None and got.startswith("Whisper hears mostly music or noise")


def test_low_no_speech_share_is_left_alone():
    """The negative half: a real transcript whose segments are sure they hold speech (10%) must
    not be skipped. (A rule that compares a value with itself would wrongly skip this one.)"""
    assert reason(words(40), segments=[(0.0, 27.0, 0.1)]) is None


def test_no_speech_share_needs_whisper_segments():
    summary = summarize(words(40), None, 27.0, SETTINGS)
    assert summary.no_speech_share is None
    assert summary.reason(SETTINGS) is None                 # an old cache without segments still runs


def test_measurements_carry_every_number():
    summary = summarize(words(1), [(0.0, 27.0, 0.9)], 27.0, SETTINGS)
    text = summary.measurements()
    assert "speech " in text and "coverage " in text and "mean probability " in text and "no-speech share " in text


def test_spoken_time_merges_overlapping_words():
    overlapping = [SpokenWord("a", 0.0, 2.0, 0.9), SpokenWord("b", 1.0, 3.0, 0.9)]
    assert summarize(overlapping, None, 10.0, SETTINGS).speech_seconds == 3.0


def test_the_limits_come_from_the_settings():
    five = words(5)
    assert reason(five) is not None                                          # 5 < default 10
    assert reason(five, settings={**SETTINGS, "min_words": 3}) is None        # overridden
