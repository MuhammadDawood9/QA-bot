"""Turn OCR readings of sampled frames into captions with start and end times.

Pure logic, no video or OCR engine here, so it can be tested with made-up readings.

1. The text boxes found in one frame are put into lines (boxes side by side: a highlighted key
   word in another color often comes back as its own box), and lines into blocks (lines stacked
   close together). One block is one piece of on-screen text: the caption, or a title elsewhere.
2. Blocks are followed from frame to frame. A block continues an open caption when its text is
   the same (allowing for small OCR differences) or when it grows from it: word-by-word and
   fade-in captions show "We" -> "We need" -> "We need to talk" and that is one caption.
   Text that changes in any other way starts a new caption.
3. When a caption ends, its text is the reading most frames agree on, from the frames that show
   it complete. How much they agree becomes the caption's OCR confidence, so later a spelling flag
   on a caption the OCR wasn't sure of can be marked as possibly the OCR's mistake.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from statistics import mean, median
from typing import Iterable, Sequence

from rapidfuzz import fuzz

from src.models import Caption

_NOT_WORD = re.compile(r"[^\w\s]")


def _letters(text: str) -> str:
    """The text without its spaces and line breaks (which the OCR may drop)."""
    return "".join(text.split())


@dataclass(frozen=True)
class TextBox:
    """One piece of text the OCR found. Positions are fractions of the full frame (0..1)."""
    text: str
    score: float
    left: float
    top: float
    right: float
    bottom: float

    @property
    def height(self) -> float:
        return self.bottom - self.top

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass(frozen=True)
class Reading:
    """One block of text in one frame: lines joined with "\\n", and the OCR's mean score."""
    text: str
    score: float
    top: float
    bottom: float
    left: float | None = None       # None: not known (the safe-area check then skips the caption)
    right: float | None = None

    @property
    def flat(self) -> str:
        return " ".join(self.text.split())

    @property
    def word_count(self) -> int:
        return len(self.text.split())


def normalize(text: str) -> str:
    """For comparing readings: case, punctuation and spacing don't decide if it's the same caption."""
    return " ".join(_NOT_WORD.sub("", text.lower()).split())


def _lines(boxes: Sequence[TextBox]) -> list[list[TextBox]]:
    """Boxes whose vertical middles are within half a box height of each other share a line."""
    lines: list[list[TextBox]] = []
    for box in sorted(boxes, key=lambda b: b.center_y):
        for line in lines:
            ref = line[0]
            if abs(box.center_y - ref.center_y) <= 0.5 * max(box.height, ref.height):
                line.append(box)
                break
        else:
            lines.append([box])
    return [sorted(line, key=lambda b: b.left) for line in lines]


def group_boxes(boxes: Iterable[TextBox], block_gap_lines: float = 1.5) -> list[Reading]:
    """Boxes of one frame -> one Reading per block of text, top to bottom."""
    lines = _lines([b for b in boxes if b.text.strip()])
    blocks: list[list[list[TextBox]]] = []
    for line in lines:                                    # already top to bottom
        top = min(b.top for b in line)
        if blocks:
            prev = blocks[-1][-1]
            prev_bottom = max(b.bottom for b in prev)
            line_height = median(b.height for b in prev + line)
            overlaps = (min(b.left for b in line) < max(b.right for b in prev)
                        and max(b.right for b in line) > min(b.left for b in prev))
            if overlaps and top - prev_bottom <= block_gap_lines * line_height:
                blocks[-1].append(line)
                continue
        blocks.append([line])

    readings = []
    for block in blocks:
        boxes_in = [b for line in block for b in line]
        text = "\n".join(" ".join(b.text.strip() for b in line) for line in block)
        weights = [max(1, len(b.text)) for b in boxes_in]
        score = sum(b.score * w for b, w in zip(boxes_in, weights)) / sum(weights)
        readings.append(Reading(text=text, score=score, top=min(b.top for b in boxes_in),
                                bottom=max(b.bottom for b in boxes_in), left=min(b.left for b in boxes_in),
                                right=max(b.right for b in boxes_in)))
    return readings


@dataclass
class _Track:
    first: float
    last_seen: float
    readings: list[Reading] = field(default_factory=list)

    @property
    def latest(self) -> Reading:
        return self.readings[-1]


