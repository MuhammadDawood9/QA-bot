"""Spelling check.

Flags:
  spelling       a word that isn't in the dictionary or glossary.txt ("hourse")
  spelling       a near-miss of a forced glossary spelling ("McGuire" when the glossary says =Mark McGwire)
  spelling       a letter used as a digit ("2O26", "O%")
  repeated_word  the same word twice in a row ("to to"), also across two captions

Unknown words that start with a capital are reported as low severity: they are often names
that just need adding to glossary.txt. Glued words ("andaccountability") are left to the
spacing check so the same word isn't reported twice.
"""

from __future__ import annotations

import re
from typing import Sequence

from src.checks.context import CheckContext
from src.models import REPEATED_WORD, SPELLING, Caption, Issue
from src.utils.text import Token, edit_distance, match_case, replace_span, tokens

_LOOKALIKE = re.compile(r"[0-9OolI]+")
_TO_DIGIT = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1"})
# Words inside forced glossary terms that are this common ("Mark" in "Mark McGwire") are
# not used for near-miss matching; only the distinctive part ("McGwire") is.
_COMMON_WORD_FREQUENCY = 1000


def _near_forced(word: str, context: CheckContext, max_distance: int) -> str | None:
    """The forced glossary spelling this word looks like a misspelling of, if any."""
    lw = word.lower()
    glossary, dictionary = context.glossary, context.dictionary
    if lw in glossary.forced_tokens or glossary.is_known(word) or len(word) < 5:
        return None
    if dictionary.is_known(word) and not word[:1].isupper():
        return None
    best, best_dist = None, None
    for low, exact in glossary.forced_tokens.items():
        if len(low) < 5 or dictionary.frequency(low) >= _COMMON_WORD_FREQUENCY:
            continue
        allowed = min(max_distance, max(1, len(low) // 4))
        dist = edit_distance(lw, low)
        if dist <= allowed and (best_dist is None or dist < best_dist):
            best, best_dist = exact, dist
    return best


def _glued_params(context: CheckContext) -> dict | None:
    spacing = context.settings_for("spacing")
    glued = spacing.get("glued_words", {})
    if not spacing.get("enabled", False) or not glued.get("enabled", False):
        return None
    return {"min_part_frequency": glued.get("min_part_frequency", 100),
            "short_words": {w.lower() for w in glued.get("short_words", [])}}


def _spelling_issues(cap: Caption, toks: list[Token], settings: dict, context: CheckContext) -> list[Issue]:
    dictionary = context.dictionary
    glued = _glued_params(context)
    max_suggestions = settings.get("max_suggestions", 3)
    min_frequency = settings.get("min_suggestion_frequency", 51)
    forced_distance = settings.get("forced_max_distance", 2)
    issues = []

    for tok in toks:
        word = tok.core
        if any(ch.isdigit() for ch in word) or (word.isupper() and len(word) <= 5):
            continue                                   # numbers, and acronyms like QC or CEO
        for part in word.split("-"):
            if not part.replace("'", "").isalpha():
                continue
            forced = _near_forced(part, context, forced_distance)
            if forced:
                issues.append(Issue(
                    caption_id=cap.id, timestamp=cap.start, type=SPELLING, severity="high",
                    original=cap.flat_text,
                    message=f'"{part}" should be spelled "{forced}" (glossary).',
                    suggestion=replace_span(cap.text, tok.start, tok.end, tok.raw.replace(part, forced)),
                    confidence=0.9,
                ))
                continue
            if dictionary.is_known(part):
                continue
            if glued and dictionary.split_glued(part, **glued):
                continue
            options = [match_case(s, part) for s in dictionary.suggestions(part, max_suggestions, min_frequency)]
            hint = f"Did you mean: {', '.join(options)}?" if options else "Check the spelling."
            if part[:1].isupper():
                issues.append(Issue(
                    caption_id=cap.id, timestamp=cap.start, type=SPELLING, severity="low",
                    original=cap.flat_text,
                    message=f'Unknown word "{part}": a misspelling, or a name to add to glossary.txt?',
                    suggestion=hint, confidence=0.5,
                ))
            else:
                issues.append(Issue(
                    caption_id=cap.id, timestamp=cap.start, type=SPELLING, severity="medium",
                    original=cap.flat_text, message=f'Possible misspelling: "{part}".',
                    suggestion=hint, confidence=0.8,
                ))
    return issues


_FLAGGED_WORD = re.compile(r'^(?:Unknown word|Possible misspelling:) "([^"]+)"')


def flagged_word(issue: Issue) -> str | None:
    """The word an unknown-word flag is about ("McGwire"), for an ignore rule or a glossary line;
    None for any other flag."""
    if issue.type != SPELLING:
        return None
    match = _FLAGGED_WORD.match(issue.message)
    return match.group(1) if match else None


def _lookalike_issues(cap: Caption, toks: list[Token]) -> list[Issue]:
    issues = []
    for tok in toks:
        word = tok.core
        has_digit = any(ch.isdigit() for ch in word)
        mixed = _LOOKALIKE.fullmatch(word) and has_digit and not word.isdigit()
        lone_o = word in ("O", "o") and (cap.text[tok.end:tok.end + 1] == "%"
                                         or cap.text[max(0, tok.start - 1):tok.start] == "$")
        if mixed or lone_o:
            fixed = word.translate(_TO_DIGIT)
            issues.append(Issue(
                caption_id=cap.id, timestamp=cap.start, type=SPELLING, severity="high",
                original=cap.flat_text,
                message=f'Letter used as a digit in "{word}". Numbers must use real digits.',
                suggestion=replace_span(cap.text, tok.start, tok.end, fixed),
                confidence=0.9,
            ))
    return issues


def _repeat_issues(cap: Caption, toks: list[Token], prev: Caption | None, prev_toks: list[Token],
                   settings: dict) -> list[Issue]:
    rw = settings.get("repeated_words", {})
    if not rw.get("enabled", True):
        return []
    allowed = {w.lower() for w in rw.get("allowed", [])}
    issues = []

    for a, b in zip(toks, toks[1:]):
        word = a.core.lower()
        if word == b.core.lower() and not cap.text[a.end:b.start].strip() and word not in allowed:
            issues.append(Issue(
                caption_id=cap.id, timestamp=cap.start, type=REPEATED_WORD, severity="medium",
                original=cap.flat_text, message=f'Repeated word: "{a.core} {b.core}".',
                suggestion=f'Remove one: "{replace_span(cap.text, a.end, b.end, "")}"',
                confidence=0.8,
            ))

    if prev is not None and prev_toks and toks:
        last, first = prev_toks[-1], toks[0]
        word = first.core.lower()
        gap = cap.start - prev.end
        if (word == last.core.lower() and word not in allowed and not prev.text[last.end:].strip()
                and not cap.text[:first.start].strip() and gap <= rw.get("across_captions_max_gap_sec", 1.0)):
            issues.append(Issue(
                caption_id=cap.id, timestamp=cap.start, type=REPEATED_WORD, severity="medium",
                original=cap.flat_text,
                message=f'Repeated word across captions: previous caption ends with "{last.core}".',
                suggestion=f'Remove one: "{replace_span(cap.text, first.start, first.end, "")}"',
                confidence=0.75,
            ))
    return issues


def check(captions: Sequence[Caption], settings: dict, context: CheckContext | None = None) -> list[Issue]:
    if not settings.get("enabled", True):
        return []
    context = context or CheckContext()
    issues: list[Issue] = []
    prev, prev_toks = None, []
    for cap in sorted(captions, key=lambda c: (c.start, c.id)):
        toks = tokens(cap.text)
        issues += _spelling_issues(cap, toks, settings, context)
        if settings.get("lookalike_digits", True):
            issues += _lookalike_issues(cap, toks)
        issues += _repeat_issues(cap, toks, prev, prev_toks, settings)
        prev, prev_toks = cap, toks
    return issues
