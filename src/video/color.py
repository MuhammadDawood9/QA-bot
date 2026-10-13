"""Color grading checks: measured from the pixels, shot by shot.

    shots = read_color(video, settings, caption_region, cache_dir, models_dir)
    issues = color_issues(shots, settings)

1. Frames are sampled (sample_fps, default 4 a second) and shrunk to 480 px wide. Cuts are found
   on every sample; measurements and faces on every n-th frame of each shot.
2. A new shot starts where the color histogram jumps (a cut).
3. Every sampled frame is measured, leaving out the caption region (white caption text would
   otherwise count as blown-out highlights):
     brightness (Rec.709 luma) and its spread, share of clipped white and pure black pixels,
     saturation (mean Lab chroma), the tint of near-neutral pixels (whites and greys: where a
     color cast shows), and on faces found by the YuNet face detector, the skin's hue and chroma.
4. Each shot gets the median of its frames, and is flagged when it's outside normal ranges, when
   its skin tone or tint jumps from the previous shot with faces, or when its grade is far from
   the rest of the video ("alien to the rest of the footage").

These match the color notes in the QC history: blown highlights, skin tones, flat contrast with
lifted blacks, dull or oversaturated color, magenta/green/warm casts, too dark, and shots that
don't match. They are measurements, not taste: a deliberate look (a grey, desaturated sequence)
gets flagged too, so a client with such a style turns the matching flag off in its "color:" block.

Results are cached per video file and settings under cache/color/.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import median
from typing import Iterator, Sequence

from src.models import (COLOR_CAST, CRUSHED_BLACKS, FLAT_CONTRAST, OVEREXPOSED, SATURATION, SHOT_MISMATCH,
                        SKIN_TONE, UNDEREXPOSED, Issue, shot_issue_id, shot_number)
from src.utils.cache import text_hash
from src.utils.progress import Stage, silent
from src.utils.system import file_sha256
from src.utils.timecode import format_timestamp

log = logging.getLogger("qcbot.color")

COLOR_VERSION = "3"     # 2: untagged HD read as BT.709; 3: measurements and faces counted per shot
ANALYSIS_WIDTH = 480

YUNET_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
             "face_detection_yunet_2023mar.onnx")
YUNET_FILE = "face_detection_yunet_2023mar.onnx"
YUNET_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"


@dataclass
class FrameStats:
    t: float
    luma_median: float        # 0..255
    black_level: float        # 2nd percentile of luma: high = lifted, washed-out blacks
    white_level: float        # 98th percentile
    clipped_white: float      # share of pixels with a channel at 250+
    clipped_black: float      # share of pixels with luma at 6 or less
    chroma: float             # mean Lab chroma: how colorful
    cast_a: float | None      # mean Lab a* of near-neutral pixels: + magenta, - green
    cast_b: float | None      # mean Lab b* of near-neutral pixels: + yellow/warm, - blue/cool
    skin_hue: float | None = None      # Lab hue angle of skin on faces, degrees
    skin_chroma: float | None = None


@dataclass
class Shot:
    number: int
    start: float
    end: float
    frames: list[FrameStats] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.end - self.start

    def value(self, name: str) -> float | None:
        values = [getattr(f, name) for f in self.frames if getattr(f, name) is not None]
        return median(values) if values else None

    @property
    def middle(self) -> float:
        return round((self.start + self.end) / 2, 3)

    @property
    def is_black(self) -> bool:
        """A black screen (fade, intro, ending): no picture to grade."""
        return (self.value("luma_median") or 0) <= 10 and (self.value("white_level") or 0) <= 25


# --- measuring frames ----------------------------------------------------------------------

def frame_stats(t: float, rgb, mask, skin: tuple[float, float] | None = None) -> FrameStats:
    """Measurements of one frame (RGB uint8), over the pixels where mask is True."""
    import cv2
    import numpy as np

    # One channel at a time, in OpenCV where it can: masking or reducing (N, 3) float arrays was
    # most of the color pass's time. Same numbers as before.
    whole = mask is None or mask.all() or not mask.any()

    def pick(plane):
        return plane.reshape(-1) if whole else plane[mask]

    r, g, b8 = cv2.split(rgb)
    luma = pick(cv2.transform(rgb.astype(np.float32), np.array([[0.2126, 0.7152, 0.0722]], dtype=np.float32)))
    p2, p50, p98 = np.percentile(luma, [2, 50, 98])
    clipped = pick(cv2.max(cv2.max(r, g), b8)) >= 250

    L8, a8, bb8 = cv2.split(cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB))
    L = pick(L8).astype(np.float32) * (100 / 255)
    a = pick(a8).astype(np.float32) - 128
    b = pick(bb8).astype(np.float32) - 128
    chroma = cv2.magnitude(a, b).reshape(-1)
    # Where a color cast shows: near-neutral pixels (whites, greys) should have a* = b* = 0. A strong
    # cast leaves none near neutral; then the brightest unclipped pixels (where whites usually are).
    neutral = (chroma < 12) & (L > 25) & (L < 95)
    if neutral.mean() < 0.05 and (~clipped).sum() >= 50:
        bright = ~clipped & (L >= np.percentile(L[~clipped], 97)) & (L > 50)
        neutral = bright if bright.sum() >= 50 else np.zeros_like(bright)
    has_neutral = bool(neutral.any())
    return FrameStats(
        t=round(t, 3), luma_median=float(p50), black_level=float(p2), white_level=float(p98),
        clipped_white=float(clipped.mean()), clipped_black=float((luma <= 6).mean()),
        chroma=float(chroma.mean()),
        cast_a=float(a[neutral].mean()) if has_neutral else None,
        cast_b=float(b[neutral].mean()) if has_neutral else None,
        skin_hue=skin[0] if skin else None, skin_chroma=skin[1] if skin else None,
    )


def skin_of_faces(rgb, detector) -> tuple[float, float] | None:
    """Median Lab hue and chroma of the skin in the middle of the faces found, or None."""
    import cv2
    import numpy as np

    if detector is None:
        return None
    h, w = rgb.shape[:2]
    detector.setInputSize((w, h))
    _, faces = detector.detect(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    if faces is None or len(faces) == 0:
        return None
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    patches = []
    for x, y, fw, fh in faces[:, :4].astype(int):
        if fw < 24 or fh < 24:
            continue                                   # too small to measure skin reliably
        x0, y0 = max(0, x + fw // 4), max(0, y + fh // 4)
        patch = lab[y0:max(y0 + 1, y + 3 * fh // 4), x0:max(x0 + 1, x + 3 * fw // 4)].reshape(-1, 3)
        L = patch[:, 0] * (100 / 255)
        patches.append(patch[(L > 20) & (L < 95)])     # no eyes, nostrils or specular shine
    if not patches:
        return None
    skin = np.concatenate(patches)
    if len(skin) < 50:
        return None
    a, b = float(np.median(skin[:, 1] - 128)), float(np.median(skin[:, 2] - 128))
    return math.degrees(math.atan2(b, a)), math.hypot(a, b)


def histogram(rgb):
    import cv2

    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    hist = cv2.calcHist([hsv], [0, 1, 2], None, [16, 4, 4], [0, 180, 0, 256, 0, 256])
    return cv2.normalize(hist, hist).flatten()


def is_cut(prev_hist, hist, threshold: float) -> bool:
    import cv2

    return prev_hist is not None and cv2.compareHist(prev_hist, hist, cv2.HISTCMP_BHATTACHARYYA) >= threshold


def analysis_mask(height: int, width: int, caption_region: dict | None):
    """True where pixels are measured: everything outside the caption region."""
    import numpy as np

    mask = np.ones((height, width), dtype=bool)
    if caption_region:
        y0, y1 = int(caption_region["top"] * height), int((caption_region["top"] + caption_region["height"]) * height)
        x0, x1 = int(caption_region["left"] * width), int((caption_region["left"] + caption_region["width"]) * width)
        mask[y0:y1, x0:x1] = False
    return mask


class ColorAnalyzer:
    """The color pass one frame at a time: feed() each sampled (time, RGB frame) in order, then
    shots(). Lets one decode of the video feed the color and the OCR passes together."""

    def __init__(self, settings: dict, caption_region: dict | None = None, face_detector=None):
        self.settings, self.caption_region, self.face_detector = settings, caption_region, face_detector
        self.cut_threshold = float(settings.get("cut_threshold", 0.35))
        self.min_shot = float(settings.get("min_shot_sec", 0.5))
        # Cuts are looked for on every sampled frame (cheap); the measurements, and faces even more
        # so, on every n-th frame of each shot, counted from the shot's first frame so every shot
        # is measured and has its faces looked for at least once.
        self.measure_every = max(1, int(settings.get("measure_every_n_frames", 1)))
        self.face_every = max(1, int(settings.get("face_every_n_frames", 2)))
        self._shots: list[Shot] = []
        self._prev_hist, self._mask, self._in_shot, self._last_t = None, None, 0, 0.0

    def feed(self, t: float, image) -> None:
        import cv2

        shots = self._shots
        h, w = image.shape[:2]
        if w > ANALYSIS_WIDTH:
            image = cv2.resize(image, (ANALYSIS_WIDTH, round(h * ANALYSIS_WIDTH / w)), interpolation=cv2.INTER_AREA)
        if self._mask is None or self._mask.shape != image.shape[:2]:
            self._mask = analysis_mask(image.shape[0], image.shape[1], self.caption_region)
        hist = histogram(image)
        if not shots or (is_cut(self._prev_hist, hist, self.cut_threshold) and t - shots[-1].start >= self.min_shot):
            if shots:
                shots[-1].end = round(t, 3)
            shots.append(Shot(number=len(shots) + 1, start=round(t, 3), end=round(t, 3)))
            self._in_shot = 0
        if self._in_shot % self.measure_every == 0:
            skin = skin_of_faces(image, self.face_detector) if self._in_shot % self.face_every == 0 else None
            shots[-1].frames.append(frame_stats(t, image, self._mask, skin))
        self._prev_hist, self._in_shot, self._last_t = hist, self._in_shot + 1, t

    def shots(self) -> list[Shot]:
        if self._shots:
            step = 1.0 / float(self.settings.get("sample_fps", 4))
            self._shots[-1].end = round(self._last_t + step, 3)
        return self._shots


def analyze(frames: Iterator[tuple[float, float, object]], settings: dict, caption_region: dict | None = None,
            face_detector=None, progress: Stage | None = None) -> list[Shot]:
    """(time, duration, RGB frame) in order -> shots with per-frame measurements."""
    progress = progress or silent()
    analyzer = ColorAnalyzer(settings, caption_region, face_detector)
    for t, duration, image in frames:
        progress(t / duration if duration else 0.0)
        analyzer.feed(t, image)
    return analyzer.shots()


# --- judging shots -------------------------------------------------------------------------

def cast_name(a: float, b: float) -> str:
    """Words for a tint, from the Lab a* (magenta/green) and b* (yellow/blue) of neutral areas."""
    parts = []
    if abs(b) >= abs(a) * 0.5:
        parts.append("warm/yellow" if b > 0 else "cool/blue")
    if abs(a) >= abs(b) * 0.5:
        parts.append("magenta" if a > 0 else "green")
    return " and ".join(parts)


def _issue(shot: Shot, kind: str, severity: str, message: str, suggestion: str, confidence: float) -> Issue:
    return Issue(caption_id=shot_issue_id(shot.number), timestamp=shot.middle, type=kind, severity=severity,
                 original=f"Shot {shot.number}, {format_timestamp(shot.start)} - {format_timestamp(shot.end)}",
                 message=message, suggestion=suggestion, confidence=confidence)


def _within(value: float, bounds) -> bool:
    lo, hi = bounds
    return lo <= value <= hi


def shot_issues(shot: Shot, s: dict) -> list[Issue]:
    """Problems a shot has on its own."""
    if shot.is_black:
        return []
    out = []
    white = shot.value("clipped_white")
    if white is not None and white >= s.get("clipped_white", 0.04):
        out.append(_issue(shot, OVEREXPOSED, "high" if white >= s.get("overexposed_high_factor", 2.5) * s.get("clipped_white", 0.04) else "medium",
                          f"Overexposed: {white:.0%} of the picture is clipped pure white, so detail there is lost.",
                          "Bring the highlights / exposure down until whites keep their detail.", 0.8))
    luma, top = shot.value("luma_median"), shot.value("white_level")
    if luma is not None and luma < s.get("dark_median", 40) and (top or 0) < s.get("dark_white_level", 200):
        out.append(_issue(shot, UNDEREXPOSED, "medium",
                          f"Too dark: the middle brightness is {luma:.0f}/255 and even the brightest parts reach "
                          f"only {top:.0f}/255.", "Raise exposure / midtones.", 0.7))
    black = shot.value("clipped_black")
    if black is not None and black >= s.get("clipped_black", 0.15):
        out.append(_issue(shot, CRUSHED_BLACKS, "medium",
                          f"Crushed blacks: {black:.0%} of the picture is pure black, so shadow detail is lost.",
                          "Lift the shadows slightly.", 0.7))
    lifted, top = shot.value("black_level"), shot.value("white_level")
    exposure_flagged = any(i.type in (OVEREXPOSED, UNDEREXPOSED) for i in out)   # the bigger problem; said once
    if lifted is not None and top is not None and not exposure_flagged:
        if lifted >= s.get("lifted_black_level", 40):
            out.append(_issue(shot, FLAT_CONTRAST, "medium",
                              f"Washed-out, lifted blacks: the darkest parts are grey ({lifted:.0f}/255 instead of "
                              f"near 0), tonal range {top - lifted:.0f} of 255.",
                              "Set the black point: pull the blacks down and add contrast.", 0.6))
        elif top - lifted < s.get("min_tonal_range", 120) and _within(luma or 0, s.get("flat_luma_range", [60, 200])):
            out.append(_issue(shot, FLAT_CONTRAST, "low",
                              f"Low contrast: the tonal range is only {top - lifted:.0f} of 255.",
                              "Add contrast: spread the blacks and whites further apart.", 0.5))
    chroma = shot.value("chroma")
    if chroma is not None and chroma < s.get("dull_chroma", 6):
        out.append(_issue(shot, SATURATION, "low", f"Dull, almost grey colors (saturation {chroma:.0f}).",
                          "Add saturation / vibrance, unless the grey look is intended.", 0.5))
    elif chroma is not None and chroma > s.get("oversaturated_chroma", 40):
        out.append(_issue(shot, SATURATION, "medium", f"Oversaturated colors (saturation {chroma:.0f}).",
                          "Reduce saturation.", 0.6))
    ca, cb = shot.value("cast_a"), shot.value("cast_b")
    if ca is not None and cb is not None:
        strength = math.hypot(ca, cb)
        if strength >= s.get("cast_strength", 6):
            out.append(_issue(shot, COLOR_CAST, "medium" if strength >= s.get("cast_medium_factor", 1.6) * s.get("cast_strength", 6) else "low",
                              f"{cast_name(ca, cb).capitalize()} color cast: whites and greys are tinted "
                              f"(a* {ca:+.0f}, b* {cb:+.0f}; neutral is 0, 0).",
                              "White-balance on something that should be white or grey.", 0.6))
    hue, sc = shot.value("skin_hue"), shot.value("skin_chroma")
    if hue is not None and sc is not None:
        lo, hi = s.get("skin_hue_range", [20, 70])
        problem = None
        if hue < lo:
            problem = f"skin is too pink/magenta (hue {hue:.0f}°, natural skin is about {lo}-{hi}°)"
        elif hue > hi:
            problem = f"skin is too yellow/green (hue {hue:.0f}°, natural skin is about {lo}-{hi}°)"
        elif sc > s.get("skin_max_chroma", 38):
            problem = f"skin is too orange/saturated (chroma {sc:.0f})"
        elif sc < s.get("skin_min_chroma", 9):
            problem = f"skin is pale/grey (chroma {sc:.0f})"
        if problem:
            out.append(_issue(shot, SKIN_TONE, "medium", f"Skin tone: {problem}.",
                              "Correct skin toward the skin-tone line on the vectorscope.", 0.6))
    return out


def mismatch_issues(shots: Sequence[Shot], s: dict, flagged: dict[int, set[str]] | None = None) -> list[Issue]:
    """Shots whose grade doesn't match: skin tone jumping from the previous shot with a face, or a
    shot far from the rest of the video. `flagged` (shot number -> issue types already reported on
    it) keeps the same problem from being reported twice: a shot whose skin is already flagged isn't
    also a skin jump, and isn't the reference for the next shot (which would then "jump back")."""
    flagged = flagged or {}
    min_len = s.get("mismatch_min_shot_sec", 1.0)
    usable = [sh for sh in shots if sh.duration >= min_len and not sh.is_black]
    out = []

    prev_face = None
    for sh in usable:
        hue = sh.value("skin_hue")
        if hue is None or SKIN_TONE in flagged.get(sh.number, ()):
            continue
        if prev_face is not None:
            jump = abs(hue - prev_face.value("skin_hue"))
            if jump >= s.get("skin_hue_jump", 12):
                out.append(_issue(sh, SHOT_MISMATCH, "medium",
                                  f"Skin tone changes from the previous shot with a face (shot {prev_face.number}): "
                                  f"hue {prev_face.value('skin_hue'):.0f}° -> {hue:.0f}°. Often a camera that "
                                  f"wasn't matched.", f"Match this shot's skin tones to shot {prev_face.number}.", 0.6))
        prev_face = sh

    def typical(name):
        values = [sh.value(name) for sh in usable if sh.value(name) is not None]
        return median(values) if len(values) >= 3 else None

    ta, tb, tc = typical("cast_a"), typical("cast_b"), typical("chroma")
    for sh in usable:
        reasons = []
        ca, cb = sh.value("cast_a"), sh.value("cast_b")
        if ta is not None and tb is not None and ca is not None and cb is not None:
            diff = math.hypot(ca - ta, cb - tb)
            if diff >= s.get("mismatch_cast", 8) and COLOR_CAST not in flagged.get(sh.number, ()):
                reasons.append(f"its whites are tinted {cast_name(ca - ta, cb - tb)} compared with the rest")
        c = sh.value("chroma")
        if (tc is not None and c is not None and abs(c - tc) >= s.get("mismatch_chroma", 12)
                and SATURATION not in flagged.get(sh.number, ())):
            reasons.append(f"it is {'more' if c > tc else 'less'} saturated ({c:.0f} vs {tc:.0f} typical)")
        if reasons:
            out.append(_issue(sh, SHOT_MISMATCH, "medium",
                              "Grade doesn't match the rest of the video: " + "; ".join(reasons) + ".",
                              "Match this shot to the surrounding footage.", 0.55))
    return out


