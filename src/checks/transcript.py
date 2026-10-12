"""Caption text compared with what is actually said (Whisper transcript).

Each caption is lined up against the spoken words in its time window (caption start - 1 s to
caption end + 1 s). Spoken words at the edges of the window that belong to neighbouring captions
are not counted against this caption. Flags:

  wrong_word     the caption has a different word than the audio ("Are" for "Or")
  missing_word   the audio has a word the caption leaves out ("but")
  extra_word     the caption has a word that isn't said, including a duplicated word ("to to")
  caption_early  the caption is on screen well before its first word is spoken

Only words Whisper heard clearly (probability at or above min_word_probability) are used as
evidence, and every issue shows both texts: caption says X, audio sounds like Y.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from rapidfuzz import fuzz

from src.checks.context import CheckContext
from src.models import CAPTION_EARLY, EXTRA_WORD, MISSING_WORD, WRONG_WORD, Caption, Issue, SpokenWord
from src.utils.normalize import Token, tokenize
from src.utils.timecode import format_timestamp


@dataclass(frozen=True)
class Pair:
    ci: int | None      # caption token index, or None for a spoken word the caption lacks
    sj: int | None      # spoken token index (global), or None for a caption word nobody said
    equal: bool


def align(cap: Sequence[str], spoken: Sequence[str], same) -> list[tuple[int | None, int | None, bool]]:
    """Line up caption tokens with a stretch of spoken tokens.

    Every caption token is used; spoken tokens before the first and after the last aligned token
    are free (they belong to neighbouring captions). Fewest edits wins; on a tie, the alignment with
    more matching words, and then a substitution over a gap, so "Are you" vs "or you" reads as
    one wrong word, not one extra plus one missing word.
    Returns (caption index, window index, equal) triples in order.
    """
    n, m = len(cap), len(spoken)
    # cost[i][j] = (edits, -matches) for cap[:i] against spoken[..j]
    cost = [[(0, 0)] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        cost[i][0] = (i, 0)
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            eq = same(cap[i - 1], spoken[j - 1])
            d_e, d_m = cost[i - 1][j - 1]
            diag = (d_e + (0 if eq else 1), d_m - (1 if eq else 0))
            up = (cost[i - 1][j][0] + 1, cost[i - 1][j][1])
            left = (cost[i][j - 1][0] + 1, cost[i][j - 1][1])
            cost[i][j] = min(diag, up, left)

    j = min(range(m + 1), key=lambda k: (cost[n][k], k))
    i, out = n, []
    while i > 0:
        if j > 0:
            eq = same(cap[i - 1], spoken[j - 1])
            d_e, d_m = cost[i - 1][j - 1]
            if cost[i][j] == (d_e + (0 if eq else 1), d_m - (1 if eq else 0)):
                out.append((i - 1, j - 1, eq))
                i, j = i - 1, j - 1
                continue
        if cost[i][j] == (cost[i - 1][j][0] + 1, cost[i - 1][j][1]):
            out.append((i - 1, None, False))
            i -= 1
            continue
        out.append((None, j - 1, False))
        j -= 1
    out.reverse()
    return out


@dataclass
class _Aligned:
    cap: Caption
    tokens: list[Token]
    pairs: list[Pair]


def _phrase(words: Sequence[str], indices: set[int], context: int = 1) -> str:
    if not indices:
        return ""
    a, b = max(0, min(indices) - context), min(len(words) - 1, max(indices) + context)
    return " ".join(words[a:b + 1])


def check(captions: Sequence[Caption], settings: dict, context: CheckContext | None = None) -> list[Issue]:
    if not settings.get("enabled", True) or context is None or context.transcript is None:
        return []
    words: list[SpokenWord] = context.transcript
    fillers = settings.get("fillers", [])
    equivalents = settings.get("equivalents", {})
    min_prob = settings.get("min_word_probability", 0.7)
    pad = settings.get("window_padding_sec", 1.0)
    ratio = settings.get("same_word_min_ratio", 85)
    edge_slack = settings.get("edge_slack_sec", 0.2)

    def same(a: str, b: str) -> bool:
        return a == b or (min(len(a), len(b)) >= 4 and fuzz.ratio(a, b) >= ratio)

    spoken = tokenize([w.text for w in words], fillers, equivalents)
    if not spoken:
        return []
    word_of = [words[t.source] for t in spoken]
    spoken_text = [w.text for w in words]

    # Pass 1: align every caption and note which spoken words each one accounts for.
    aligned: list[_Aligned] = []
    claimed: set[int] = set()
    for cap in sorted(captions, key=lambda c: (c.start, c.id)):
        cap_tokens = tokenize(cap.flat_text.split(), fillers, equivalents)
        if not cap_tokens:
            continue
        window = [k for k, w in enumerate(word_of) if w.end >= cap.start - pad and w.start <= cap.end + pad]
        if not window:
            continue
        lo = window[0]
        triples = align([t.text for t in cap_tokens], [spoken[k].text for k in window], same)
        pairs = [Pair(ci, None if sj is None else lo + sj, eq) for ci, sj, eq in triples]
        matched = sum(p.equal for p in pairs)
        if matched / len(cap_tokens) < settings.get("min_matched_fraction", 0.5):
            continue          # not said at all (a title, on-screen text, or music): nothing to compare
        claimed.update(p.sj for p in pairs if p.sj is not None)
        aligned.append(_Aligned(cap, cap_tokens, pairs))

    issues: list[Issue] = []
    for a in aligned:
        cap, pairs = a.cap, list(a.pairs)
        cap_words = cap.flat_text.split()

        # Unclaimed spoken words right before / after this caption, said while it is on screen,
        # are words the caption leaves out at its start or end.
        first = min(p.sj for p in pairs if p.sj is not None)
        last = max(p.sj for p in pairs if p.sj is not None)
        k = first - 1
        while k >= 0 and k not in claimed and word_of[k].middle >= cap.start - edge_slack:
            pairs.insert(0, Pair(None, k, False))
            k -= 1
        k = last + 1
        while k < len(spoken) and k not in claimed and word_of[k].middle <= cap.end + edge_slack:
            pairs.append(Pair(None, k, False))
            k += 1

        span = sorted({spoken[p.sj].source for p in pairs if p.sj is not None})
        audio_text = " ".join(spoken_text[span[0]:span[-1] + 1]) if span else ""
        issues += _block_issues(cap, a.tokens, pairs, spoken, word_of, spoken_text, cap_words,
                                audio_text, min_prob, settings)

        first_pair = pairs[0] if pairs else None
        lead_pair = next((p for p in pairs if p.ci is not None), None)
        if lead_pair and lead_pair.equal and lead_pair.ci == 0 and first_pair is lead_pair:
            word = word_of[lead_pair.sj]
            lead = word.start - cap.start
            if lead >= settings.get("early_threshold_sec", 0.7) and word.probability >= min_prob:
                issues.append(Issue(
                    caption_id=cap.id, timestamp=cap.start, type=CAPTION_EARLY,
                    severity="medium" if lead >= 1.0 else "low", original=cap.flat_text,
                    message=(f'Caption appears {lead:.1f}s before "{word.text}" is spoken '
                             f"(caption at {format_timestamp(cap.start)}, word at {format_timestamp(word.start)})."),
                    suggestion=f"Start the caption at {format_timestamp(word.start)}.",
                    confidence=round(word.probability, 2),
                ))
    return issues


def _block_issues(cap, cap_tokens, pairs, spoken, word_of, spoken_text, cap_words, audio_text,
                  min_prob, settings) -> list[Issue]:
    issues = []
    i = 0
    while i < len(pairs):
        if pairs[i].equal:
            i += 1
            continue
        j = i
        while j < len(pairs) and not pairs[j].equal:
            j += 1
        block, before, after = pairs[i:j], pairs[i - 1] if i > 0 else None, pairs[j] if j < len(pairs) else None
        i = j

        cap_idx = [p.ci for p in block if p.ci is not None]
        sp_idx = [p.sj for p in block if p.sj is not None]
        cap_tok = [cap_tokens[c].text for c in cap_idx]
        sp_tok = [spoken[s].text for s in sp_idx]
        if "".join(cap_tok) == "".join(sp_tok):
            continue                                 # "every day" vs "everyday"

        if sp_idx:                                   # every word the audio is said to contain must be clear
            evidence = [word_of[s].probability for s in sp_idx]
            strength = min(evidence) if evidence else 0.0
        else:
            # Extra caption words have no spoken word to score; use how clearly the words on either
            # side were heard (their average, so one shaky neighbour doesn't hide the gap).
            evidence = [word_of[p.sj].probability for p in (before, after) if p is not None and p.sj is not None]
            strength = sum(evidence) / len(evidence) if evidence else 0.0
        if strength < min_prob:
            continue
        confidence = round(strength, 2)

        anchors = [p for p in (before, after) if p is not None]
        cap_src = {cap_tokens[c].source for c in cap_idx} | {cap_tokens[p.ci].source for p in anchors if p.ci is not None}
        sp_src = {spoken[s].source for s in sp_idx} | {spoken[p.sj].source for p in anchors if p.sj is not None}
        # The changed words plus the matching word on each side, from both texts.
        said_cap = _phrase(cap_words, cap_src, context=0)
        said_audio = _phrase(spoken_text, sp_src, context=0)
        both = f'caption says "{said_cap}", audio sounds like "{said_audio}".'
        suggestion = f'Audio: "{audio_text}"'

        if cap_idx and sp_idx:
            kind, label = WRONG_WORD, "Wrong word"
        elif sp_idx:
            if settings.get("ignore_spoken_repeats", True) and _is_spoken_repeat(sp_idx, spoken):
                continue                             # a stutter ("to to"): not a caption mistake
            kind, label = MISSING_WORD, f'Missing word{"s" if len(sp_tok) > 1 else ""} "{" ".join(sp_tok)}"'
        else:
            repeated = any((c > 0 and cap_tokens[c - 1].text == cap_tokens[c].text)
                           or (c + 1 < len(cap_tokens) and cap_tokens[c + 1].text == cap_tokens[c].text)
                           for c in cap_idx)
            kind = EXTRA_WORD
            label = f'Extra word{"s" if len(cap_tok) > 1 else ""} "{" ".join(cap_tok)}"' + (" (repeated)" if repeated else "")

        severity = "high" if confidence >= 0.85 else "medium"
        issues.append(Issue(caption_id=cap.id, timestamp=cap.start, type=kind, severity=severity,
                            original=cap.flat_text, message=f"{label}: {both}", suggestion=suggestion,
                            confidence=confidence))
    return issues


def _is_spoken_repeat(sp_idx: list[int], spoken: list[Token]) -> bool:
    """The left-out spoken words just repeat the words next to them (a stutter or false start)."""
    for s in sp_idx:
        neighbours = [spoken[k].text for k in (s - 1, s + 1) if 0 <= k < len(spoken)]
        if spoken[s].text not in neighbours:
            return False
    return True
