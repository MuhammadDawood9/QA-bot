"""Core data structures shared by every input path and every check."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

# A "word" is any whitespace-separated token with at least one letter or digit,
# so stray dashes or ellipses don't count towards the word total.
_WORDLIKE = re.compile(r"\w")

TOO_SHORT = "too_short"
PHRASE_BREAK = "phrase_break"
SPELLING = "spelling"
REPEATED_WORD = "repeated_word"
CAPITALIZATION = "capitalization"
SPACING = "spacing"
PUNCTUATION = "punctuation"
# Caption text compared with the spoken audio (Phase 4)
WRONG_WORD = "wrong_word"
MISSING_WORD = "missing_word"
EXTRA_WORD = "extra_word"
CAPTION_EARLY = "caption_early"

SEVERITIES = ("high", "medium", "low")


@dataclass(frozen=True)
class Caption:
    id: int
    start: float          # seconds
    end: float            # seconds
    text: str             # line breaks inside one caption are kept as "\n"
    source: str = "srt"   # "srt" or "ocr"
    ocr_confidence: float | None = None   # OCR captions only: how sure the reading is (0..1)

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def words(self) -> list[str]:
        return [w for w in self.text.split() if _WORDLIKE.search(w)]

    @property
    def flat_text(self) -> str:
        """Text on one line, for messages and merge suggestions."""
        return " ".join(self.text.split())


@dataclass(frozen=True)
class SpokenWord:
    """One word from the speech transcript (Whisper), with its timing and how sure Whisper was."""
    text: str
    start: float          # seconds
    end: float            # seconds
    probability: float    # 0..1

    @property
    def middle(self) -> float:
        return (self.start + self.end) / 2


@dataclass(frozen=True)
class Issue:
    caption_id: int
    timestamp: float      # seconds, usually the caption start
    type: str             # too_short, phrase_break, spelling, ..., wrong_word, missing_word, ...
    severity: str         # high, medium, low
    original: str
    message: str
    suggestion: str = ""
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"severity must be one of {SEVERITIES}, got {self.severity!r}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be between 0 and 1, got {self.confidence}")

    def to_dict(self) -> dict:
        return asdict(self)