def color_issues(shots: Sequence[Shot], settings: dict) -> list[Issue]:
    if not settings.get("enabled", True):
        return []
    skip = set(settings.get("skip", []) or [])
    issues = [i for sh in shots for i in shot_issues(sh, settings)]
    flagged: dict[int, set[str]] = {}
    for i in issues:
        flagged.setdefault(shot_number(i.caption_id), set()).add(i.type)
    issues += mismatch_issues(shots, settings, flagged)
    return [i for i in issues if i.type not in skip]


# --- running it on a video, with a cache ---------------------------------------------------

def face_detector(models_dir, threshold: float = 0.8):
    """YuNet face detector (OpenCV), its model downloaded once into models/ (232 KB, checksum
    checked). models_dir may be several folders (the shared read-only models folder first): the
    first that has the model is used, else it's downloaded into the last. None if it can't be
    had: the skin checks are then skipped, nothing else."""
    import cv2

    if models_dir is None:
        return None
    dirs = [Path(models_dir)] if isinstance(models_dir, (str, Path)) else [Path(d) for d in models_dir]
    if not dirs:
        return None
    path = next((d / YUNET_FILE for d in dirs if (d / YUNET_FILE).is_file()), dirs[-1] / YUNET_FILE)
    try:
        if not path.is_file():
            log.info("downloading the face detection model (once, 232 KB)...")
            path.parent.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(YUNET_URL, timeout=60) as r:
                data = r.read()
            if hashlib.sha256(data).hexdigest() != YUNET_SHA256:
                raise OSError("the downloaded face model doesn't match its checksum")
            path.write_bytes(data)
        return cv2.FaceDetectorYN.create(str(path), "", (320, 320), threshold)
    except (OSError, cv2.error) as exc:
        log.warning("no face detector, so skin tones aren't checked: %s", exc)
        return None


