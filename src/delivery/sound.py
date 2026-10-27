"""The exported audio: loudness against the platform's target, clipping, and a voice buried under
the music.

    m = measure_loudness(path, ffmpeg)                 # {"integrated": -15.2, "true_peak": -0.4, "lra": 6.1}
    issues = loudness_issues(m, settings["loudness"])
    times = scan_clipping(path, ffmpeg, info, settings["clipping"])     # seconds of each clip
    issues += clipping_issues(times, settings["clipping"])
    issues += voice_under_music(samples, sr, words, settings["voice_under_music"])
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Sequence

from src.models import CLIPPING, LOUDNESS, TRUE_PEAK, VOICE_UNDER_MUSIC, Issue
from src.utils.system import run_ffmpeg, stream_ffmpeg
from src.utils.timecode import format_timestamp

_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)


# --- loudness ------------------------------------------------------------------------------------

def measure_loudness(path: Path, ffmpeg: str) -> dict | None:
    """Integrated loudness (LUFS), true peak (dBTP) and loudness range (LU) of the whole file, from
    ffmpeg's loudnorm analysis. None when the file has no audio ffmpeg can measure."""
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-i", str(path), "-vn",
           "-af", "loudnorm=print_format=json", "-f", "null", "-"]
    result = run_ffmpeg(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg could not measure the loudness of {Path(path).name}: "
                           f"{(result.stderr or '').strip()[-300:]}")
    return parse_loudnorm(result.stderr or "")


def parse_loudnorm(text: str) -> dict | None:
    """The numbers from loudnorm's JSON block (the last one in ffmpeg's output)."""
    found = _JSON.findall(text)
    if not found:
        return None
    try:
        data = json.loads(found[-1])
        return {"integrated": float(data["input_i"]), "true_peak": float(data["input_tp"]),
                "lra": float(data["input_lra"])}
    except (ValueError, KeyError, TypeError):
        return None


def loudness_issues(measured: dict | None, settings: dict) -> list[Issue]:
    if not measured:
        return []
    out = []
    integrated, peak = measured["integrated"], measured["true_peak"]
    shown = f"{integrated:.1f} LUFS, true peak {peak:.1f} dBTP"
    target = settings.get("target_lufs")
    if target is not None and math.isfinite(integrated):
        tolerance = float(settings.get("tolerance_lu", 2.0))
        off = integrated - float(target)
        if abs(off) > tolerance:
            way = "too loud" if off > 0 else "too quiet"
            out.append(Issue(0, 0.0, LOUDNESS, "medium", shown,
                             f"Loudness is {integrated:.1f} LUFS, {abs(off):.1f} LU {way} for the {float(target):g} LUFS "
                             f"target (±{tolerance:g}). The platform will turn it {'down' if off > 0 else 'up'}, "
                             f"or it will sound {'louder' if off > 0 else 'quieter'} than everything around it.",
                             f"{'Lower' if off > 0 else 'Raise'} the mix by about {abs(off):.0f} dB, or normalize to "
                             f"{float(target):g} LUFS integrated."))
    max_peak = settings.get("max_true_peak_dbtp")
    if max_peak is not None and math.isfinite(peak) and peak > float(max_peak):
        out.append(Issue(0, 0.0, TRUE_PEAK, "medium", shown,
                         f"True peak reaches {peak:.1f} dBTP, above {float(max_peak):g} dBTP: it can distort once the "
                         f"platform re-encodes the audio.",
                         f"Put a true-peak limiter at {float(max_peak):g} dBTP on the master."))
    return out


# --- clipping ------------------------------------------------------------------------------------

class ClipScanner:
    """Finds clips (min_run samples in a row at or above `level` in one channel) in audio fed one
    block at a time, runs across block edges included. clips: the start time of each, in seconds."""

    def __init__(self, sample_rate: int, channels: int, level: float, min_run: int):
        self.sr, self.channels, self.level, self.min_run = sample_rate, channels, level, max(1, int(min_run))
        self.offset = 0                                      # samples fed so far, per channel
        self.carry = [(0, 0)] * channels                     # (length, start) of a run reaching the block end
        self.clips: list[float] = []

    def feed(self, block) -> None:
        import numpy as np

        block = np.asarray(block, dtype=np.float32).reshape(-1, self.channels)
        n = len(block)
        mask = np.abs(block) >= self.level
        for c in range(self.channels):
            m = mask[:, c]
            carry_len, carry_start = self.carry[c]
            if carry_len and (n == 0 or not m[0]):
                self._end(carry_len, carry_start)
                carry_len = 0
            edges = np.diff(np.concatenate(([0], m.astype(np.int8), [0])))
            new_carry = (0, 0)
            for s, e in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)):
                length, start = int(e - s), self.offset + int(s)
                if s == 0 and carry_len:
                    length, start = length + carry_len, carry_start
                if e == n:
                    new_carry = (length, start)
                else:
                    self._end(length, start)
            self.carry[c] = new_carry
        self.offset += n

    def finish(self) -> list[float]:
        for length, start in self.carry:
            if length:
                self._end(length, start)
        self.carry = [(0, 0)] * self.channels
        return sorted(self.clips)

    def _end(self, length: int, start: int) -> None:
        if length >= self.min_run:
            self.clips.append(start / self.sr)


