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
CAPTION_LATE = "caption_late"
CAPTION_SYNC = "caption_sync"                # all captions shifted by the same amount (one issue per video)
UNCAPTIONED_SPEECH = "uncaptioned_speech"    # caption_id is negative: there is no caption
# On-screen graphics (logos, name titles) compared with the captions
SCREEN_TEXT_MISMATCH = "screen_text_mismatch"
# Color grading, per shot (src/video/color.py)
OVEREXPOSED = "overexposed"
UNDEREXPOSED = "underexposed"
CRUSHED_BLACKS = "crushed_blacks"
FLAT_CONTRAST = "flat_contrast"
SATURATION = "saturation"
COLOR_CAST = "color_cast"
SKIN_TONE = "skin_tone"
SHOT_MISMATCH = "shot_mismatch"
COLOR_ISSUES = {OVEREXPOSED, UNDEREXPOSED, CRUSHED_BLACKS, FLAT_CONTRAST, SATURATION, COLOR_CAST, SKIN_TONE,
                SHOT_MISMATCH}

# Audio edits (podcasts, src/audio/edits.py)
STUTTER = "stutter"
REPEATED_PHRASE = "repeated_phrase"
ABRUPT_CUT = "abrupt_cut"
JUMP_CUT = "jump_cut"
DEAD_AIR = "dead_air"
FILLER = "filler"
MISSING_CUE = "missing_cue"          # a cue the client's audio guidelines require isn't there (off by default)
AUDIO_ISSUES = {STUTTER, REPEATED_PHRASE, ABRUPT_CUT, JUMP_CUT, DEAD_AIR, FILLER, MISSING_CUE}

# Issues that aren't about a caption have negative caption ids: -1, -2... for speech with no
# caption, -1001, -1002... for shot 1, 2... of the color checks, -3001, -3002... for audio issues.
SHOT_ID_BASE = 1000
AUDIO_ID_BASE = 3000                 # audio issue n: -3000 - n


def shot_issue_id(shot_number: int) -> int:
    return -(SHOT_ID_BASE + shot_number)


def shot_number(caption_id: int) -> int | None:
    """The shot number for a color issue's id, None for any other id."""
    return -caption_id - SHOT_ID_BASE if -AUDIO_ID_BASE < caption_id <= -SHOT_ID_BASE else None


def audio_issue_id(n: int) -> int:
    return -(AUDIO_ID_BASE + n)


def audio_number(caption_id: int) -> int | None:
    """The number of an audio issue's id, None for any other id."""
    return -caption_id - AUDIO_ID_BASE if caption_id <= -AUDIO_ID_BASE else None

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
    signals: str = ""     # what the flag rests on (audio checks: each piece of evidence and its weight)

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"severity must be one of {SEVERITIES}, got {self.severity!r}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be between 0 and 1, got {self.confidence}")

    def to_dict(self) -> dict:
        return asdict(self)
