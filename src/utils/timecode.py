"""Parse the timestamps editors and reviewers actually write, and return seconds.

Supported:
    HH:MM:SS:FF     non-drop-frame timecode ("00:00:14:32")
    HH;MM;SS;FF     drop-frame timecode at 29.97 or 59.94 ("00;00;15;07")
    H:MM:SS         ("00:02:12", decimals allowed in the seconds field)
    M:SS / MM:SS    ("0:05", "13:06", "42: 34")
    ranges          "a - b", "a–b", "a — b", "a to b", shorthand "13:20-21", and "2:45 onwards"
"""

from __future__ import annotations

import re

_SEP_SPACES = re.compile(r"\s*([:;])\s*")
_TIMESTAMP = re.compile(r"\d+(?:[:;]\d+)*(?:\.\d+)?")
_RANGE_SPLIT = re.compile(r"\s*(?:\bto\b|[-–—])\s*")
_ONWARDS = re.compile(r"^(.*?)\s*(?:onwards?|\+)$")
_FOUR_PART = re.compile(r"\d+[:;]\d+[:;]\d+[:;](\d+)")


class TimecodeError(ValueError):
    pass


def _nominal_rate(fps: float) -> int:
    nominal = round(fps)
    if nominal <= 0:
        raise TimecodeError(f"fps must be positive, got {fps}")
    return nominal


def _check_minutes_seconds(text: str, minutes: int, seconds: float) -> None:
    if minutes >= 60:
        raise TimecodeError(f"{text!r}: minutes must be under 60")
    if seconds >= 60:
        raise TimecodeError(f"{text!r}: seconds must be under 60")


def _drop_frame_to_seconds(text: str, h: int, m: int, s: int, f: int, nominal: int) -> float:
    if nominal not in (30, 60):
        raise TimecodeError(f"{text!r}: drop-frame timecode needs fps 30 (29.97) or 60 (59.94), got {nominal}")
    drop = 2 if nominal == 30 else 4
    if s == 0 and f < drop and m % 10 != 0:
        raise TimecodeError(f"{text!r}: frame label does not exist in drop-frame timecode")
    total_minutes = 60 * h + m
    frame_number = (
        nominal * 3600 * h + nominal * 60 * m + nominal * s + f
        - drop * (total_minutes - total_minutes // 10)
    )
    return frame_number * 1001 / (nominal * 1000)


def parse_timestamp(text: str, fps: float = 30) -> float:
    """Convert one timestamp to seconds. Raises TimecodeError if it can't be read."""
    if not isinstance(text, str):
        raise TimecodeError(f"expected a string, got {type(text).__name__}")
    cleaned = _SEP_SPACES.sub(r"\1", text.strip())
    if not _TIMESTAMP.fullmatch(cleaned):
        raise TimecodeError(f"{text!r} is not a timestamp")

    drop_frame = ";" in cleaned
    parts = re.split(r"[:;]", cleaned)

    if len(parts) == 4:
        if "." in cleaned:
            raise TimecodeError(f"{text!r}: frame timecode can't have decimals")
        h, m, s, f = (int(p) for p in parts)
        _check_minutes_seconds(text, m, s)
        nominal = _nominal_rate(fps)
        if f >= nominal:
            raise TimecodeError(f"{text!r}: frame {f} is not valid at {fps} fps")
        if drop_frame:
            return _drop_frame_to_seconds(text, h, m, s, f, nominal)
        return h * 3600 + m * 60 + s + f / nominal

    if drop_frame:
        raise TimecodeError(f"{text!r}: ';' is only valid in HH;MM;SS;FF timecode")

    if len(parts) == 3:
        h, m, s = int(parts[0]), int(parts[1]), float(parts[2])
        _check_minutes_seconds(text, m, s)
        return h * 3600 + m * 60 + s

    if len(parts) == 2:
        m, s = int(parts[0]), float(parts[1])
        if s >= 60:
            raise TimecodeError(f"{text!r}: seconds must be under 60")
        return m * 60 + s

    # A bare number ("35") is ambiguous: seconds or minutes? Refuse rather than guess.
    raise TimecodeError(f"{text!r} is ambiguous; write it as M:SS or HH:MM:SS:FF")


def parse_range(text: str, fps: float = 30) -> tuple[float, float | None]:
    """Parse a single timestamp or a range.

    Returns (start, end). A single timestamp gives (t, t); "2:45 onwards" gives (t, None).
    """
    cleaned = text.strip()
    onwards = _ONWARDS.match(cleaned)
    if onwards:
        return parse_timestamp(onwards.group(1), fps), None

    pieces = _RANGE_SPLIT.split(cleaned)
    if len(pieces) == 1:
        t = parse_timestamp(cleaned, fps)
        return t, t
    if len(pieces) != 2 or not all(pieces):
        raise TimecodeError(f"{text!r} is not a timestamp range")

    start_text, end_text = (_SEP_SPACES.sub(r"\1", p) for p in pieces)
    start_fields = re.split(r"([:;])", start_text)
    end_fields = re.split(r"([:;])", end_text)
    if len(end_fields) < len(start_fields) and len(start_fields) > 1:
        # Shorthand like "13:20-21": the end borrows the leading fields of the start.
        end_text = "".join(start_fields[: len(start_fields) - len(end_fields)] + end_fields)

    start = parse_timestamp(start_text, fps)
    end = parse_timestamp(end_text, fps)
    if end < start:
        raise TimecodeError(f"{text!r}: range ends before it starts")
    return start, end


def infer_fps(text: str, default: float = 30) -> float:
    """Guess the timeline frame rate from a written timecode.

    Reviewers' notes mix 30 and 60 fps timelines. A frame field of 30 or more can only be 60 fps.
    Only meant for human-written notes; real videos should use their actual frame rate.
    """
    frames = [int(f) for f in _FOUR_PART.findall(text)]
    if frames and max(frames) >= _nominal_rate(default):
        return 60
    return default


def format_timestamp(seconds: float) -> str:
    """Seconds -> "MM:SS.mmm", or "H:MM:SS.mmm" past one hour."""
    total_ms = int(round(seconds * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}.{ms:03d}"
    return f"{minutes:02d}:{secs:02d}.{ms:03d}"
