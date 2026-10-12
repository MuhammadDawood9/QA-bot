"""Read burned-in captions from a video: sampled frames -> OCR -> captions, cached per video.

    captions = read_burned_in_captions(video, ocr_settings, cache_dir)

1. PyAV decodes the video and keeps sample_fps frames per second.
2. Each frame is cropped to the client's caption region ("full" = the whole frame).
3. When the crop hasn't changed since the last frame read, the last result is reused, so the OCR
   (the slow part) only runs when the text or picture changes.
4. RapidOCR finds and reads the text; boxes in ignore_regions and text matching ignore_patterns
   (titles, logos, "day 07" graphics) are dropped.
5. caption_builder groups the readings into captions, including word-by-word animation.

The captions are saved under cache/ocr/, keyed by a hash of the video file and the settings,
so rerunning the checks on the same video doesn't read it again.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterator, Sequence

from rapidfuzz import fuzz

from src.inputs.caption_builder import Reading, TextBox, build_captions, group_boxes, normalize
from src.models import Caption
from src.utils.cache import text_hash
from src.utils.progress import Stage, silent
from src.utils.system import file_sha256

log = logging.getLogger("qcbot.ocr")

# Bump when the reading or grouping logic changes, so old cached captions aren't reused.
OCR_VERSION = "2"

# image (H x W x 3, RGB, uint8) -> text boxes with positions in pixels of that image
OcrEngine = Callable[[object], list[TextBox]]

_WORD_CHARS = re.compile(r"\w")

FULL_FRAME = {"left": 0.0, "top": 0.0, "width": 1.0, "height": 1.0}


class OcrError(RuntimeError):
    pass


def parse_region(region, name: str = "region") -> dict:
    """"full" or {left, top, width, height} as fractions of the frame -> a checked dict."""
    if region in (None, "full"):
        return dict(FULL_FRAME)
    if not isinstance(region, dict):
        raise OcrError(f"ocr {name} must be \"full\" or {{left, top, width, height}}, got {region!r}")
    try:
        r = {k: float(region[k]) for k in ("left", "top", "width", "height")}
    except (KeyError, TypeError, ValueError):
        raise OcrError(f"ocr {name} needs left, top, width and height as numbers (fractions of the "
                       f"frame, 0 to 1), got {region!r}") from None
    if not (0 <= r["left"] < 1 and 0 <= r["top"] < 1 and 0 < r["width"] <= 1 and 0 < r["height"] <= 1
            and r["left"] + r["width"] <= 1.0001 and r["top"] + r["height"] <= 1.0001):
        raise OcrError(f"ocr {name} {region!r} is outside the frame: use fractions from 0 to 1")
    return r


def crop(frame, region: dict):
    h, w = frame.shape[:2]
    x0, y0 = int(region["left"] * w), int(region["top"] * h)
    x1, y1 = int(round((region["left"] + region["width"]) * w)), int(round((region["top"] + region["height"]) * h))
    return frame[y0:max(y1, y0 + 1), x0:max(x1, x0 + 1)]


def thumbnail(image, size: int = 192):
    """Small grey version of the crop (each pixel the average of a patch), for a cheap
    "has anything changed?" test."""
    import numpy as np

    grey = np.asarray(image, dtype=np.float32)
    if grey.ndim == 3:
        grey = grey.mean(axis=2)
    fy, fx = max(1, grey.shape[0] // size), max(1, grey.shape[1] // size)
    h, w = grey.shape[0] // fy * fy, grey.shape[1] // fx * fx
    return grey[:h, :w].reshape(h // fy, fy, w // fx, fx).mean(axis=(1, 3))


def changed(a, b, threshold: float, tiles: int = 12) -> bool:
    """True when any tile of a tiles x tiles grid changed by `threshold` grey levels on average.
    Tiles, not the whole picture: one new word is a big change in its own tile but almost nothing
    averaged over a full frame."""
    import numpy as np

    if a is None or b is None or a.shape != b.shape:
        return True
    diff = np.abs(a - b)
    th, tw = max(1, diff.shape[0] // tiles), max(1, diff.shape[1] // tiles)
    h, w = diff.shape[0] // th * th, diff.shape[1] // tw * tw
    if h == 0 or w == 0:
        return float(diff.mean()) >= threshold
    per_tile = diff[:h, :w].reshape(h // th, th, w // tw, tw).mean(axis=(1, 3))
    return float(per_tile.max()) >= threshold


def to_frame_boxes(boxes: Sequence[TextBox], region: dict, crop_w: int, crop_h: int) -> list[TextBox]:
    """Pixel boxes inside the crop -> fractions of the full frame."""
    out = []
    for b in boxes:
        out.append(TextBox(text=b.text, score=b.score,
                           left=region["left"] + b.left / crop_w * region["width"],
                           top=region["top"] + b.top / crop_h * region["height"],
                           right=region["left"] + b.right / crop_w * region["width"],
                           bottom=region["top"] + b.bottom / crop_h * region["height"]))
    return out


def _inside(box: TextBox, region: dict) -> bool:
    cx, cy = (box.left + box.right) / 2, (box.top + box.bottom) / 2
    return (region["left"] <= cx <= region["left"] + region["width"]
            and region["top"] <= cy <= region["top"] + region["height"])


class ReadingFilter:
    """Drops text that isn't a caption: unsure boxes, a lone letter or digit unless it's read
    surely (a round shape in the picture reads as "O"), boxes in ignore_regions, and blocks whose
    whole text matches one of ignore_patterns (case is ignored)."""

    def __init__(self, settings: dict):
        self.min_score = float(settings.get("min_box_score", 0.5))
        self.single_char_score = float(settings.get("min_single_char_score", 0.8))
        self.ignore_regions = [parse_region(r, "ignore_regions entry") for r in settings.get("ignore_regions") or []]
        try:
            # A space in a pattern also matches no space: the OCR may read "DAY 07" as "DAY07".
            self.patterns = [re.compile(p.replace(" ", r"\s*"), re.IGNORECASE)
                             for p in settings.get("ignore_patterns") or []]
        except re.error as exc:
            raise OcrError(f"ocr ignore_patterns has a bad pattern: {exc}") from None
        self.block_gap = float(settings.get("block_gap_lines", 1.5))

    def __call__(self, boxes: Sequence[TextBox]) -> list[Reading]:
        kept = [b for b in boxes if b.score >= self.min_score and b.text.strip()
                and (len(_WORD_CHARS.findall(b.text)) > 1 or b.score >= self.single_char_score)
                and not any(_inside(b, r) for r in self.ignore_regions)]
        readings = group_boxes(kept, self.block_gap)
        return [r for r in readings
                if _WORD_CHARS.search(r.text) and not any(p.fullmatch(r.flat) for p in self.patterns)]


def rapidocr_engine(settings: dict) -> OcrEngine:
    """RapidOCR (PaddleOCR models on onnxruntime, CPU). The models ship inside the package, so
    nothing is downloaded."""
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:      # pragma: no cover - only without the package installed
        raise OcrError("the OCR package is missing: pip install -r requirements.txt") from exc

    # limit_type "max" shrinks big frames to det_max_side; the package default ("min") enlarges
    # a wide, short caption strip several times over and makes every frame about 5x slower.
    ocr = RapidOCR(det_limit_type="max", det_limit_side_len=int(settings.get("det_max_side", 960)),
                   intra_op_num_threads=int(settings.get("cpu_threads", -1)))

    def read(image) -> list[TextBox]:
        result, _ = ocr(image[:, :, ::-1].copy(), use_cls=False)    # RapidOCR expects BGR, like OpenCV
        boxes = []
        for points, text, score in result or []:
            xs, ys = [p[0] for p in points], [p[1] for p in points]
            boxes.append(TextBox(text=str(text), score=float(score),
                                 left=min(xs), top=min(ys), right=max(xs), bottom=max(ys)))
        return boxes
    return read


# libav's codes for the color tags of a frame
_UNSPECIFIED_COLORSPACE = 2
_FULL_RANGE = 2


def frame_to_rgb(frame):
    """Full-range RGB (0-255) from a decoded frame, using the frame's own color tags.

    Tagged frames are converted by the matrix and range they carry. Untagged ones (common in
    edited exports) get what players assume: BT.709 for HD (wider than 1279 or taller than 576
    pixels), BT.601 for SD, and limited (16-235) range unless tagged full range. The plain libav
    default would use BT.601 for every untagged frame, which bends HD skin tones a few degrees."""
    if frame.colorspace == _UNSPECIFIED_COLORSPACE and (frame.width >= 1280 or frame.height > 576):
        return frame.reformat(format="rgb24", src_colorspace="ITU709",
                              src_color_range="JPEG" if frame.color_range == _FULL_RANGE else "MPEG",
                              dst_color_range="JPEG").to_ndarray()
    return frame.to_ndarray(format="rgb24")


def sample_frames(video: Path, fps: float) -> Iterator[tuple[float, float, object]]:
    """(time, video duration, RGB frame) about `fps` times per second, in order. The rotation a
    phone stores in the file is applied, so vertical videos come out upright."""
    for t, duration, image, _ in shared_frames(video, {"frames": fps}):
        yield t, duration, image


def shared_frames(video: Path, rates: dict[str, float]) -> Iterator[tuple[float, float, object, list[str]]]:
    """One decode of the video for several passes at their own rates: (time, video duration, RGB
    frame, names of the passes this frame is for). Each pass gets exactly the frames
    sample_frames(video, its rate) would give it."""
    import av
    import numpy as np

    try:
        container = av.open(str(video))
    except av.error.FFmpegError as exc:
        raise OcrError(f"could not open {video.name}: {exc}") from None
    with container:
        if not container.streams.video:
            raise OcrError(f"{video.name} has no video stream")
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        duration = (float(stream.duration * stream.time_base) if stream.duration
                    else (container.duration or 0) / 1_000_000)
        steps = {name: 1.0 / fps for name, fps in rates.items()}
        next_t = {name: 0.0 for name in rates}
        for frame in container.decode(stream):
            t = frame.time
            if t is None:
                continue
            due = [name for name in rates if t + 1e-6 >= next_t[name]]
            if not due:
                continue
            image = frame_to_rgb(frame)
            rotation = int(getattr(frame, "rotation", 0) or 0) % 360
            if rotation:
                image = np.ascontiguousarray(np.rot90(image, k=rotation // 90))
            yield t, duration, image, due
            for name in due:
                while next_t[name] <= t + 1e-6:
                    next_t[name] += steps[name]


def ocr_settings_key(settings: dict) -> str:
    relevant = {k: v for k, v in settings.items() if k not in ("cpu_threads", "low_confidence")}
    return text_hash(OCR_VERSION, json.dumps(relevant, sort_keys=True, default=str))[:10]


def captions_cache_file(video: Path, settings: dict, cache_dir: Path) -> Path:
    return Path(cache_dir) / f"{file_sha256(video)[:16]}-{ocr_settings_key(settings)}.json"


def screen_cache_file(video: Path, settings: dict, captions: Sequence[Caption], cache_dir: Path) -> Path:
    captions_key = text_hash(*(c.flat_text for c in captions))[:6]     # captions are left out of it
    return Path(cache_dir) / f"{file_sha256(video)[:16]}-{ocr_settings_key(settings)}-screen-{captions_key}.json"


def save_screen_text(path: Path, video: Path, texts: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump({"video": video.name, "screen_text": list(texts)}, f, ensure_ascii=False, indent=1)


def save_captions(path: Path, video: Path, settings: dict, captions: Sequence[Caption]) -> None:
    _save(path, video, settings, captions)


def _save(path: Path, video: Path, settings: dict, captions: Sequence[Caption]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"video": video.name, "settings": settings,
            "captions": [[c.start, c.end, c.text, c.ocr_confidence] for c in captions]}
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, default=str)
    tmp.replace(path)


def _load(path: Path) -> list[Caption] | None:
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        return [Caption(id=i, start=s, end=e, text=t, source="ocr", ocr_confidence=c)
                for i, (s, e, t, c) in enumerate(data["captions"], start=1)]
    except (OSError, ValueError, KeyError, TypeError):
        log.warning("cached OCR captions %s are unreadable; reading the video again", path.name)
        return None


class CaptionReader:
    """The caption OCR pass one frame at a time: feed() each sampled (time, RGB frame) in order,
    then captions(). The OCR runs only when the caption area changed."""

    def __init__(self, settings: dict, engine: OcrEngine):
        self.settings, self.engine = settings, engine
        self.fps = float(settings.get("sample_fps", 5))
        self.region = parse_region(settings.get("region", "full"))
        self.keep = ReadingFilter(settings)
        self.threshold = float(settings.get("change_threshold", 6.0))
        self.readings_by_frame: list[tuple[float, list[Reading]]] = []
        self._last_thumb, self._last_readings, self.ocr_runs = None, [], 0

    def feed(self, t: float, image) -> None:
        part = crop(image, self.region)
        thumb = thumbnail(part)
        if changed(thumb, self._last_thumb, self.threshold):
            boxes = to_frame_boxes(self.engine(part), self.region, part.shape[1], part.shape[0])
            self._last_readings, self._last_thumb = self.keep(boxes), thumb
            self.ocr_runs += 1
        self.readings_by_frame.append((t, self._last_readings))

    def captions(self) -> list[Caption]:
        log.info("OCR ran on %d of %d frames", self.ocr_runs, len(self.readings_by_frame))
        return drop_long_text(build_captions(self.readings_by_frame, 1.0 / self.fps, self.settings), self.settings)


def read_frames(video: Path, settings: dict, engine: OcrEngine,
                frames: Iterator[tuple[float, float, object]] | None = None,
                progress: Stage | None = None) -> list[Caption]:
    """The OCR pass itself (no cache): frames -> readings -> captions."""
    progress = progress or silent()
    reader = CaptionReader(settings, engine)
    frames = frames if frames is not None else sample_frames(video, reader.fps)
    for t, duration, image in frames:
        progress(t / duration if duration else 0.0)
        reader.feed(t, image)
    return reader.captions()


def drop_long_text(captions: list[Caption], settings: dict) -> list[Caption]:
    """Text on screen longer than max_caption_sec is a logo or title, not a caption (it is still
    read by the on-screen text pass). Captions are renumbered."""
    limit = float(settings.get("max_caption_sec", 12.0))
    if limit <= 0:
        return captions
    kept = [c for c in captions if c.duration <= limit]
    for c in captions:
        if c.duration > limit:
            log.info("not a caption (on screen %.0f s): %s", c.duration, c.flat_text)
    return [replace(c, id=n) for n, c in enumerate(kept, start=1)]


def read_screen_text(video: Path, settings: dict, captions: Sequence[Caption], cache_dir: Path | None = None,
                     engine: OcrEngine | None = None,
                     frames: Iterator[tuple[float, float, object]] | None = None,
                     progress: Stage | None = None) -> list[str]:
    """On-screen text that isn't a caption (logos, name titles, graphics): the whole frame read
    every screen_text_every_sec seconds, minus anything in the caption region and anything that
    is one of the captions. Each different text once, in order of first appearance."""
    every = float(settings.get("screen_text_every_sec", 2.0))
    if every <= 0:
        return []
    progress = progress or silent()
    video = Path(video)
    cache_file = None
    if cache_dir is not None and frames is None:
        cache_file = screen_cache_file(video, settings, captions, cache_dir)
        if cache_file.is_file():
            try:
                with cache_file.open(encoding="utf-8") as f:
                    return list(json.load(f)["screen_text"])
            except (OSError, ValueError, KeyError, TypeError):
                log.warning("cached on-screen text %s is unreadable; reading again", cache_file.name)

    reader = ScreenTextReader(settings, engine or rapidocr_engine(settings))
    frames = frames if frames is not None else sample_frames(video, 1.0 / every)
    for t, duration, image in frames:
        progress(t / duration if duration else 0.0)
        reader.feed(t, image)
    texts = reader.texts(captions)
    if cache_file is not None:
        save_screen_text(cache_file, video, texts)
    return texts


class ScreenTextReader:
    """The on-screen text pass one frame at a time: feed() each sampled (time, RGB frame), then
    texts(captions). The captions are only needed at the end, so this can run in the same decode
    as the caption pass."""

    def __init__(self, settings: dict, engine: OcrEngine):
        region = parse_region(settings.get("region", "full"))
        self.caption_region = None if region == FULL_FRAME else region
        self.keep = ReadingFilter(settings)
        self.engine = engine
        self.seen: dict[str, str] = {}          # each normalized reading once, in order of first appearance

    def feed(self, t: float, image) -> None:
        h, w = image.shape[:2]
        boxes = [TextBox(b.text, b.score, b.left / w, b.top / h, b.right / w, b.bottom / h) for b in self.engine(image)]
        if self.caption_region is not None:
            boxes = [b for b in boxes if not _inside(b, self.caption_region)]
        for r in self.keep(boxes):
            self.seen.setdefault(normalize(r.flat), r.flat)

    def texts(self, captions: Sequence[Caption]) -> list[str]:
        """What isn't a caption, each different text once."""
        caption_texts = [normalize(c.flat_text) for c in captions]
        found: dict[str, str] = {}
        for n, flat in self.seen.items():
            key = n.replace(" ", "")              # the OCR may read the same logo with or without a space
            if n and key not in found and not any(n in c or fuzz.ratio(n, c) >= 90 for c in caption_texts):
                found[key] = flat
        return list(found.values())


def read_burned_in_captions(video: Path, settings: dict, cache_dir: Path | None = None,
                            engine: OcrEngine | None = None, progress: Stage | None = None) -> list[Caption]:
    """Captions burned into the video, from the cache if this exact video and settings were done."""
    video = Path(video)
    if not video.is_file():
        raise FileNotFoundError(f"video not found: {video}")
    parse_region(settings.get("region", "full"))             # fail on a bad region before the slow part
    cache_file = None
    if cache_dir is not None:
        cache_file = captions_cache_file(video, settings, cache_dir)
        if cache_file.is_file():
            captions = _load(cache_file)
            if captions is not None:
                log.info("captions: read from the video earlier, from cache (%s)", cache_file.name)
                return captions

    log.info("reading the captions burned into %s (OCR on the CPU)...", video.name)
    started = time.monotonic()
    captions = read_frames(video, settings, engine or rapidocr_engine(settings), progress=progress)
    log.info("captions: %d read from the video in %.0f s", len(captions), time.monotonic() - started)
    if cache_file is not None:
        _save(cache_file, video, settings, captions)
    return captions
