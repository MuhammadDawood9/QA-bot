"""What the exported file is (size, aspect, frame rate, length, bitrate, audio format) against what
the client ordered (delivery.spec in config.yaml or the client file). Read with PyAV, no decoding.

    info = probe(video)                          # MediaInfo
    issues = spec_issues(info, settings["spec"])
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.models import DELIVERY_SPEC, Issue


@dataclass(frozen=True)
class MediaInfo:
    duration: float | None = None          # seconds
    width: int | None = None               # as shown: rotation applied
    height: int | None = None
    pixel_aspect: float = 1.0              # sample aspect ratio (1.0 = square pixels)
    fps: float | None = None
    video_kbps: float | None = None
    sample_rate: int | None = None
    channels: int | None = None

    @property
    def has_picture(self) -> bool:
        return bool(self.width and self.height)

    @property
    def aspect(self) -> float | None:
        """Width / height of the picture as shown."""
        return self.width * self.pixel_aspect / self.height if self.has_picture else None

    @property
    def vertical(self) -> bool:
        return self.has_picture and self.aspect < 1.0

    def describe(self) -> str:
        parts = []
        if self.has_picture:
            parts.append(f"{self.width}x{self.height}")
            if self.fps:
                parts.append(f"{self.fps:.3g} fps")
        if self.duration is not None:
            parts.append(f"{self.duration:.1f} s")
        if self.sample_rate:
            parts.append(f"{self.sample_rate} Hz {'mono' if self.channels == 1 else f'{self.channels} ch'}")
        return ", ".join(parts)


def probe(path: Path) -> MediaInfo:
    """The file's properties from its headers. Missing values stay None. Raises OSError if the
    file can't be opened."""
    import av

    try:
        container = av.open(str(path))
    except av.error.FFmpegError as exc:
        raise OSError(f"could not open {Path(path).name}: {exc}") from None
    with container:
        duration = container.duration / 1_000_000 if container.duration else None
        width = height = fps = kbps = None
        pixel_aspect = 1.0
        if container.streams.video:
            v = container.streams.video[0]
            cc = v.codec_context
            width, height = cc.width or None, cc.height or None
            sar = cc.sample_aspect_ratio
            if sar and sar.numerator and sar.denominator:
                pixel_aspect = float(sar)
            if _rotation(v) in (90, 270) and width and height:
                width, height, pixel_aspect = height, width, 1.0 / pixel_aspect
            rate = v.average_rate or v.base_rate
            fps = float(rate) if rate else None
            bit_rate = v.bit_rate or cc.bit_rate
            if not bit_rate and container.bit_rate and not container.streams.audio:
                bit_rate = container.bit_rate
            kbps = bit_rate / 1000 if bit_rate else None
            if duration is None and v.duration:
                duration = float(v.duration * v.time_base)
        sample_rate = channels = None
        if container.streams.audio:
            a = container.streams.audio[0].codec_context
            sample_rate = a.sample_rate or None
            channels = a.channels or None
            if duration is None and container.streams.audio[0].duration:
                s = container.streams.audio[0]
                duration = float(s.duration * s.time_base)
    return MediaInfo(duration, width, height, pixel_aspect, fps, kbps, sample_rate, channels)


def _rotation(stream) -> int:
    """The display rotation phones write into the file (0, 90, 180, 270)."""
    try:
        value = stream.metadata.get("rotate") or 0
        if not value and stream.side_data:
            matrix = stream.side_data.get("DISPLAYMATRIX")
            value = matrix if isinstance(matrix, (int, float)) else 0
        return int(round(float(value))) % 360
    except (AttributeError, TypeError, ValueError):
        return 0