HDR_TRANSFERS = {16: "PQ (HDR10)", 18: "HLG"}       # libav color_trc codes
BT2020_COLORSPACES = {9, 10}                         # libav colorspace codes: BT.2020 (non-constant / constant)


def hdr_transfer(video: Path) -> str | None:
    """"PQ (HDR10)", "HLG" or "BT.2020 wide gamut" for footage that isn't SDR Rec.709, None for SDR.
    Every color threshold assumes SDR Rec.709, so such footage isn't measured."""
    import av

    try:
        with av.open(str(video)) as container:
            if not container.streams.video:
                return None
            cc = container.streams.video[0].codec_context
            hdr = HDR_TRANSFERS.get(int(cc.color_trc or 0))
            if hdr is None and int(cc.colorspace or 0) in BT2020_COLORSPACES:
                hdr = "BT.2020 wide gamut"
            return hdr
    except (av.error.FFmpegError, ValueError, TypeError):
        return None


def color_cache_file(video: Path, settings: dict, caption_region: dict | None, cache_dir: Path) -> Path:
    key_settings = {k: settings.get(k) for k in ("sample_fps", "cut_threshold", "min_shot_sec", "face_every_n_frames",
                                                  "measure_every_n_frames")}
    if float(settings.get("face_min_score", 0.8)) != 0.8:      # only when changed, so existing caches stay valid
        key_settings["face_min_score"] = float(settings["face_min_score"])
    key = text_hash(COLOR_VERSION, json.dumps(key_settings, sort_keys=True), json.dumps(caption_region, sort_keys=True))[:10]
    return Path(cache_dir) / f"{file_sha256(video)[:16]}-{key}.json"


