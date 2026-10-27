"""Punctuation by rule: free, offline, no API (the "punctuation" check is the paid AI one, which
also judges commas inside a sentence and apostrophes).

Flags (issue type "punctuation"):
  - a question without a question mark: a sentence that starts like a question ("why would you",
    "are you") but ends in "." or "!"
  - the client's full-stop style: "never" (flowing text, E3RK) flags every full stop; "sentences"
    flags a caption that ends a sentence (a pause follows) with no end punctuation
  - a comma at the end of a caption, for clients who don't want one (E2AB)
  - more commas in one caption than the client allows

Every word list and threshold is in config.yaml checks.punctuation_style; a client changes them
under overrides: punctuation_style:.
"""

from __future__ import annotations

import re
from typing import Sequence

from src.checks.context import CheckContext
from src.models import PUNCTUATION, Caption, Issue
from src.utils.text import CLOSERS, ends_sentence

# A sentence inside one caption: text up to and including its end mark, or to the caption's end.
_SENTENCE = re.compile(r"[^.!?…]+(?:[.!?…]+|$)")
# A full stop that isn't one: decimals (3.5), initials and abbreviations (U.S., Dr.), ellipses.
_REAL_FULL_STOP = re.compile(r"(?<![.\d])(?<!\b[A-Za-z])\.(?![.\d\w])")
_WORD = re.compile(r"[A-Za-z']+")


def _issue(cap: Caption, severity: str, message: str, suggestion: str, confidence: float) -> Issue:
    return Issue(caption_id=cap.id, timestamp=cap.start, type=PUNCTUATION, severity=severity,
                 original=cap.flat_text, message=message, suggestion=suggestion, confidence=confidence)


def looks_like_question(sentence: str, settings: dict) -> bool:
    """A question word followed by an auxiliary ("why would", "what is"), or a sentence that
    starts with an auxiliary followed by a pronoun ("are you", "can we"). "What I mean is",
    "how we did it" and "what's important is" are statements and don't count."""
    words = [w.lower() for w in _WORD.findall(sentence.replace("’", "'"))]
    if len(words) < 2:
        return False
    wh = {w.lower() for w in settings.get("question_words", [])}
    aux = {w.lower() for w in settings.get("auxiliaries", [])}
    pronouns = {w.lower() for w in settings.get("pronouns", [])}
    first, second = words[0], words[1]
    if first in wh:
        return second in aux
    if first in aux:
        return second in pronouns
    return False


def _question_issues(cap: Caption, settings: dict) -> list[Issue]:
    out = []
    text = cap.text
    for m in _SENTENCE.finditer(text):
        core = m.group().rstrip().rstrip(CLOSERS)            # the sentence up to and including its end marks
        marks_end = m.start() + len(core)
        marks_start = marks_end
        while marks_start > m.start() and text[marks_start - 1] in ".!":
            marks_start -= 1
        if marks_start == marks_end or text[marks_end - 1:marks_end] not in (".", "!"):
            continue
        if not looks_like_question(core, settings):
            continue
        body = " ".join(text[m.start():marks_start].split())
        fixed = text[:marks_start] + "?" + text[marks_end:]
        out.append(_issue(cap, "medium", f'"{body}" is a question: end it with "?" not "{text[marks_start:marks_end]}".',
                          " ".join(fixed.split()), 0.7))
    return out


def check(captions: Sequence[Caption], settings: dict, context: CheckContext | None = None) -> list[Issue]:
    if not settings.get("enabled", True):
        return []
    questions = settings.get("question_marks") or {}
    full_stops = str(settings.get("full_stops", "any")).lower()
    gap = float(settings.get("sentence_gap_sec", 2.0))
    trailing_commas = bool(settings.get("trailing_commas", True))
    max_commas = int(settings.get("max_commas_per_caption", 0) or 0)
    abbreviations = {a.lower().rstrip(".") for a in settings.get("abbreviations", [])}

    ordered = sorted(captions, key=lambda c: (c.start, c.id))
    issues: list[Issue] = []
    for n, cap in enumerate(ordered):
        text = cap.text.strip()
        if not text:
            continue
        if questions.get("enabled", True):
            issues += _question_issues(cap, questions)

        if full_stops == "never":
            stops = [m for m in _REAL_FULL_STOP.finditer(cap.text)
                     if (re.findall(r"[A-Za-z]+$", cap.text[:m.start()]) or [""])[0].lower() not in abbreviations]
            if stops:
                fixed = "".join(ch for i, ch in enumerate(cap.text) if i not in {m.start() for m in stops})
                issues.append(_issue(cap, "medium",
                                     "This client's style has no full stops (flowing sentences): remove "
                                     + ("it." if len(stops) == 1 else f"all {len(stops)}."),
                                     " ".join(fixed.split()), 0.85))
        elif full_stops == "sentences":
            nxt = ordered[n + 1] if n + 1 < len(ordered) else None
            ends_here = nxt is None or nxt.start - cap.end >= gap
            if ends_here and not ends_sentence(text) and not text.rstrip(CLOSERS).endswith((",", ";", ":", "-", "—")):
                issues.append(_issue(cap, "low", "This sentence ends here (a pause follows) but has no full stop.",
                                     text + ".", 0.6))

        if not trailing_commas and text.rstrip(CLOSERS).endswith(","):
            issues.append(_issue(cap, "medium", "This client doesn't want a comma at the end of a caption.",
                                 text.rstrip(CLOSERS)[:-1].rstrip() + text[len(text.rstrip(CLOSERS)):], 0.8))
        if max_commas > 0 and text.count(",") > max_commas:
            issues.append(_issue(cap, "low", f"{text.count(',')} commas in one caption (this client's style allows "
                                             f"{max_commas}): too many for light punctuation.", text, 0.6))
    return issues