def scan_clipping(path: Path, ffmpeg: str, sample_rate: int, channels: int, settings: dict) -> list[float]:
    """Every clip in the file at its own sample rate and channels (resampling or mixing would hide
    them), streamed a second at a time."""
    import numpy as np

    scanner = ClipScanner(sample_rate, channels, float(settings.get("level", 0.999)),
                          int(settings.get("min_run_samples", 3)))
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn",
           "-f", "f32le", "-acodec", "pcm_f32le", "-"]
    frame_bytes = 4 * channels
    leftover = b""
    for chunk in stream_ffmpeg(cmd, sample_rate * frame_bytes):
        data = leftover + chunk
        whole = len(data) // frame_bytes * frame_bytes
        leftover = data[whole:]
        scanner.feed(np.frombuffer(data[:whole], dtype="<f4"))
    return scanner.finish()


def clipping_issues(times: Sequence[float], settings: dict) -> list[Issue]:
    """Clips close together become one flag; the regions with the most clips are flagged."""
    if not times:
        return []
    gap = float(settings.get("merge_gap_sec", 1.0))
    regions: list[list[float]] = [[times[0]]]
    for t in times[1:]:
        if t - regions[-1][-1] <= gap:
            regions[-1].append(t)
        else:
            regions.append([t])
    limit = int(settings.get("max_flags", 10))
    worst = sorted(regions, key=lambda r: -len(r))[:limit] if limit > 0 else regions
    out = []
    for r in sorted(worst, key=lambda r: r[0]):
        span = f"{format_timestamp(r[0])}" + (f" - {format_timestamp(r[-1])}" if r[-1] > r[0] else "")
        out.append(Issue(0, r[0], CLIPPING, "high" if len(r) >= 3 else "medium", span,
                         f"Audio clips {len(r)} time{'s' if len(r) != 1 else ''} here (samples at full scale): "
                         f"it will sound distorted.",
                         "Lower the gain before this point or put a limiter on the master, then re-export."))
    return out


# --- voice under music ---------------------------------------------------------------------------

def voice_under_music(samples, sample_rate: int, words: Sequence, settings: dict) -> list[Issue]:
    """Windows where the voice is barely louder than what plays in the pauses between its words
    (music, effects): the speech level (voice + music) against the pause level (music alone).
    words: SpokenWords (start, end). Pauses quieter than quiet_background_db are skipped: nothing
    is there to bury the voice."""
    import numpy as np

    if samples is None or len(samples) == 0 or not words:
        return []
    window = float(settings.get("window_sec", 5.0))
    min_speech = float(settings.get("min_speech_sec", 1.0))
    min_gap = float(settings.get("min_gap_sec", 0.25))
    margin = float(settings.get("gap_margin_sec", 0.05))
    min_background = float(settings.get("min_background_sec", 0.4))
    min_db = float(settings.get("min_voice_over_background_db", 6.0))
    quiet_db = float(settings.get("quiet_background_db", -45.0))

    x = np.asarray(samples, dtype=np.float64)
    energy = np.concatenate(([0.0], np.cumsum(x * x)))
    duration = len(x) / sample_rate
    spoken = sorted(((float(w.start), float(w.end)) for w in words if w.end > w.start), key=lambda s: s[0])
    pauses = [(a_end + margin, b_start - margin) for (_, a_end), (b_start, _) in zip(spoken, spoken[1:])
              if b_start - a_end >= min_gap and b_start - a_end > 2 * margin]

    def inside(spans, lo, hi):
        return [(max(a, lo), min(b, hi)) for a, b in spans if b > lo and a < hi]

    def level(spans):
        total, count = 0.0, 0
        for a, b in spans:
            i, j = int(a * sample_rate), int(b * sample_rate)
            i, j = max(0, min(i, len(x))), max(0, min(j, len(x)))
            total, count = total + energy[j] - energy[i], count + (j - i)
        return 10 * math.log10(total / count + 1e-12) if count else None

    flagged = []
    start = 0.0
    while start < duration:
        end = min(start + window, duration)
        speech, background = inside(spoken, start, end), inside(pauses, start, end)
        if (sum(b - a for a, b in speech) >= min_speech and sum(b - a for a, b in background) >= min_background):
            speech_db, background_db = level(speech), level(background)
            if speech_db is not None and background_db is not None and background_db > quiet_db \
                    and speech_db - background_db < min_db:
                flagged.append((start, end, speech_db - background_db, background_db))
        start += window

    merge = float(settings.get("merge_gap_sec", 2.0))
    regions: list[list[tuple]] = []
    for f in flagged:
        if regions and f[0] - regions[-1][-1][1] <= merge:
            regions[-1].append(f)
        else:
            regions.append([f])
    out = []
    for r in regions:
        worst = min(r, key=lambda f: f[2])
        span = f"{format_timestamp(r[0][0])} - {format_timestamp(r[-1][1])}"
        out.append(Issue(0, r[0][0], VOICE_UNDER_MUSIC, "medium", span,
                         f"The voice is only {worst[2]:.1f} dB louder than the sound in its pauses "
                         f"({worst[3]:.0f} dBFS; at least {min_db:g} dB wanted): music or effects may bury it.",
                         "Duck the music under the voice here, or raise the voice."))
    return out
