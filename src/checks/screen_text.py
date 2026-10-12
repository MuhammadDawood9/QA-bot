"""Caption words compared with the text in the video's own graphics (logos, name titles).

A name spelled one way in the graphics and almost the same way in a caption is very likely a
caption mistake: the logo says "MAIER", the caption says "Meyer". The on-screen text comes from
the OCR's on-screen text pass (src/inputs/video_ocr.read_screen_text), so this runs only when a
video is given.

Flags (issue type "screen_text_mismatch") a caption word that:
  - starts with a capital (names do; set require_capital: false for all-lowercase styles),
  - is not itself one of the on-screen words,
  - is within max_edits letters of an on-screen word that is rare in everyday English (a name,
    not "State" or "Representative", so ordinary words never trigger it).
High severity when the word next to it is also a name from the graphics ("Elizabeth Meyer"
next to "ELIZABETH MAIER"), medium otherwise.
"""

from __future__ import annotations

import re
from typing import Sequence

from src.checks.context import CheckContext
from src.models import SCREEN_TEXT_MISMATCH, Caption, Issue
from src.utils.text import edit_distance, match_case, replace_span, tokens

_LETTERS = re.compile(r"[A-Za-z][A-Za-z'’-]*[A-Za-z]")


def screen_words(texts: Sequence[str]) -> list[str]:
    """The words in the on-screen texts, in order, each once (as written on screen)."""
    out: dict[str, str] = {}
    for text in texts:
        for w in _LETTERS.findall(text):
            out.setdefault(w.lower(), w)
    return list(out.values())


def _allowed_edits(word: str, max_edits: int) -> int:
    return 1 if len(word) <= 4 else max_edits


def check(captions: Sequence[Caption], settings: dict, context: CheckContext | None = None) -> list[Issue]:
    if not settings.get("enabled", True) or context is None or not context.screen_text:
        return []
    min_len = int(settings.get("min_word_length", 4))
    max_edits = int(settings.get("max_edits", 2))
    max_freq = int(settings.get("max_term_frequency", 5000))
    require_capital = settings.get("require_capital", True)

    words = screen_words(context.screen_text)
    on_screen = {w.lower() for w in words}
    # Only rare words can be names; common ones ("State") would match far too much.
    names = [w for w in words if len(w) >= min_len and context.dictionary.frequency(w) <= max_freq]
    if not names:
        return []
    name_set = {n.lower() for n in names}

    issues = []
    for cap in captions:
        text = cap.flat_text
        toks = tokens(text)
        for i, tok in enumerate(toks):
            word = tok.core
            if word.lower().endswith("'s"):
                word = word[:-2]
            low = word.lower()
            if len(word) < min_len or low in on_screen or (require_capital and not word[:1].isupper()):
                continue
            best = min(names, key=lambda n: (edit_distance(low, n.lower()), n))
            distance = edit_distance(low, best.lower())
            if distance == 0 or distance > _allowed_edits(best, max_edits):
                continue
            neighbours = [toks[j].core.lower() for j in (i - 1, i + 1) if 0 <= j < len(toks)]
            backed = any(n in name_set for n in neighbours)
            fixed = match_case(best.lower(), word)
            start = tok.start + tok.raw.find(word) if word in tok.raw else tok.start
            issues.append(Issue(
                caption_id=cap.id, timestamp=cap.start, type=SCREEN_TEXT_MISMATCH,
                severity="high" if backed else "medium", original=text,
                message=(f'"{word}" is spelled "{best}" in the on-screen graphics'
                         + (" (the word next to it matches the graphics too)." if backed else ".")),
                suggestion=replace_span(text, start, start + len(word), fixed),
                confidence=0.9 if backed else 0.7,
            ))
    return issues