def load_shots(cache_file: Path) -> list[Shot] | None:
    """The shots in a color cache file; None if it's missing or unreadable."""
    if not cache_file.is_file():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        return [Shot(s["number"], s["start"], s["end"], [FrameStats(**f) for f in s["frames"]]) for s in data["shots"]]
    except (OSError, ValueError, KeyError, TypeError):
        log.warning("cached color analysis %s is unreadable; analysing again", cache_file.name)
        return None


def save_shots(cache_file: Path, video: Path, shots: Sequence[Shot]) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps({"video": video.name, "shots": [
        {"number": s.number, "start": s.start, "end": s.end, "frames": [asdict(f) for f in s.frames]}
        for s in shots]}), encoding="utf-8")


def read_color(video: Path, settings: dict, caption_region: dict | None = None, cache_dir: Path | None = None,
               models_dir: Path | None = None, progress: Stage | None = None) -> list[Shot]:
    from src.inputs.video_ocr import sample_frames

    video = Path(video)
    if not video.is_file():
        raise FileNotFoundError(f"video not found: {video}")
    cache_file = color_cache_file(video, settings, caption_region, cache_dir) if cache_dir is not None else None
    if cache_file is not None:
        shots = load_shots(cache_file)
        if shots is not None:
            log.info("color: from cache (%s)", cache_file.name)
            return shots

    log.info("measuring the color of %s...", video.name)
    started = time.monotonic()
    shots = analyze(sample_frames(video, float(settings.get("sample_fps", 4))), settings, caption_region,
                    face_detector(models_dir, float(settings.get("face_min_score", 0.8)))
                    if settings.get("skin_tones", True) else None, progress)
    log.info("color: %d shots measured in %.0f s", len(shots), time.monotonic() - started)
    if cache_file is not None:
        save_shots(cache_file, video, shots)
    return shots
