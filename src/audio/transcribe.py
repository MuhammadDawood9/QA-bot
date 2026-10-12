"""Speech-to-text for a video with faster-whisper, cached per video.

1. ffmpeg extracts the audio as mono 16 kHz WAV.
2. faster-whisper transcribes it on the CPU (int8, word timestamps, voice-activity filter, beam
   size 1), with glossary terms in the initial prompt so names come out spelled right.
3. The words (text, start, end, probability) are saved under cache/transcripts/, keyed by a hash
   of the video file plus the model and settings, so a rerun on the same video is instant.
"""

from __future__ import annotations

import json
import logging
import tempfile
import time
import wave
from pathlib import Path
from typing import Callable, Sequence

from src.models import SpokenWord
from src.utils.cache import text_hash
from src.utils.progress import Stage, silent
from src.utils.system import extract_audio, file_sha256, find_ffmpeg

log = logging.getLogger("qcbot.transcribe")

# (wav path, settings, initial prompt) -> words. Swapped for a fake in tests.
Transcriber = Callable[[Path, dict, str], list[SpokenWord]]


def build_initial_prompt(terms: Sequence[str], max_chars: int = 600) -> str:
    """Whisper reads the prompt as text that came "before" the audio; listing names there makes it
    more likely to spell them the same way. Whisper only uses the last ~224 tokens of a prompt."""
    if not terms:
        return ""
    prompt = "Glossary: " + ", ".join(terms) + "."
    if len(prompt) > max_chars:
        cut = prompt[:max_chars].rsplit(",", 1)[0]
        prompt = cut + "."
    return prompt


def whisper_settings(machine: dict, settings: dict) -> dict:
    """Everything that changes the transcript, in one place (it is also part of the cache key)."""
    return {
        "model_size": machine.get("whisper_model_size", "base"),
        "cpu_threads": int(machine.get("cpu_threads", 4)),
        "device": settings.get("device", "cpu"),
        "compute_type": settings.get("compute_type", "int8"),
        "language": settings.get("language", "en"),
        "beam_size": int(settings.get("beam_size", 1)),
        "vad_filter": bool(settings.get("vad_filter", True)),
    }


def read_wav(path: Path):
    """16-bit mono WAV (as written by extract_audio) -> float32 samples in -1..1.

    Reading it here, instead of letting faster-whisper decode the file, keeps its audio decoder
    (PyAV) out of the way: ffmpeg has already done the conversion."""
    import numpy as np

    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != 16000:
            raise RuntimeError(f"expected 16 kHz 16-bit mono audio in {path.name}")
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def _load_model(ws: dict, models_dir: Path | None):
    from faster_whisper import WhisperModel   # heavy import, only when actually transcribing

    kwargs = dict(device=ws["device"], compute_type=ws["compute_type"], cpu_threads=ws["cpu_threads"],
                  download_root=str(models_dir) if models_dir else None)
    try:
        return WhisperModel(ws["model_size"], local_files_only=True, **kwargs)   # no network once downloaded
    except Exception:
        log.info("downloading Whisper model '%s' (once; base is about 145 MB, small about 465 MB)...",
                 ws["model_size"])
        return WhisperModel(ws["model_size"], **kwargs)


def faster_whisper_transcriber(models_dir: Path | None = None, progress: Stage | None = None) -> Transcriber:
    progress = progress or silent()

    def transcribe(wav: Path, ws: dict, prompt: str) -> list[SpokenWord]:
        model = _load_model(ws, models_dir)
        segments, info = model.transcribe(
            read_wav(wav), language=ws["language"], beam_size=ws["beam_size"], word_timestamps=True,
            vad_filter=ws["vad_filter"], initial_prompt=prompt or None,
        )
        words: list[SpokenWord] = []
        duration = info.duration or 0.0
        for segment in segments:                  # a generator: transcription happens while iterating
            for w in segment.words or []:
                words.append(SpokenWord(text=w.word.strip(), start=float(w.start), end=float(w.end),
                                        probability=float(w.probability)))
            progress(segment.end / duration if duration else 0.0)    # also where a cancel takes effect
        return words
    return transcribe


def _save(path: Path, video: Path, digest: str, ws: dict, prompt: str, words: list[SpokenWord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"video": video.name, "sha256": digest, "settings": ws, "initial_prompt": prompt,
            "words": [[w.text, round(w.start, 3), round(w.end, 3), round(w.probability, 4)] for w in words]}
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False)
    tmp.replace(path)


def _load(path: Path) -> list[SpokenWord] | None:
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        return [SpokenWord(t, s, e, p) for t, s, e, p in data["words"]]
    except (OSError, ValueError, KeyError, TypeError):
        log.warning("cached transcript %s is unreadable; transcribing again", path.name)
        return None


def get_transcript(video: Path, machine: dict, settings: dict, glossary_terms: Sequence[str],
                   cache_dir: Path, models_dir: Path | None = None,
                   transcriber: Transcriber | None = None, progress: Stage | None = None) -> list[SpokenWord]:
    """The words spoken in the video, from the cache if this exact video was done before."""
    video = Path(video)
    if not video.is_file():
        raise FileNotFoundError(f"video not found: {video}")
    ws = whisper_settings(machine, settings)
    prompt = build_initial_prompt(glossary_terms, settings.get("initial_prompt_max_chars", 600))

    digest = file_sha256(video)
    settings_key = text_hash(json.dumps({k: v for k, v in ws.items() if k != "cpu_threads"}, sort_keys=True),
                             prompt)[:10]
    cache_file = Path(cache_dir) / f"{digest[:16]}-{ws['model_size']}-{settings_key}.json"
    if cache_file.is_file():
        words = _load(cache_file)
        if words is not None:
            log.info("transcript: from cache (%s)", cache_file.name)
            return words

    ffmpeg = find_ffmpeg(machine.get("ffmpeg_path"))
    transcriber = transcriber or faster_whisper_transcriber(models_dir, progress)
    with tempfile.TemporaryDirectory(prefix="qcbot-") as tmp:
        wav = extract_audio(video, Path(tmp) / "audio.wav", ffmpeg)
        log.info("transcribing %s with Whisper '%s' on the CPU...", video.name, ws["model_size"])
        started = time.monotonic()
        words = transcriber(wav, ws, prompt)
    log.info("transcript: %d words in %.0f s", len(words), time.monotonic() - started)
    _save(cache_file, video, digest, ws, prompt, words)
    return words