def parse_aspect(value) -> float | None:
    """"9:16", "16/9", 0.5625 or None -> width / height."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("/", ":").replace("x", ":")
    if ":" in text:
        w, h = text.split(":", 1)
        return float(w) / float(h)
    return float(text)


def _issue(original: str, message: str, suggestion: str, severity: str = "high") -> Issue:
    return Issue(0, 0.0, DELIVERY_SPEC, severity, original, message, suggestion)


def spec_issues(info: MediaInfo, spec: dict) -> list[Issue]:
    """One issue per way the file differs from the spec. Unset values aren't checked."""
    spec = spec or {}
    out: list[Issue] = []
    shown = info.describe()

    width, height = spec.get("width"), spec.get("height")
    if info.has_picture and (width or height):
        if (width and info.width != int(width)) or (height and info.height != int(height)):
            want = f"{width or '?'}x{height or '?'}"
            out.append(_issue(shown, f"Resolution is {info.width}x{info.height}; the client ordered {want}.",
                              f"Export at {want}."))

    aspect = parse_aspect(spec.get("aspect"))
    if aspect and info.has_picture:
        tolerance = float(spec.get("aspect_tolerance", 0.01))
        if abs(info.aspect - aspect) / aspect > tolerance:
            out.append(_issue(shown, f"Picture is {_ratio(info.aspect)} ({info.aspect:.3f}); the client ordered "
                                     f"{spec.get('aspect')} ({aspect:.3f}).",
                              f"Reframe or export as {spec.get('aspect')}."))

    rates = spec.get("fps")
    if rates not in (None, "", []) and info.fps:
        rates = [float(r) for r in (rates if isinstance(rates, (list, tuple)) else [rates])]
        tolerance = float(spec.get("fps_tolerance", 0.01))
        if not any(abs(info.fps - r) / r <= tolerance for r in rates):
            allowed = " or ".join(f"{r:g}" for r in rates)
            out.append(_issue(shown, f"Frame rate is {info.fps:.3f} fps; the client ordered {allowed}.",
                              f"Export at {allowed} fps (and check the timeline's frame rate)."))

    lo, hi = spec.get("min_duration_sec"), spec.get("max_duration_sec")
    if info.duration is not None:
        if hi is not None and info.duration > float(hi):
            out.append(_issue(shown, f"Runs {info.duration:.1f} s; the client allows at most {float(hi):g} s.",
                              f"Trim it to {float(hi):g} s or less."))
        if lo is not None and info.duration < float(lo):
            out.append(_issue(shown, f"Runs {info.duration:.1f} s; the client wants at least {float(lo):g} s.",
                              "Check the cut is complete."))

    kbps = spec.get("min_video_kbps")
    if kbps and info.video_kbps is not None and info.video_kbps < float(kbps):
        out.append(_issue(shown, f"Video bitrate is {info.video_kbps:.0f} kbit/s, under the {float(kbps):g} kbit/s "
                                 f"wanted: the picture may look blocky.",
                          "Export at a higher bitrate or quality setting.", "medium"))

    rate = spec.get("audio_sample_rate")
    if rate and info.sample_rate and info.sample_rate != int(rate):
        out.append(_issue(shown, f"Audio is {info.sample_rate} Hz; the client ordered {int(rate)} Hz.",
                          f"Export the audio at {int(rate)} Hz.", "medium"))
    channels = spec.get("audio_channels")
    if channels and info.channels and info.channels != int(channels):
        names = {1: "mono", 2: "stereo"}
        out.append(_issue(shown, f"Audio is {names.get(info.channels, f'{info.channels} channels')}; the client "
                                 f"ordered {names.get(int(channels), f'{int(channels)} channels')}.",
                          f"Export {names.get(int(channels), f'{int(channels)} channels')} audio.", "medium"))
    if spec.get("audio_sample_rate") or spec.get("audio_channels"):
        if info.sample_rate is None and info.has_picture:
            out.append(_issue(shown, "The file has no audio track.", "Export with the audio."))
    return out


def _ratio(aspect: float) -> str:
    """The nearest common ratio name, for messages."""
    common = {"9:16": 9 / 16, "16:9": 16 / 9, "1:1": 1.0, "4:5": 0.8, "4:3": 4 / 3, "21:9": 21 / 9}
    name, value = min(common.items(), key=lambda kv: abs(kv[1] - aspect))
    return name if abs(value - aspect) / value < 0.01 else f"{aspect:.3f}:1"