def continues(track_text: str, new_text: str, same_ratio: float) -> float:
    """How well new_text continues a caption whose latest reading is track_text (0 = it doesn't).

    Same text (allowing OCR noise) or the same text with words added at the end (word-by-word
    animation). Text that only shrinks does not continue: the next caption can start with the
    same word the last one did, and fading captions are dropped by their low OCR score anyway."""
    # Spaces are left out of the comparison: the OCR sometimes drops one ("It was" -> "Itwas").
    a, b = normalize(track_text).replace(" ", ""), normalize(new_text).replace(" ", "")
    if not a or not b:
        return 0.0
    same = fuzz.ratio(a, b)
    if same >= same_ratio:
        return same
    if len(b) > len(a) and fuzz.ratio(a, b[:len(a)]) >= same_ratio:
        return same_ratio - 1 + fuzz.ratio(a, b[:len(a)]) / 100       # a match, ranked below "same"
    return 0.0


def final_text(readings: Sequence[Reading]) -> tuple[str, float]:
    """The caption's text and the OCR's confidence in it.

    Only complete readings count (the earlier ones of a word-by-word caption are shorter). Length
    is counted in letters, not words: the OCR often drops the spaces between words, so
    "MarkMcGwirehit" is as complete as "Mark McGwire hit" though it reads as one word. A length
    seen in only one frame loses to one seen in several, so a single noisy frame with a stray
    extra letter doesn't decide the caption. Among the complete readings the most common text
    (spaces aside) wins, ties going to the higher OCR score; of its readings, the one with the
    most word breaks gives the text, since the OCR drops spaces far more often than it adds them.
    Confidence is that text's share of the complete readings times its mean OCR score."""
    counts = Counter(len(_letters(r.flat)) for r in readings)
    seen_twice = [n for n, c in counts.items() if c >= 2]
    target = max(seen_twice) if seen_twice else max(counts)
    full = [r for r in readings if len(_letters(r.flat)) == target]

    votes: dict[str, list[Reading]] = {}
    for r in full:
        votes.setdefault(_letters(r.flat), []).append(r)
    winner = max(votes.values(), key=lambda rs: (len(rs), mean(r.score for r in rs)))
    best = max(winner, key=lambda r: (r.word_count, r.score))     # keeps that reading's line breaks
    confidence = len(winner) / len(full) * mean(r.score for r in winner)
    return best.text, round(min(1.0, max(0.0, confidence)), 3)


def build_captions(frames: Iterable[tuple[float, Sequence[Reading]]], frame_interval: float,
                   settings: dict | None = None) -> list[Caption]:
    """(time, readings in that frame) in time order -> Captions, numbered 1, 2, 3... by start time."""
    settings = settings or {}
    same_ratio = float(settings.get("same_text_ratio", 85))
    max_gap = float(settings.get("max_gap_sec", 0.5))
    single_frame_score = float(settings.get("min_single_frame_score", 0.8))

    open_tracks: list[_Track] = []
    done: list[_Track] = []
    for t, readings in frames:
        unmatched = list(readings)
        # Best matches first, so two captions on screen at once each keep their own track.
        pairs = sorted(((continues(tr.latest.text, r.text, same_ratio), i, j)
                        for i, tr in enumerate(open_tracks) for j, r in enumerate(readings)),
                       reverse=True)
        used_tracks, used_readings = set(), set()
        for score, i, j in pairs:
            if score <= 0 or i in used_tracks or j in used_readings:
                continue
            open_tracks[i].readings.append(readings[j])
            open_tracks[i].last_seen = t
            used_tracks.add(i)
            used_readings.add(j)
        unmatched = [r for j, r in enumerate(readings) if j not in used_readings]

        still_open = []
        for i, tr in enumerate(open_tracks):
            if i in used_tracks or t - tr.last_seen <= max_gap:
                still_open.append(tr)
            else:
                done.append(tr)
        open_tracks = still_open + [_Track(first=t, last_seen=t, readings=[r]) for r in unmatched]
    done.extend(open_tracks)

    captions = []
    for tr in sorted(done, key=lambda tr: (tr.first, tr.latest.top)):
        if len(tr.readings) == 1 and tr.readings[0].score < single_frame_score:
            continue                                      # one unsure frame: OCR noise, not a caption
        text, confidence = final_text(tr.readings)
        captions.append(Caption(id=len(captions) + 1, start=round(tr.first, 3),
                                end=round(tr.last_seen + frame_interval, 3), text=text,
                                source="ocr", ocr_confidence=confidence, box=track_box(tr.readings)))
    return captions


def track_box(readings: Sequence[Reading]) -> tuple[float, float, float, float] | None:
    """Everywhere a caption's text was on screen: (left, top, right, bottom), fractions of the frame."""
    if any(r.left is None or r.right is None for r in readings):
        return None
    return (round(min(r.left for r in readings), 4), round(min(r.top for r in readings), 4),
            round(max(r.right for r in readings), 4), round(max(r.bottom for r in readings), 4))
