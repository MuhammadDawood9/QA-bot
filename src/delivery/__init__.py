"""Technical delivery checks: the exported file itself, not what's in it.

    spec.py       resolution, aspect, frame rate, duration, bitrate, audio format vs the client's order
    sound.py      loudness (LUFS, true peak), clipping, a voice buried under the music
    picture.py    black frames, a frozen picture, flash frames (every frame, during the one read of the video)
    safe_area.py  burned-in captions under TikTok / Reels / Shorts buttons

Every threshold is in config.yaml "delivery:", and a client file's "delivery:" block changes any of
them. No UI and no printing: problems in one check are logged and noted in the stats, and the
other checks still run.

    settings = delivery_settings(config, client)
    issues, stats = check_delivery(video, settings, ffmpeg=..., captions=..., words=..., ...)
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from src.config import deep_merge
from src.models import Caption, Issue, delivery_issue_id

log = logging.getLogger("qcbot.delivery")


def delivery_settings(config: dict, client: dict) -> dict:
    """config.yaml "delivery:" with the client file's "delivery:" block laid over it."""
    return deep_merge(config.get("delivery") or {}, (client or {}).get("delivery") or {})


def picture_settings(settings: dict) -> dict | None:
    """The picture block when the frame checks run, else None."""
    if not settings.get("enabled", True):
        return None
    picture = settings.get("picture") or {}
    return picture if picture.get("enabled", True) else None


def check_delivery(path: Path, settings: dict, *, ffmpeg: str, captions: Sequence[Caption] = (),
                   words: Sequence | None = None, audio_only: bool = False, picture_cache: Path | None = None,
                   progress=None) -> tuple[list[Issue], dict]:
    """Every delivery check that's switched on, for one file. words: the speech transcript
    (SpokenWords), for the voice-under-music check; None skips that check."""
    from src.delivery.picture import picture_issues, read_frame_stats
    from src.delivery.safe_area import applies, safe_area_issues
    from src.delivery.sound import (clipping_issues, loudness_issues, measure_loudness, scan_clipping,
                                    voice_under_music)
    from src.delivery.spec import probe, spec_issues

    progress = progress or (lambda fraction: None)
    stats: dict = {"errors": []}
    issues: list[Issue] = []

    def attempt(name, work):
        try:
            return work()
        except (OSError, RuntimeError, ValueError) as exc:
            log.warning("delivery check %s not run: %s", name, exc)
            stats["errors"].append(f"{name}: {exc}")
            return None

    info = attempt("file", lambda: probe(path))
    if info is None:
        return [], stats
    stats["file"] = info.describe()
    issues += spec_issues(info, settings.get("spec") or {})
    progress(0.05)

    loud = settings.get("loudness") or {}
    if info.sample_rate and loud.get("enabled", True):
        measured = attempt("loudness", lambda: measure_loudness(path, ffmpeg))
        stats["loudness"] = measured
        issues += loudness_issues(measured, loud)
    progress(0.3)

    clip = settings.get("clipping") or {}
    if info.sample_rate and info.channels and clip.get("enabled", True):
        times = attempt("clipping", lambda: scan_clipping(path, ffmpeg, info.sample_rate, info.channels, clip))
        if times is not None:
            stats["clips"] = len(times)
            issues += clipping_issues(times, clip)
    progress(0.55)

    voice = settings.get("voice_under_music") or {}
    if info.sample_rate and voice.get("enabled", True):
        if not words:
            stats["voice_under_music"] = "skipped: no speech transcript"
        else:
            from src.audio.media import load_audio

            sr = int(voice.get("sample_rate", 16000))
            samples = attempt("voice under music", lambda: load_audio(path, ffmpeg, sr))
            if samples is not None:
                issues += voice_under_music(samples, sr, words, voice)
    progress(0.75)

    picture = picture_settings(settings)
    if not audio_only and info.has_picture and picture is not None and picture_cache is not None:
        frames = attempt("picture", lambda: read_frame_stats(path, picture, picture_cache))
        if frames is not None:
            stats["frames"] = frames.frame_count
            issues += picture_issues(frames, picture)
    progress(0.95)

    safe = settings.get("safe_area") or {}
    if not audio_only and info.has_picture and applies(safe, info.vertical):
        found, unknown = safe_area_issues(captions, safe)
        issues += found
        stats["safe_area"] = {"checked": sum(1 for c in captions if c.source == "ocr") - unknown, "unknown": unknown}
    progress(1.0)

    numbered, n = [], 0
    for issue in sorted(issues, key=lambda i: i.timestamp):
        if issue.caption_id == 0:                      # not tied to a caption: give it a delivery id
            n += 1
            issue = replace(issue, caption_id=delivery_issue_id(n))
        numbered.append(issue)
    stats["issues"] = len(numbered)
    return numbered, stats
