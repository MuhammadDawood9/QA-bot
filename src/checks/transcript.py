"""Caption text compared with what is actually said (Whisper transcript).

Each caption is lined up against the spoken words in its time window (caption start - 1 s to
caption end + 1 s). Spoken words at the edges of the window that belong to neighbouring captions
are not counted against this caption. Flags:

  wrong_word     the caption has a different word than the audio ("Are" for "Or")
  missing_word   the audio has a word the caption leaves out ("but")
  extra_word     the caption has a word that isn't said, including a duplicated word ("to to")
  caption_early  the caption is on screen well before its first word is spoken
  caption_late   the caption comes up well after its first word is spoken
  caption_sync   every caption is shifted by about the same amount (reported once for the video)
  uncaptioned_speech  speech with no caption on screen at all

Sync first: the captions are tried at every shift up to max_offset_sec and the shift where the
most caption words line up with spoken words wins. If that shift is clearly better than none,
it is reported once and the word comparison and the per-caption early/late checks are done with
the captions moved back by it, so one sync problem doesn't hide every wrong word or flag every
caption as late.

Only words Whisper heard clearly (probability at or above min_word_probability) are used as
evidence, and every issue shows both texts: caption says X, audio sounds like Y.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, replace
from typing import Sequence

from rapidfuzz import fuzz

from src.checks.context import CheckContext
from src.models import (CAPTION_EARLY, CAPTION_LATE, CAPTION_SYNC, EXTRA_WORD, MISSING_WORD,
                        UNCAPTIONED_SPEECH, WRONG_WORD, Caption, Issue, SpokenWord)
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
    if (context.stats.get("speech") or {}).get("reason"):
        return []                       # too little speech to compare: the report says why instead
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

    issues: list[Issue] = []
    original = {c.id: c for c in captions}
    offset = estimate_offset(captions, spoken, word_of, settings, fillers, equivalents)
    if offset is not None:
        issues.append(_sync_issue(captions, offset))
        # Compare with the captions where they should be; issues still point at the real times.
        captions = [replace(c, start=c.start - offset.seconds, end=c.end - offset.seconds) for c in captions]
    shift_note = (f" (after allowing for the {abs(offset.seconds):.1f}s shift of all captions)"
                  if offset is not None else "")

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
        pairs = _reach_back(pairs, cap, [t.text for t in cap_tokens], spoken, word_of, claimed, same,
                            float(settings.get("late_lookback_sec", 5.0)))
        claimed.update(p.sj for p in pairs if p.sj is not None)
        aligned.append(_Aligned(cap, cap_tokens, pairs))

    # Speech no caption accounts for. Those words then aren't also reported as missing from the
    # caption next to them.
    uncaptioned, reported = _uncaptioned_speech(captions, spoken, word_of, spoken_text, settings,
                                                min_prob, offset, claimed)
    issues += uncaptioned
    claimed |= reported

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

        issues += _timing_issues(cap, original[cap.id], pairs, word_of, settings, min_prob, shift_note)

    # Issues were found on the shifted captions; point them at the captions' real times.
    return [i if i.caption_id not in original or i.type in (CAPTION_SYNC,)
            else replace(i, timestamp=original[i.caption_id].start) for i in issues]


def _reach_back(pairs: list[Pair], cap: Caption, cap_tokens: list[str], spoken: list[Token],
                word_of: list[SpokenWord], claimed: set[int], same, lookback: float) -> list[Pair]:
    """A caption that comes up late starts after its first words were said, so they fall before
    its comparison window and look like extra words. Match the caption's unmatched leading words
    with the words said just before, if no earlier caption used them and they were said at most
    `lookback` seconds before the caption appeared."""
    first = next((n for n, p in enumerate(pairs) if p.sj is not None), None)
    if first is None or not all(p.ci is not None for p in pairs[:first]):
        return pairs
    pairs = list(pairs)
    k = pairs[first].sj - 1
    for n in range(first - 1, -1, -1):
        if (k < 0 or k in claimed or word_of[k].start < cap.start - lookback
                or not same(cap_tokens[pairs[n].ci], spoken[k].text)):
            break
        pairs[n] = Pair(pairs[n].ci, k, True)
        k -= 1
    return pairs


def _timing_issues(cap: Caption, real: Caption, pairs: list[Pair], word_of: list[SpokenWord],
                   settings: dict, min_prob: float, shift_note: str) -> list[Issue]:
    """caption_early / caption_late, from the caption's first word when it matches the audio."""
    first_pair = pairs[0] if pairs else None
    lead_pair = next((p for p in pairs if p.ci is not None), None)
    if not (lead_pair and lead_pair.equal and lead_pair.ci == 0 and first_pair is lead_pair):
        return []
    word = word_of[lead_pair.sj]
    if word.probability < min_prob:
        return []
    lead = word.start - cap.start                     # > 0: caption before the word; < 0: after it
    # Where the caption should start, in the video's real timeline (the shift, if any, added back).
    fix_at = word.start + (real.start - cap.start)
    if lead >= settings.get("early_threshold_sec", 0.7):
        kind, text = CAPTION_EARLY, f'Caption appears {lead:.1f}s before "{word.text}" is spoken'
        severity = "medium" if lead >= 1.0 else "low"
    elif -lead >= settings.get("late_threshold_sec", 0.7):
        kind, text = CAPTION_LATE, f'Caption appears {-lead:.1f}s after "{word.text}" is spoken'
        severity = "high" if -lead >= 1.5 else "medium"
    else:
        return []
    return [Issue(caption_id=cap.id, timestamp=real.start, type=kind, severity=severity, original=cap.flat_text,
                  message=(f"{text}{shift_note} (caption at {format_timestamp(real.start)}, "
                           f"word at {format_timestamp(fix_at)})."),
                  suggestion=f"Start the caption at {format_timestamp(fix_at)}.",
                  confidence=round(word.probability, 2))]


