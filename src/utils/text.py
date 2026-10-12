"""Small text helpers shared by the spelling, capitalization and spacing checks."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Words, including contractions (don't, y'all) and hyphenated compounds (well-known).
_TOKEN = re.compile(r"[\w'’]+(?:-[\w'’]+)*")
# Things that aren't words to proofread: links, e-mail addresses, @handles and #hashtags.
_NOT_PROSE = re.compile(r"https?://\S+|www\.\S+|\S+@\S+\.\S+|[@#]\w+|\b\w+\.(?:com|org|net|io|co)\b", re.I)
_SENTENCE_BREAK = re.compile(r"[.!?…:]")
_OPEN_QUOTE = re.compile(r"[\"“(\[]\s*$")

SENTENCE_END = (".", "!", "?", "…")
CLOSERS = "\"'”’)]"


@dataclass(frozen=True)
class Token:
    start: int
    end: int
    raw: str

    @property
    def core(self) -> str:
        """The word without surrounding apostrophes, with curly apostrophes made straight."""
        return self.raw.replace("’", "'").strip("'")


def non_prose_spans(text: str) -> list[tuple[int, int]]:
    """Where links, e-mail addresses, @handles and #hashtags are, so checks can skip them."""
    return [m.span() for m in _NOT_PROSE.finditer(text)]


def tokens(text: str) -> list[Token]:
    skip = non_prose_spans(text)
    out = []
    for m in _TOKEN.finditer(text):
        if any(s <= m.start() < e for s, e in skip):
            continue
        if m.group().strip("'’"):
            out.append(Token(m.start(), m.end(), m.group()))
    return out


def starts_sentence(text: str, toks: list[Token], i: int, caption_starts_sentence: bool) -> bool:
    """Is token i the first word of a sentence (or of a quote)?"""
    before = text[toks[i - 1].end: toks[i].start] if i > 0 else text[: toks[i].start]
    if i == 0 and caption_starts_sentence:
        return True
    return bool(_SENTENCE_BREAK.search(before) or _OPEN_QUOTE.search(before))


def ends_sentence(text: str) -> bool:
    return text.rstrip().rstrip(CLOSERS).endswith(SENTENCE_END)


def replace_span(text: str, start: int, end: int, new: str) -> str:
    """Replace text[start:end] and return the caption on one line, for suggestions."""
    return " ".join((text[:start] + new + text[end:]).split())


def edit_distance(a: str, b: str) -> int:
    """Damerau-Levenshtein (optimal string alignment): swaps of two letters count as one edit."""
    if a == b:
        return 0
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[-1]


def match_case(word: str, model: str) -> str:
    """Give a suggestion the same capitalization style as the word it replaces."""
    if model.isupper() and len(model) > 1:
        return word.upper()
    if model[:1].isupper():
        return word[:1].upper() + word[1:]
    return word
