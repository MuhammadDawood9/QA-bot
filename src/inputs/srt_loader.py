"""Read an .srt file into a list of Captions."""

from __future__ import annotations

import re
import warnings
from pathlib import Path

from src.models import Caption

_TIME_LINE = re.compile(
    r"(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})\s*-->\s*(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})"
)
# Formatting that isn't part of the spoken text: <i>, </b>, <font ...>, {\an8}
_TAGS = re.compile(r"<[^>]+>|\{\\[^}]*\}")


class SrtError(ValueError):
    pass


def _to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_srt(content: str, source: str = "srt") -> list[Caption]:
    """Parse SRT text. Malformed blocks are skipped with a warning, not fatal."""
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", content.strip())
    captions: list[Caption] = []

    for block_no, block in enumerate(blocks, start=1):
        lines = [line.rstrip() for line in block.split("\n")]
        time_idx = next((i for i, line in enumerate(lines) if _TIME_LINE.search(line)), None)
        if time_idx is None:
            if block.strip():
                warnings.warn(f"SRT block {block_no} has no timing line; skipped.", stacklevel=2)
            continue

        index_text = lines[time_idx - 1].strip() if time_idx > 0 else ""
        cap_id = int(index_text) if index_text.isdigit() else len(captions) + 1

        m = _TIME_LINE.search(lines[time_idx])
        start = _to_seconds(*m.group(1, 2, 3, 4))
        end = _to_seconds(*m.group(5, 6, 7, 8))
        if end < start:
            warnings.warn(f"SRT caption {cap_id} ends before it starts; skipped.", stacklevel=2)
            continue

        text_lines = [_TAGS.sub("", line).strip() for line in lines[time_idx + 1 :]]
        text = "\n".join(line for line in text_lines if line)
        captions.append(Caption(id=cap_id, start=start, end=end, text=text, source=source))

    captions.sort(key=lambda c: (c.start, c.id))
    return captions


def load_srt(path: Path) -> list[Caption]:
    path = Path(path)
    if not path.is_file():
        raise SrtError(f"SRT file not found: {path}")
    # utf-8-sig is UTF-8 that also drops the byte-order mark some editors write at the start.
    with path.open(encoding="utf-8-sig") as f:
        return parse_srt(f.read())


def _srt_time(seconds: float) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_srt(captions: list[Caption]) -> str:
    blocks = [f"{i}\n{_srt_time(c.start)} --> {_srt_time(c.end)}\n{c.text}"
              for i, c in enumerate(captions, start=1)]
    return "\n\n".join(blocks) + "\n" if blocks else ""


def write_srt(captions: list[Caption], path: Path) -> Path:
    """Save captions (e.g. the ones read from a video) as an .srt, to look at or reuse with --srt."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(format_srt(captions), encoding="utf-8")
    return path