@dataclass(frozen=True)
class Offset:
    seconds: float           # > 0: captions come up this much after the speech; < 0: before it
    matched: int             # caption words that line up with spoken words at this shift
    matched_unshifted: int   # ... and with no shift
    total: int               # caption words in all


def _match_count(captions: Sequence[Caption], cap_tokens: dict[int, list[str]], middles: list[float],
                 spoken: list[Token], shift: float, pad: float) -> int:
    """Caption words found among the words spoken while the caption (moved back by `shift`) is up."""
    total = 0
    for cap in captions:
        lo = bisect.bisect_left(middles, cap.start - shift - pad)
        hi = bisect.bisect_right(middles, cap.end - shift + pad)
        heard = [spoken[k].text for k in range(lo, hi)]
        for token in cap_tokens[cap.id]:
            if token in heard:
                heard.remove(token)
                total += 1
    return total


def estimate_offset(captions: Sequence[Caption], spoken: list[Token], word_of: list[SpokenWord],
                    settings: dict, fillers=(), equivalents=None) -> Offset | None:
    """The shift that lines the captions up with the speech, when it is clearly better than none.

    Tries every shift from -max_offset_sec to +max_offset_sec in 0.1 s steps. Reported only when
    the best shift is at least sync_threshold_sec, at least min_sync_match_fraction of the caption
    words line up at it, and it matches clearly more words than no shift does. Captions that are
    each a little off in different directions don't produce a shift; that's per-caption timing."""
    max_offset = float(settings.get("max_offset_sec", 5.0))
    threshold = float(settings.get("sync_threshold_sec", 0.7))
    if max_offset <= 0 or not captions:
        return None
    pad = 0.3
    cap_tokens = {c.id: [t.text for t in tokenize(c.flat_text.split(), fillers, equivalents or {})] for c in captions}
    total = sum(len(t) for t in cap_tokens.values())
    if not total:
        return None
    middles = [w.middle for w in word_of]
    steps = int(round(max_offset * 10))
    scores = {s / 10: _match_count(captions, cap_tokens, middles, spoken, s / 10, pad)
              for s in range(-steps, steps + 1)}
    best = max(scores.values())
    # Among the shifts with the best score, the middle one (the plateau's centre), then the smallest.
    plateau = sorted(s for s, v in scores.items() if v == best)
    shift = plateau[len(plateau) // 2]
    unshifted = scores[0.0]
    if (abs(shift) < threshold or best < settings.get("min_sync_match_fraction", 0.4) * total
            or best < unshifted * 1.5 or best - unshifted < 3):
        return None
    return Offset(seconds=round(shift, 1), matched=best, matched_unshifted=unshifted, total=total)


def _sync_issue(captions: Sequence[Caption], offset: Offset) -> Issue:
    first = min(captions, key=lambda c: (c.start, c.id))
    late = offset.seconds > 0
    direction = "after" if late else "before"
    return Issue(
        caption_id=first.id, timestamp=first.start, type=CAPTION_SYNC, severity="high",
        original=first.flat_text,
        message=(f"All captions are about {abs(offset.seconds):.1f}s {'late' if late else 'early'}: they come up "
                 f"{abs(offset.seconds):.1f}s {direction} the words are spoken. Shifted by that much, "
                 f"{offset.matched} of {offset.total} caption words line up with the audio "
                 f"(without the shift: {offset.matched_unshifted})."),
        suggestion=f"Move every caption {abs(offset.seconds):.1f}s {'earlier' if late else 'later'}.",
        confidence=round(min(1.0, offset.matched / offset.total + 0.2), 2),
    )


def _uncaptioned_speech(captions: Sequence[Caption], spoken: list[Token], word_of: list[SpokenWord],
                        spoken_text: list[str], settings: dict, min_prob: float,
                        offset: Offset | None, claimed: set[int]) -> tuple[list[Issue], set[int]]:
    """Stretches of clearly heard speech with no caption on screen and not matched to any
    caption's words. Each stretch gets its own negative caption id (-1, -2, ...), since there is
    no caption to point at. Also returns the spoken word indices that were reported."""
    min_sec = float(settings.get("uncaptioned_min_sec", 1.5))
    min_words = int(settings.get("uncaptioned_min_words", 4))
    max_pause = float(settings.get("uncaptioned_max_pause_sec", 1.5))
    if min_sec <= 0 and min_words <= 0:
        return [], set()
    spans = sorted((c.start, c.end) for c in captions if c.words)
    starts = [s for s, _ in spans]

    def covered(t: float) -> bool:
        k = bisect.bisect_right(starts, t) - 1
        return any(spans[j][0] <= t <= spans[j][1] for j in range(max(0, k - 3), k + 1))

    runs: list[list[int]] = []
    for k, w in enumerate(word_of):
        if k in claimed or covered(w.middle):
            if runs and runs[-1]:
                runs.append([])
            continue
        if runs and runs[-1] and w.start - word_of[runs[-1][-1]].end > max_pause:
            runs.append([])
        if not runs:
            runs.append([])
        runs[-1].append(k)

    shift = offset.seconds if offset is not None else 0.0
    issues, reported = [], set()
    for run in (r for r in runs if r):
        clear = [k for k in run if word_of[k].probability >= min_prob]
        start, end = word_of[run[0]].start, word_of[run[-1]].end
        if len(clear) < max(1, len(run) // 2) or not (end - start >= min_sec and len(run) >= min_words):
            continue
        sources = sorted({spoken[k].source for k in run})
        text = " ".join(spoken_text[sources[0]:sources[-1] + 1])
        shown = text if len(text.split()) <= 40 else " ".join(text.split()[:40]) + " ..."
        duration = end - start
        note = (f" (where the captions would be once moved {abs(shift):.1f}s to line up with the audio)"
                if shift else "")
        issues.append(Issue(
            caption_id=-(len(issues) + 1), timestamp=round(start, 3), type=UNCAPTIONED_SPEECH,
            severity="high" if duration >= 3.0 else "medium", original="(no caption)",
            message=(f"{len(run)} spoken words with no caption, {format_timestamp(start)} to "
                     f"{format_timestamp(end)} ({duration:.1f}s){note}."),
            suggestion=f'Add captions: "{shown}"',
            confidence=round(sum(word_of[k].probability for k in clear) / len(clear), 2),
        ))
        reported.update(run)
    return issues, reported


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
