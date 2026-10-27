"""One decode of the video for every pass over the picture that isn't cached yet.

The burned-in captions (OCR, 5 frames a second), the on-screen graphics (OCR of the whole frame
every 2 s) and the color measurements (4 frames a second) each used to decode the whole video
themselves. Decoding is the costly part once the per-frame work is quick, so this reads the video
once and hands each pass exactly the frames it would have picked on its own (same results),
then stores each result in that pass's own cache. The engine's usual calls then load from there.

    ran = read_video_once(video, captions=ocr_settings, screen=ocr_settings, color=color_settings, ...)
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Sequence

from src.inputs.video_ocr import (CaptionReader, ScreenTextReader, _load, captions_cache_file, rapidocr_engine,
                                  save_captions, save_screen_text, screen_cache_file, shared_frames)
from src.models import Caption
from src.utils.progress import Stage, silent

log = logging.getLogger("qcbot.video")


def read_video_once(video: Path, *, ocr: dict, ocr_cache: Path, read_captions: bool, read_screen: bool,
                    known_captions: Sequence[Caption] | None = None, color: dict | None = None,
                    caption_region: dict | None = None, color_cache: Path | None = None,
                    models_dir: Path | None = None, engine=None, progress: Stage | None = None,
                    picture: dict | None = None, picture_cache: Path | None = None) -> list[str]:
    """Run the passes whose results aren't cached, in one decode, and cache their results.

    ocr             the OCR settings (captions and on-screen text)
    read_captions   the captions are read from the video (no caption file)
    read_screen     the on-screen text check is on
    known_captions  the captions when they come from a caption file (the on-screen text pass
                    leaves them out)
    color           the color settings, or None when color isn't checked
    picture         the delivery checks' picture settings (every frame measured), or None
    Returns the names of the passes it ran: [] when everything was in the caches.
    """
    from src.video.color import ColorAnalyzer, color_cache_file, face_detector, load_shots, save_shots

    progress = progress or silent()
    video = Path(video)
    wanted: dict[str, float] = {}

    captions: list[Caption] | None = list(known_captions) if known_captions is not None else None
    if read_captions:
        cache_file = captions_cache_file(video, ocr, ocr_cache)
        cached = _load(cache_file) if cache_file.is_file() else None
        if cached is None:
            wanted["captions"] = float(ocr.get("sample_fps", 5))
        else:
            captions = cached
    every = float(ocr.get("screen_text_every_sec", 2.0))
    if read_screen and every > 0:
        # Its cache name depends on the captions; unknown captions mean it can't be cached yet.
        if captions is None or not screen_cache_file(video, ocr, captions, ocr_cache).is_file():
            wanted["screen"] = 1.0 / every
    if color is not None and color_cache is not None:
        if load_shots(color_cache_file(video, color, caption_region, color_cache)) is None:
            wanted["color"] = float(color.get("sample_fps", 4))
    scanner = None
    if picture is not None and picture_cache is not None:
        from src.delivery.picture import FrameScanner, frame_cache_file, load_frame_stats

        if load_frame_stats(frame_cache_file(video, picture, picture_cache)) is None:
            scanner = FrameScanner(picture)
    if not wanted and scanner is None:
        return []

    names = {"captions": "captions", "screen": "on-screen text", "color": "color"}
    log.info("reading %s once for: %s...", video.name,
             ", ".join([names[n] for n in wanted] + (["delivery frames"] if scanner else [])))
    started = time.monotonic()
    if ("captions" in wanted or "screen" in wanted) and engine is None:
        engine = rapidocr_engine(ocr)
    readers = {}
    if "captions" in wanted:
        readers["captions"] = CaptionReader(ocr, engine)
    if "screen" in wanted:
        readers["screen"] = ScreenTextReader(ocr, engine)
    if "color" in wanted:
        readers["color"] = ColorAnalyzer(color, caption_region,
                                         face_detector(models_dir, float(color.get("face_min_score", 0.8)))
                                         if color.get("skin_tones", True) else None)

    def every_frame(t, duration, frame):
        scanner.feed(t, frame)
        if not wanted and duration and scanner.stats.frame_count % 30 == 0:
            progress(t / duration)                      # no sampled frames to report progress with

    shared_reads = 0
    for t, duration, image, due in shared_frames(video, wanted, every_frame if scanner else None):
        progress(t / duration if duration else 0.0)
        for name in due:                                # captions come before screen: see `wanted`
            if name == "screen" and "captions" in due and readers["captions"].whole_frame_boxes is not None:
                readers["screen"].feed(t, image, boxes=readers["captions"].whole_frame_boxes)
                shared_reads += 1                       # full-frame mode: that frame was just read
            else:
                readers[name].feed(t, image)
    if shared_reads:
        log.info("on-screen text: %d frames reused from the caption pass", shared_reads)

    if "captions" in readers:
        captions = readers["captions"].captions()
        save_captions(captions_cache_file(video, ocr, ocr_cache), video, ocr, captions)
    if "screen" in readers:
        save_screen_text(screen_cache_file(video, ocr, captions or [], ocr_cache), video,
                         readers["screen"].texts(captions or []))
    if "color" in readers:
        save_shots(color_cache_file(video, color, caption_region, color_cache), video, readers["color"].shots())
    if scanner is not None:
        from src.delivery.picture import save_frame_stats

        save_frame_stats(frame_cache_file(video, picture, picture_cache), video, scanner.result())
    log.info("read %s in %.0f s", video.name, time.monotonic() - started)
    return list(wanted) + (["picture"] if scanner is not None else [])
