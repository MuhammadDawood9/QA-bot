"""The audio-notes ceiling tool (scripts/audio_diagnostics.py) and the E2CS client file."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import audio_diagnostics as D  # noqa: E402

from src.audio.edits import effective_settings  # noqa: E402
from src.config import load_client, load_config  # noqa: E402
from src.engine import audio_settings  # noqa: E402

CONFIG = load_config()


def test_e2cs_client_switches_audio_edits_on_at_the_defaults():
    e2cs = load_client(CONFIG, "E2CS")
    settings = audio_settings(CONFIG, e2cs, audio_only=True)
    assert settings["enabled"] is True
    eff = effective_settings(settings)
    default = CONFIG["audio"]["levels"]["medium"]
    assert eff["dead_air_sec"] == default["dead_air_sec"]                 # no override: measured
    assert eff["acoustic_similarity"] == default["acoustic_similarity"]   # tuning did not pay off
    assert eff["cut_score"] == default["cut_score"]


def test_parse_seconds_reads_timecodes_and_skips_ranges_and_blanks():
    assert D.parse_seconds("00:00:52:13") == 52 + 13 / 30
    assert D.parse_seconds("01:02:03:00") == 3723.0
    assert D.parse_seconds("14 \u2013 0") is None                           # a range
    assert D.parse_seconds("") is None
    assert D.parse_seconds("   ") is None


def test_transcript_pattern_detectors_see_repeats_the_check_targets():
    words = [("we", 0.0), ("that", 1.0), ("that", 1.2), ("market", 1.4),
             ("the", 4.0), ("the", 4.1), ("office", 4.2), ("the", 5.0), ("office", 5.2)]
    assert D.adjacent_duplicate(words, 1.1, 5.0, 1.0) == "that"
    assert D.immediate_phrase(words, 4.6, 5.0, 1.5) == "the office"
    assert D.adjacent_duplicate(words, 1.1, 0.05, 1.0) is None             # window too tight
    assert D.immediate_phrase(words, 30.0, 5.0, 1.5) is None
