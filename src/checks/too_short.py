"""Too-short and phrase-break caption check.

Flags (all thresholds come from config.yaml / the client file):
  too_short     fewer than min_words words, or on screen for less than min_duration_sec
  phrase_break  the caption ends on a dangling word ("...in the"), or, if enabled for the
                client, a short fragment runs straight into the next caption ("so you even")

Exceptions (word count and phrase breaks only; a caption that flashes by is always flagged):
  - last caption of a segment (end of file, or a long gap after it)
  - whitelisted one-word captions ("Wow.")
  - a large gap before or after the caption (intentional pause)
"""

from __future__ import annotations

import math
import re
from typing import Sequence

from src.models import PHRASE_BREAK, TOO_SHORT, Caption, Issue

_SENTENCE_END = (".", "!", "?", "…")
_ANY_END_PUNCT = _SENTENCE_END + (",", ";", ":", "—", "-")
_CLOSERS = "\"'”’)]"
_EDGE_PUNCT = re.compile(r"^[^\w']+|[^\w']+$")


def _norm(word: str) -> str:
    return _EDGE_PUNCT.sub("", word).lower().replace("’", "'")


def _word_set(settings: dict, *keys: str) -> set[str]:
    """A word list from settings, normalized. Rejects YAML's unquoted yes/no/on/off booleans."""
    node = settings
    for key in keys[:-1]:
        node = node.get(key, {})
    words = node.get(keys[-1], [])
    bad = [w for w in words if not isinstance(w, str)]
    if bad:
        raise ValueError(f"too_short.{'.'.join(keys)} contains {bad}: put words in quotes in the "
                         f"YAML file (unquoted yes/no/on/off are read as true/false)")
    return {_norm(w) for w in words}


def _trailing(text: str) -> str:
    return text.rstrip().rstrip(_CLOSERS)


def ends_sentence(caption: Caption) -> bool:
    return _trailing(caption.text).endswith(_SENTENCE_END)


def _ends_with_punct(caption: Caption) -> bool:
    return _trailing(caption.text).endswith(_ANY_END_PUNCT)


def _exemption(words: list[str], gap_before: float, gap_after: float,
               exc: dict, whitelist: set[str]) -> str | None:
    """Why this caption is excused, or None. Pass an empty whitelist to skip that exception."""
    if exc.get("whitelist", True) and len(words) == 1 and _norm(words[0]) in whitelist:
        return "whitelisted"
    if exc.get("last_of_segment", True) and gap_after >= exc.get("segment_gap_sec", 2.0):
        return "last caption of segment"
    if exc.get("isolated_by_gap", True):
        limit = exc.get("isolation_gap_sec", 1.5)
        if gap_before >= limit or gap_after >= limit:
            return "isolated by pause"
    return None


def _merge_suggestion(caps: Sequence[Caption], i: int, order: list[str], settings: dict) -> str | None:
    sug = settings.get("suggestion", {})
    max_gap = sug.get("merge_max_gap_sec", 1.0)
    max_dur = sug.get("max_merged_duration_sec", 6.0)
    max_words = sug.get("max_merged_words", 12)
    cap = caps[i]
    for direction in order:
        j = i + 1 if direction == "next" else i - 1
        if not 0 <= j < len(caps):
            continue
        other = caps[j]
        first, second = (cap, other) if direction == "next" else (other, cap)
        gap = second.start - first.end
        merged = f"{first.flat_text} {second.flat_text}"
        if gap <= max_gap and second.end - first.start <= max_dur and len(merged.split()) <= max_words:
            return f'Merge with {direction} caption: "{merged}"'
    return None


def _merge_order(caps: Sequence[Caption], i: int, gap_before: float, gap_after: float,
                 min_words: int) -> list[str]:
    cap = caps[i]
    prev = caps[i - 1] if i > 0 else None
    nxt = caps[i + 1] if i + 1 < len(caps) else None
    if ends_sentence(cap):
        return ["previous", "next"]          # it closes the sentence the previous caption began
    if prev is None or ends_sentence(prev):
        return ["next", "previous"]          # it opens a new sentence
    if len(prev.words) < min_words:
        return ["previous", "next"]          # two fragments in a row belong together
    if nxt is not None and len(nxt.words) < min_words:
        return ["next", "previous"]
    return ["previous", "next"] if gap_before < gap_after else ["next", "previous"]


def check(captions: Sequence[Caption], settings: dict, context=None) -> list[Issue]:
    """context is unused: this check needs nothing beyond the captions and its settings."""
    if not settings.get("enabled", True):
        return []

    min_words = settings.get("min_words", 2)
    min_duration = settings.get("min_duration_sec", 0.5)
    exc = settings.get("exceptions", {})
    whitelist = _word_set(settings, "whitelist")
    pb = settings.get("phrase_break", {})
    dangling = _word_set(settings, "phrase_break", "dangling_end_words")
    fragment_max_words = pb.get("fragment_max_words", 0)
    fragment_max_gap = pb.get("fragment_max_gap_sec", 0.3)

    caps = sorted(captions, key=lambda c: (c.start, c.id))
    issues: list[Issue] = []

    for i, cap in enumerate(caps):
        words = cap.words
        if not words:
            continue
        gap_before = cap.start - caps[i - 1].end if i > 0 else math.inf
        gap_after = caps[i + 1].start - cap.end if i + 1 < len(caps) else math.inf

        too_brief = cap.duration < min_duration
        too_few = len(words) < min_words and _exemption(
            words, gap_before, gap_after, exc, whitelist) is None

        if too_brief or too_few:
            reasons = []
            if too_few:
                reasons.append(f"{len(words)} word{'s' if len(words) != 1 else ''} (minimum {min_words})")
            if too_brief:
                reasons.append(f"on screen {cap.duration:.2f}s (minimum {min_duration:.2f}s)")
            order = _merge_order(caps, i, gap_before, gap_after, min_words)
            suggestion = _merge_suggestion(caps, i, order, settings)
            if suggestion is None:
                suggestion = (f"Keep it on screen for at least {min_duration:.2f}s"
                              if not too_few else f"Show at least {min_words} words together")
            issues.append(Issue(
                caption_id=cap.id,
                timestamp=cap.start,
                type=TOO_SHORT,
                severity="high" if too_brief or len(words) == 1 else "medium",
                original=cap.flat_text,
                message="Caption too short: " + "; ".join(reasons) + ".",
                suggestion=suggestion,
                confidence=0.95 if too_few and len(words) == 1 else 0.85,
            ))
            continue

        if not pb.get("enabled", True):
            continue
        if _exemption(words, gap_before, gap_after, exc, whitelist=set()):
            continue

        last = _norm(words[-1])
        if last in dangling and not ends_sentence(cap):
            message = f'Caption ends mid-phrase on "{words[-1]}".'
            confidence = 0.7
        elif (fragment_max_words and len(words) <= fragment_max_words
              and not _ends_with_punct(cap) and gap_after <= fragment_max_gap):
            message = (f"Short fragment ({len(words)} words) with no ending punctuation runs "
                       f"straight into the next caption.")
            confidence = 0.6
        else:
            continue

        suggestion = _merge_suggestion(caps, i, ["next", "previous"], settings)
        issues.append(Issue(
            caption_id=cap.id,
            timestamp=cap.start,
            type=PHRASE_BREAK,
            severity="medium",
            original=cap.flat_text,
            message=message,
            suggestion=suggestion or "Move the line break so the phrase stays together.",
            confidence=confidence,
        ))

    return issues
