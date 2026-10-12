"""Spacing check.

Flags (issue type "spacing"):
  - two or more spaces between words
  - no space after a comma or full stop ("foundation,and", "end.Next")
  - a space before punctuation ("word ,")
  - two words glued together ("andaccountability" -> "and accountability")
"""

from __future__ import annotations

import re
from typing import Sequence

from src.checks.context import CheckContext
from src.models import SPACING, Caption, Issue
from src.utils.text import non_prose_spans, replace_span, tokens

_DOUBLE_SPACE = re.compile(r"(?<=\S)[ \t]{2,}(?=\S)")
_NO_SPACE_AFTER = re.compile(r"(?<=[A-Za-z]{2})[,;!?](?=[A-Za-z])|(?<=[a-z]{2})\.(?=[A-Z][a-z])")
_SPACE_BEFORE = re.compile(r"(?<=\w)[ \t]+(?=[,.!?;:](?:\s|$))")


def _issue(cap: Caption, severity: str, message: str, suggestion: str, confidence: float) -> Issue:
    return Issue(caption_id=cap.id, timestamp=cap.start, type=SPACING, severity=severity,
                 original=cap.flat_text, message=message, suggestion=suggestion, confidence=confidence)


def _spaced(text: str) -> str:
    """The caption with spacing around punctuation fixed, on one line."""
    text = _NO_SPACE_AFTER.sub(lambda m: m.group() + " ", text)
    text = _SPACE_BEFORE.sub("", text)
    return " ".join(text.split())


def check(captions: Sequence[Caption], settings: dict, context: CheckContext | None = None) -> list[Issue]:
    if not settings.get("enabled", True):
        return []
    context = context or CheckContext()
    glued = settings.get("glued_words", {})
    short_words = {w.lower() for w in glued.get("short_words", [])}
    issues: list[Issue] = []

    for cap in sorted(captions, key=lambda c: (c.start, c.id)):
        text = cap.text
        skip = non_prose_spans(text)

        def outside_links(m: re.Match) -> bool:
            return not any(s <= m.start() < e for s, e in skip)

        if settings.get("double_spaces", True):
            doubles = [m for m in _DOUBLE_SPACE.finditer(text) if outside_links(m)]
            if doubles:
                issues.append(_issue(cap, "low", "Double space between words.",
                                     f'Use one space: "{" ".join(text.split())}"', 0.95))

        if settings.get("space_around_punctuation", True):
            missing = [m for m in _NO_SPACE_AFTER.finditer(text) if outside_links(m)]
            before = [m for m in _SPACE_BEFORE.finditer(text) if outside_links(m)]
            for m in missing:
                issues.append(_issue(cap, "medium", f'No space after "{m.group()}".',
                                     _spaced(text), 0.85))
            for m in before:
                issues.append(_issue(cap, "low", f'Space before "{text[m.end()]}".', _spaced(text), 0.85))

        if glued.get("enabled", True):
            for tok in tokens(text):
                word = tok.core
                if not word.isalpha() or len(word) < 5:
                    continue
                split = context.dictionary.split_glued(
                    word, glued.get("min_part_frequency", 100), short_words)
                if split:
                    fixed = f"{split[0]} {split[1]}"
                    issues.append(_issue(cap, "medium", f'Missing space: "{word}" should be "{fixed}".',
                                         replace_span(text, tok.start, tok.end, fixed), 0.8))
    return issues
