"""End-to-end run on the synthetic sample video: real ffmpeg, real Whisper, real audio.

    python scripts/make_sample_video.py          (once, makes data/samples/synthetic_sample.*)
    python -m pytest --run-slow tests/test_sample_video.py

The first run downloads the Whisper model; later runs read the cached transcript.
"""

import json
import shutil
from pathlib import Path

import pytest

from src.audio.transcribe import get_transcript
from src.checks import transcript
from src.checks.context import CheckContext
from src.config import resolve_path
from src.inputs.srt_loader import load_srt
from tests.helpers import CONFIG

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"
VIDEO = SAMPLES / "synthetic_sample.mp4"


@pytest.mark.slow
def test_synthetic_sample_video():
    if not VIDEO.exists():
        pytest.skip("run: python scripts/make_sample_video.py")
    if not (CONFIG["machine"].get("ffmpeg_path") or shutil.which("ffmpeg")):
        pytest.skip("ffmpeg is not installed")

    settings = CONFIG["checks"]["transcript"]
    words = get_transcript(VIDEO, CONFIG["machine"], settings, [],
                           resolve_path(CONFIG, "transcript_cache_dir"), resolve_path(CONFIG, "models_dir"))
    assert len(words) > 40

    captions = load_srt(SAMPLES / "synthetic_sample.srt")
    issues = transcript.check(captions, settings, CheckContext(transcript=words))
    found = {(i.caption_id, i.type) for i in issues}

    expected = json.loads((SAMPLES / "synthetic_sample_expected.json").read_text(encoding="utf-8"))
    for item in expected["expected"]:
        assert (item["caption_id"], item["type"]) in found, f"missed: {item}"
    flagged_clean = {cid for cid, _ in found} & set(expected["clean_caption_ids"])
    assert not flagged_clean, f"false alarms on clean captions {flagged_clean}: {issues}"
