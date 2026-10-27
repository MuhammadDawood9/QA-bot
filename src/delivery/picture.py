"""Black frames, a frozen picture and flash frames, from every frame of the video.

Each decoded frame is shrunk to a small grey image (delivery.picture.analysis_width pixels wide)
and reduced to a few numbers: its share of black pixels and how much it differs from each of the
LAGS frames before it. Those numbers are cached per video, so changing a threshold re-judges a
video without reading it again. The frames come from the one decode of the video for every
picture pass (src/inputs/video_pass.py); read_frame_stats() decodes on its own when that didn't run.

    stats = FrameScanner(settings)            # scanner.feed(t, av_frame) for every frame, in order
    issues = picture_issues(stats.result(), settings)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from src.models import BLACK_FRAMES, FLASH_FRAME, FROZEN_PICTURE, Issue
from src.utils.timecode import format_timestamp

log = logging.getLogger("qcbot.delivery")

PICTURE_VERSION = "1"
LAGS = 4                    # differences kept to the 1..4 frames before: flashes of up to 3 frames


@dataclass
class FrameStats:
    """Per frame, in order: its time, its share of black pixels, and diffs[k][i] = mean grey-level
    difference between frame i and frame i-k-1 (None before the first frames)."""
    times: list[float] = field(default_factory=list)
    black: list[float] = field(default_factory=list)
    diffs: list[list[float | None]] = field(default_factory=lambda: [[] for _ in range(LAGS)])

    @property
    def frame_count(self) -> int:
        return len(self.times)

    @property
    def frame_step(self) -> float:
        """Typical time between frames."""
        if len(self.times) < 2:
            return 1 / 30
        steps = sorted(b - a for a, b in zip(self.times, self.times[1:]) if b > a)
        return steps[len(steps) // 2] if steps else 1 / 30

    def end_time(self) -> float:
        return self.times[-1] + self.frame_step if self.times else 0.0

    def diff(self, i: int, lag: int) -> float | None:
        """Difference between frame i and frame i - lag (lag 1..LAGS)."""
        if not 1 <= lag <= LAGS or not 0 <= i < len(self.times):
            return None
        return self.diffs[lag - 1][i]


class FrameScanner:
    """Fed every decoded frame (time, av.VideoFrame or a 2-D grey array) in order."""

    def __init__(self, settings: dict):
        self.width = max(8, int(settings.get("analysis_width", 64)))
        self.pixel_luma = float(settings.get("black_pixel_luma", 24))
        self.stats = FrameStats()
        self._recent: list = []                      # the last LAGS small frames, newest last

    def feed(self, t: float, frame) -> None:
        import numpy as np

        grey = frame if isinstance(frame, np.ndarray) else self._small(frame)
        grey = np.asarray(grey, dtype=np.float32)
        self.stats.times.append(round(float(t), 4))
        self.stats.black.append(round(float((grey <= self.pixel_luma).mean()), 4))
        for k in range(LAGS):
            prev = self._recent[-1 - k] if len(self._recent) > k else None
            value = (round(float(np.abs(grey - prev).mean()), 3)
                     if prev is not None and prev.shape == grey.shape else None)
            self.stats.diffs[k].append(value)
        self._recent = (self._recent + [grey])[-LAGS:]

    def _small(self, frame):
        h = max(2, round(self.width * frame.height / max(1, frame.width)))
        return frame.reformat(width=self.width, height=h, format="gray").to_ndarray()

    def result(self) -> FrameStats:
        return self.stats


# --- the cache -----------------------------------------------------------------------------------

def frame_cache_file(video: Path, settings: dict, cache_dir: Path) -> Path:
    from src.utils.system import file_sha256

    key = f"{PICTURE_VERSION}-w{int(settings.get('analysis_width', 64))}-b{float(settings.get('black_pixel_luma', 24)):g}"
    return Path(cache_dir) / f"{file_sha256(video)[:16]}-{key}.json"


def save_frame_stats(path: Path, video: Path, stats: FrameStats) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"video": Path(video).name, "times": stats.times, "black": stats.black,
                               "diffs": stats.diffs}), encoding="utf-8")
    tmp.replace(path)


def load_frame_stats(path: Path) -> FrameStats | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        stats = FrameStats(list(data["times"]), list(data["black"]), [list(d) for d in data["diffs"]])
        if len(stats.diffs) != LAGS or any(len(d) != len(stats.times) for d in stats.diffs):
            return None
        return stats
    except (OSError, ValueError, KeyError, TypeError):
        log.warning("cached frame measurements %s are unreadable; reading the video again", path.name)
        return None


def read_frame_stats(video: Path, settings: dict, cache_dir: Path, progress=None) -> FrameStats:
    """From the cache, else by decoding every frame (when the shared read of the video didn't run)."""
    from src.inputs.video_ocr import shared_frames

    cache = frame_cache_file(video, settings, cache_dir)
    stats = load_frame_stats(cache)
    if stats is not None:
        return stats
    scanner = FrameScanner(settings)

    def every(t, duration, frame):
        scanner.feed(t, frame)
        if progress is not None and duration and scanner.stats.frame_count % 30 == 0:
            progress(min(1.0, t / duration))

    for _ in shared_frames(video, {}, every_frame=every):
        pass
    stats = scanner.result()
    save_frame_stats(cache, video, stats)
    return stats


# --- judging -------------------------------------------------------------------------------------

def _runs(flags: list[bool]) -> list[tuple[int, int]]:
    """(first, last) index of each run of True."""
    out, start = [], None
    for i, f in enumerate(flags + [False]):
        if f and start is None:
            start = i
        elif not f and start is not None:
            out.append((start, i - 1))
            start = None
    return out


def black_runs(stats: FrameStats, settings: dict) -> list[tuple[float, float]]:
    """(start, end) of black stretches long enough to flag; a fade at either end of the video
    (up to edge_black_sec) is fine."""
    share = float(settings.get("black_share", 0.98))
    min_sec = float(settings.get("min_black_sec", 0.1))
    edge = float(settings.get("edge_black_sec", 1.0))
    step, end_of_video = stats.frame_step, stats.end_time()
    out = []
    for a, b in _runs([s >= share for s in stats.black]):
        start, end = stats.times[a], stats.times[b] + step
        if end - start + 1e-6 < min_sec:
            continue
        at_edge = a == 0 or b == stats.frame_count - 1
        if at_edge and end - start <= edge + 1e-6:
            continue
        out.append((start, end))
    return out


def frozen_runs(stats: FrameStats, settings: dict) -> list[tuple[float, float]]:
    """(start, end) of stretches where the picture doesn't change, black left out."""
    limit = float(settings.get("freeze_diff", 0.15))
    min_sec = float(settings.get("min_freeze_sec", 2.0))
    share = float(settings.get("black_share", 0.98))
    still = [d is not None and d < limit and stats.black[i] < share for i, d in enumerate(stats.diffs[0])]
    out = []
    for a, b in _runs(still):
        start, end = stats.times[max(0, a - 1)], stats.times[b] + stats.frame_step   # frame a-1 is the first still one
        if end - start + 1e-6 >= min_sec:
            out.append((start, end))
    return out


def flash_frames(stats: FrameStats, settings: dict) -> list[tuple[float, int]]:
    """(time, frames) of 1..max_flash_frames frames unlike both the frame before and the frame after,
    where those two are alike: a stray frame left at a cut, or a flash of another shot."""
    big = float(settings.get("flash_diff", 40.0))
    close = float(settings.get("flash_neighbour_diff", 12.0))
    longest = max(1, min(int(settings.get("max_flash_frames", 2)), LAGS - 1))
    out, i, n = [], 1, stats.frame_count
    while i < n:
        found = 0
        for k in range(1, longest + 1):                # frames i .. i+k-1 are the flash
            after = i + k
            if after >= n:
                break
            into, out_of = stats.diff(i, 1), stats.diff(after, 1)
            across = stats.diff(after, k + 1)          # frame after vs the frame before the flash
            if into is None or out_of is None or across is None:
                continue
            if into >= big and out_of >= big and across <= close:
                found = k
                break
        if found:
            out.append((stats.times[i], found))
            i += found + 1
        else:
            i += 1
    return out


def picture_issues(stats: FrameStats, settings: dict) -> list[Issue]:
    out = []
    for start, end in black_runs(stats, settings):
        span = f"{format_timestamp(start)} - {format_timestamp(end)}"
        out.append(Issue(0, start, BLACK_FRAMES, "high", span,
                         f"The picture is black for {end - start:.2f} s.",
                         "Fill the gap in the timeline, or check it's an intended fade."))
    for start, end in frozen_runs(stats, settings):
        span = f"{format_timestamp(start)} - {format_timestamp(end)}"
        out.append(Issue(0, start, FROZEN_PICTURE, "low", span,
                         f"The picture doesn't change for {end - start:.1f} s: a frozen frame, a missing clip "
                         f"or an export glitch, unless it's a still on purpose.",
                         "Check the timeline here: replace the frozen frame or confirm the still is intended."))
    for t, frames in flash_frames(stats, settings):
        out.append(Issue(0, t, FLASH_FRAME, "high", format_timestamp(t),
                         f"{frames} frame{'s' if frames != 1 else ''} here {'don' if frames != 1 else 'doesn'}'t match "
                         f"the frames on either side: a stray frame or flash at the edit.",
                         "Step through the cut frame by frame and remove the stray frame."))
    return out
