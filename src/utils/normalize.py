"""Turn caption text and spoken words into comparable tokens.

Both sides get the same treatment: lowercase, no punctuation or apostrophes, numbers written as
words ("40%" -> "forty percent"), equivalent spellings unified ("gonna" -> "going to"), and
filler words ("um", "uh") dropped. Each token remembers which original word it came from, so
messages can quote the real text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_SCALES = [(1_000_000_000, "billion"), (1_000_000, "million"), (1_000, "thousand")]
_ORDINAL_IRREGULAR = {"one": "first", "two": "second", "three": "third", "five": "fifth",
                      "eight": "eighth", "nine": "ninth", "twelve": "twelfth"}

_NUMBER = re.compile(r"^(?P<currency>\$)?(?P<int>\d[\d,]*)(?:\.(?P<frac>\d+))?"
                     r"(?P<suffix>%|st|nd|rd|th|s)?$")
_EDGE = re.compile(r"^[^\w$]+|[^\w%]+$")
_SPLIT = re.compile(r"[-–—/]+")


def number_to_words(n: int) -> list[str]:
    if n < 20:
        return [_ONES[n]]
    if n < 100:
        tens, ones = divmod(n, 10)
        return [_TENS[tens]] + ([_ONES[ones]] if ones else [])
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        return [_ONES[hundreds], "hundred"] + (number_to_words(rest) if rest else [])
    for value, name in _SCALES:
        if n >= value:
            high, rest = divmod(n, value)
            return number_to_words(high) + [name] + (number_to_words(rest) if rest else [])
    return [str(n)]


def _ordinal(words: list[str]) -> list[str]:
    last = words[-1]
    if last in _ORDINAL_IRREGULAR:
        last = _ORDINAL_IRREGULAR[last]
    elif last.endswith("y"):
        last = last[:-1] + "ieth"
    else:
        last += "th"
    return words[:-1] + [last]


def _number_tokens(text: str) -> list[str] | None:
    """Words for a number written in digits, or None if it isn't one."""
    m = _NUMBER.match(text)
    if not m:
        return None
    digits = m.group("int").replace(",", "")
    if len(digits) > 12:
        return None
    words = number_to_words(int(digits))
    if m.group("frac"):
        words += ["point"] + [_ONES[int(d)] for d in m.group("frac")]
    suffix = m.group("suffix")
    if suffix in ("st", "nd", "rd", "th"):
        words = _ordinal(words)
    elif suffix == "s":                     # "1990s" -> "nineteen nineties" is rare; keep it simple
        words[-1] += "s"
    elif suffix == "%":
        words.append("percent")
    if m.group("currency"):
        words.append("dollars")
    return words


@dataclass(frozen=True)
class Token:
    text: str
    source: int        # index of the original word (caption word or spoken word) it came from


def word_tokens(word: str, fillers: set[str] = frozenset(),
                equivalents: Mapping[str, str] | None = None) -> list[str]:
    """Normalized tokens for one original word (may be zero, one or several)."""
    word = word.replace("’", "'").replace("&", " and ").lower()
    out: list[str] = []
    for piece in _SPLIT.split(word):
        for part in piece.split():
            if part.strip(".,!?;:") == "%":            # Whisper sometimes writes "40 %"
                out.append("percent")
                continue
            part = _EDGE.sub("", part)
            if not part:
                continue
            number = _number_tokens(part)
            if number is not None:
                out.extend(number)
                continue
            part = re.sub(r"[^\w]", "", part)          # it's -> its, y'all -> yall
            if not part:
                continue
            mapped = (equivalents or {}).get(part, part)
            out.extend(mapped.split())
    return [t for t in out if t not in fillers]


def tokenize(words: Iterable[str], fillers: Iterable[str] = (),
             equivalents: Mapping[str, str] | None = None) -> list[Token]:
    """Normalize a sequence of original words, keeping track of where each token came from."""
    filler_set = {f.lower() for f in fillers}
    tokens: list[Token] = []
    for index, word in enumerate(words):
        tokens.extend(Token(t, index) for t in word_tokens(word, filler_set, equivalents))
    return tokens
