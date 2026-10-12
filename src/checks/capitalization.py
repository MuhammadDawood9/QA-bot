"""Capitalization check.

Flags (issue type "capitalization"):
  - lowercase "i" ("i should be" -> "I should be")
  - forced glossary spellings written with the wrong case ("Mark Mcgwire", "youtube")
  - proper nouns from config written in lowercase (months and days: "august" -> "August")
  - broken mixed case ("realM", "THe"); brand-style names like YouTube, iPhone, McGwire are fine
  - a common word capitalized in the middle of a sentence ("and There it was")

Clients with allow_stylized_lowercase (E8D) may write anything in all lowercase on purpose,
so lowercase "i", months and names are not flagged for them. Broken mixed case still is.
"""

from __future__ import annotations

import re
from typing import Sequence

from src.checks.context import CheckContext
from src.models import CAPITALIZATION, Caption, Issue
from src.utils.text import ends_sentence, replace_span, starts_sentence, tokens

_I_FORMS = {"i", "i'm", "i'll", "i've", "i'd"}
_TITLE = re.compile(r"[A-Z][a-z']+")
_BRAND_STYLE = re.compile(r"(?:[A-Z][a-z]+){2,}|[a-z][A-Z][a-z]+|[A-Z]{2,}s")   # YouTube, McGwire, iPhone, CEOs
_LOWER_THEN_UPPER = re.compile(r"[a-z].*[A-Z]")
_SHIFT_SLIP = re.compile(r"[A-Z]{2,}[a-z]+")                                      # THe, YOu
_COMMON_WORD_FREQUENCY = 1000


def _base(word: str) -> str:
    return word[:-2] if word.lower().endswith("'s") and len(word) > 2 else word


def _is_name_part(word: str) -> bool:
    """A capitalized neighbour means a name or title ("Warren Buffett", "New York"); "I" doesn't count."""
    return bool(_TITLE.fullmatch(_base(word))) and word.lower() not in _I_FORMS


def _bad_mixed_case(word: str) -> bool:
    letters = word.replace("'", "")
    if not letters.isalpha() or _BRAND_STYLE.fullmatch(letters):
        return False
    return bool(_LOWER_THEN_UPPER.search(letters) or _SHIFT_SLIP.fullmatch(letters))


def _issue(cap: Caption, severity: str, message: str, suggestion: str, confidence: float) -> Issue:
    return Issue(caption_id=cap.id, timestamp=cap.start, type=CAPITALIZATION, severity=severity,
                 original=cap.flat_text, message=message, suggestion=suggestion, confidence=confidence)


def _phrase_issues(cap: Caption, phrases: list[str], stylized: bool) -> tuple[list[Issue], list[tuple[int, int]]]:
    """Multi-word forced spellings. Returns the issues and the spans they cover."""
    issues, spans = [], []
    for phrase in phrases:
        pattern = r"(?<![\w'])" + r"\s+".join(re.escape(w) for w in phrase.split()) + r"(?![\w'])"
        for m in re.finditer(pattern, cap.text, re.IGNORECASE):
            spans.append(m.span())
            written = " ".join(m.group().split())
            if written == phrase or (stylized and written.islower()):
                continue
            issues.append(_issue(cap, "high", f'Must be written exactly "{phrase}" (glossary).',
                                 replace_span(cap.text, m.start(), m.end(), phrase), 0.9))
    return issues, spans


def check(captions: Sequence[Caption], settings: dict, context: CheckContext | None = None) -> list[Issue]:
    if not settings.get("enabled", True):
        return []
    context = context or CheckContext()
    glossary = context.glossary
    stylized = settings.get("allow_stylized_lowercase", False)
    proper = {w.lower() for w in settings.get("proper_nouns", [])}
    ambiguous = {w.lower() for w in settings.get("ambiguous_lowercase", [])}
    mid = settings.get("mid_sentence_capitals", {})
    check_mixed = settings.get("check_mixed_case", True)

    forced = dict(glossary.forced_words)
    for low, exact in glossary.forced_tokens.items():
        # Distinctive words inside forced phrases ("McGwire") are enforced on their own too.
        if low not in forced and context.dictionary.frequency(low) < _COMMON_WORD_FREQUENCY:
            forced[low] = exact

    issues: list[Issue] = []
    prev: Caption | None = None
    for cap in sorted(captions, key=lambda c: (c.start, c.id)):
        text = cap.text
        toks = tokens(text)
        new_sentence = (prev is None or ends_sentence(prev.text)
                        or cap.start - prev.end >= mid.get("sentence_gap_sec", 2.0))
        first_word_is_start = new_sentence or not mid.get("check_caption_starts", False)

        phrase_issues, covered = _phrase_issues(cap, glossary.forced_phrases, stylized)
        issues += phrase_issues

        for i, tok in enumerate(toks):
            word = tok.core
            low = word.lower()
            base = _base(word)
            if any(s <= tok.start < e for s, e in covered):
                continue

            if low in _I_FORMS and word[0] == "i":
                is_abbreviation = text[tok.end:tok.end + 1] == "." and text[tok.end + 1:tok.end + 2].isalpha()
                if not stylized and not is_abbreviation:
                    issues.append(_issue(cap, "high", '"I" is always a capital letter.',
                                         replace_span(text, tok.start, tok.end, "I" + tok.raw[1:]), 0.95))
                continue
            if low in _I_FORMS:
                continue                                   # "I", "I've": always correct

            exact = forced.get(base.lower())
            if exact is not None:
                if base != exact and not (stylized and base.islower()):
                    issues.append(_issue(cap, "high", f'Must be written exactly "{exact}" (glossary).',
                                         replace_span(text, tok.start, tok.start + len(base), exact), 0.9))
                continue

            if base.lower() in proper:
                if base.islower() and base.lower() not in ambiguous and not stylized:
                    fixed = base[0].upper() + base[1:]
                    issues.append(_issue(cap, "medium", f'"{fixed}" is a proper noun: capitalize it.',
                                         replace_span(text, tok.start, tok.start + len(base), fixed), 0.85))
                continue

            if glossary.is_known(base):
                continue

            start = starts_sentence(text, toks, i, first_word_is_start)
            if check_mixed and _bad_mixed_case(word):
                fixed = word.capitalize() if start else word.lower()
                issues.append(_issue(cap, "medium", f'Odd capitalization: "{word}".',
                                     replace_span(text, tok.start, tok.end, fixed), 0.75))
                continue

            if (mid.get("enabled", True) and not start and _TITLE.fullmatch(base)
                    and not (i > 0 and _is_name_part(toks[i - 1].core))
                    and not (i + 1 < len(toks) and _is_name_part(toks[i + 1].core))
                    and context.dictionary.frequency(base) >= mid.get("min_word_frequency", 100000)):
                issues.append(_issue(cap, "low", f'Capital letter mid-sentence: "{word}".',
                                     replace_span(text, tok.start, tok.end, word.lower()), 0.6))
        prev = cap
    return issues
