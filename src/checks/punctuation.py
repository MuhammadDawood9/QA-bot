"""Punctuation check, done by Claude through the Anthropic API.

Each caption is reviewed in a window: the target caption plus the captions around it, with
timings, so the model can tell whether a sentence ends or carries on into the next caption.
Several windows go in one request to keep the cost down. The prompt (Section 7.4 of the plan)
carries the punctuation rules from rules.yaml, the client's own rules, real mistakes from
data/examples.csv and the glossary.

Replies must be JSON (enforced by the API's structured output and parsed defensively here).
A request that fails or a review that can't be read is logged and skipped, never fatal.
Results are cached by a hash of the caption text, so a rerun only pays for captions that changed.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Sequence

import anthropic

from src.checks.context import CheckContext
from src.models import PUNCTUATION, Caption, Issue
from src.utils.cache import JsonCache, text_hash
from src.utils.llm import ClaudeClient, LLMError, MissingApiKeyError
from src.utils.timecode import format_timestamp

log = logging.getLogger("qcbot.punctuation")

# Bump when the prompt or reply format changes, so old cached answers aren't reused.
PROMPT_VERSION = "2"

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "reviews": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target_id": {"type": "integer"},
                    "has_issue": {"type": "boolean"},
                    "issue_type": {"type": "string", "enum": ["punctuation"]},
                    "original": {"type": "string"},
                    "suggestion": {"type": "string"},
                    "explanation": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["target_id", "has_issue", "issue_type", "original",
                             "suggestion", "explanation", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["reviews"],
    "additionalProperties": False,
}

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


@dataclass(frozen=True)
class Window:
    target: Caption
    before: tuple[Caption, ...]
    after: tuple[Caption, ...]

    def render(self, number: int) -> str:
        lines = [f"WINDOW {number}: review ONLY caption #{self.target.id}"]
        for cap in (*self.before, self.target, *self.after):
            mark = ">>" if cap is self.target else "  "
            note = "   <- TARGET" if cap is self.target else ""
            lines.append(f"{mark} #{cap.id} [{format_timestamp(cap.start)} - {format_timestamp(cap.end)}] "
                         f"{cap.flat_text}{note}")
        return "\n".join(lines)

    def cache_text(self) -> str:
        """What the answer depends on: the texts and the pauses between them, not absolute times."""
        caps = (*self.before, self.target, *self.after)
        parts = []
        for prev, cap in zip((None, *caps), caps):
            gap = cap.start - prev.end if prev else 0.0
            role = "T" if cap is self.target else "c"
            parts.append(f"{role}|{gap:.1f}|{cap.flat_text}")
        return "\n".join(parts)


def build_windows(captions: Sequence[Caption], before: int = 2, after: int = 2) -> list[Window]:
    caps = sorted(captions, key=lambda c: (c.start, c.id))
    windows = []
    for i, cap in enumerate(caps):
        if not cap.words:
            continue
        windows.append(Window(cap, tuple(caps[max(0, i - before):i]), tuple(caps[i + 1:i + 1 + after])))
    return windows


def _example_lines(context: CheckContext, settings: dict) -> list[str]:
    categories = set(settings.get("example_categories", ["punctuation"]))
    pattern = re.compile(settings.get("example_filter", "."), re.IGNORECASE)
    lines = []
    for row in context.examples:
        if row.get("category") not in categories:
            continue
        searchable = " ".join(row.get(k, "") for k in ("editor_wrote", "should_be", "comment_text"))
        if not pattern.search(searchable):
            continue
        wrote, should = row.get("editor_wrote", ""), row.get("should_be", "")
        pair = f'"{wrote}" -> "{should}" ' if wrote and should else ""
        lines.append(f'- [{row.get("client", "?")}] {pair}reviewer: "{row.get("comment_text", "")}"')
    return lines[: settings.get("max_examples", 40)]


def build_system_prompt(context: CheckContext, settings: dict) -> str:
    areas = set(settings.get("rule_areas", ["Full stops", "Commas"]))
    rules = [f"- {r['rule']}" for r in context.rules if r.get("area") in areas]
    client = context.client or {}
    client_line = (f"- {client.get('client')}: {client['notes']}" if client.get("notes")
                   else "- (no client-specific rules)")
    examples = _example_lines(context, settings) or ["- (none)"]
    glossary = ", ".join(context.glossary.all_terms()) or "(none)"

    return "\n".join([
        "You are a QC reviewer for video captions.",
        "",
        "RULES:",
        *(rules or ["- (none)"]),
        "",
        "CLIENT RULES:",
        client_line,
        "",
        "EXAMPLES OF REAL MISTAKES (wrong -> correct, with the reviewer's comment):",
        *examples,
        "",
        f"GLOSSARY (never flag these terms): {glossary}",
        "",
        "HOW TO REVIEW:",
        "You will get one or more windows of consecutive captions with timestamps. In each window,",
        "review ONLY the caption marked TARGET. Use the surrounding captions to decide if a sentence",
        "ends or continues: captions often split one sentence across several captions, so a caption",
        "with no full stop is normal when the sentence carries on in the next caption.",
        "Only report punctuation: missing, extra or wrong full stops, commas, question marks and",
        "apostrophes, and a capital letter that only exists because of a wrong full stop.",
        "Do not report spelling, word choice, line breaks or style; other checks cover those.",
        "When unsure, set has_issue to false. Follow the client rules over the general rules.",
        "",
        "For each target return: target_id; has_issue; issue_type \"punctuation\"; original (the target",
        "caption text); suggestion (the full corrected caption); explanation (one short sentence);",
        "confidence (0.0 to 1.0). When has_issue is false, leave original, suggestion and explanation",
        "as empty strings.",
        "Respond with ONLY JSON, no preamble, no markdown fences.",
    ])


def parse_reviews(text: str, expected_ids: set[int]) -> dict[int, dict]:
    """Read the model's JSON reply. Bad items are logged and skipped; bad JSON raises ValueError."""
    data = json.loads(_FENCE.sub("", text.strip()))
    if not isinstance(data, dict) or not isinstance(data.get("reviews"), list):
        raise ValueError("reply has no 'reviews' list")
    out: dict[int, dict] = {}
    for item in data["reviews"]:
        try:
            target_id = int(item["target_id"])
            has_issue = item["has_issue"]
            if not isinstance(has_issue, bool):
                raise TypeError("has_issue is not true/false")
            confidence = min(1.0, max(0.0, float(item.get("confidence", 0.0))))
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("skipped an unreadable review %r: %s", item, exc)
            continue
        if target_id not in expected_ids:
            log.warning("skipped a review for caption #%s, which was not asked about", target_id)
            continue
        out[target_id] = {
            "has_issue": has_issue,
            "suggestion": str(item.get("suggestion", "")),
            "explanation": str(item.get("explanation", "")),
            "confidence": confidence,
        }
    return out


def _to_issue(cap: Caption, review: dict) -> Issue | None:
    if not review["has_issue"]:
        return None
    suggestion = " ".join(review["suggestion"].split())
    if not suggestion or suggestion == cap.flat_text:
        return None
    confidence = review["confidence"]
    return Issue(
        caption_id=cap.id, timestamp=cap.start, type=PUNCTUATION,
        severity="medium" if confidence >= 0.8 else "low",
        original=cap.flat_text,
        message=review["explanation"] or "Punctuation issue.",
        suggestion=suggestion, confidence=confidence,
    )


def check(captions: Sequence[Caption], settings: dict, context: CheckContext | None = None) -> list[Issue]:
    if not settings.get("enabled", True):
        return []
    context = context or CheckContext()
    windows = build_windows(captions, settings.get("window_before", 2), settings.get("window_after", 2))
    if not windows:
        return []

    llm = context.llm or ClaudeClient.from_settings(settings)
    context.llm = llm
    system = build_system_prompt(context, settings)
    prefix = text_hash(PROMPT_VERSION, llm.model, str(llm.effort), system)
    cache = JsonCache(context.llm_cache_file)
    keys = {w.target.id: text_hash(prefix, w.cache_text()) for w in windows}

    todo = [w for w in windows if keys[w.target.id] not in cache]
    stats = context.stats.setdefault("punctuation", {"windows": len(windows), "cached": 0, "failed": 0})
    stats["cached"] = len(windows) - len(todo)

    per_request = max(1, int(settings.get("windows_per_request", 10)))
    for start in range(0, len(todo), per_request):
        if context.progress is not None:
            context.progress(start / len(todo))          # API calls are slow: report, and allow a cancel
        batch = todo[start:start + per_request]
        user = "CAPTIONS:\n\n" + "\n\n".join(w.render(n) for n, w in enumerate(batch, start=1))
        expected = {w.target.id for w in batch}
        try:
            reviews = parse_reviews(llm.complete_json(system, user, REVIEW_SCHEMA), expected)
        except MissingApiKeyError as exc:
            log.warning("%s", exc)
            stats["skipped"] = "no API key"
            break
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                anthropic.NotFoundError) as exc:
            log.warning("punctuation check stopped: %s", exc)   # every later request would fail too
            stats["failed"] += len(todo) - start
            break
        except (LLMError, ValueError, anthropic.APIError) as exc:
            # Refusals, cut-off or unreadable replies, rate limits and network errors that
            # survived the SDK's own retries: log, skip this batch, carry on with the next.
            log.warning("punctuation request for captions #%s-#%s skipped: %s",
                        batch[0].target.id, batch[-1].target.id, exc)
            stats["failed"] += len(batch)
            continue
        for w in batch:
            if w.target.id in reviews:
                cache.set(keys[w.target.id], reviews[w.target.id])
            else:
                log.warning("no review came back for caption #%s; it will be retried next run", w.target.id)
                stats["failed"] += 1
        cache.save()                      # keep progress even if a later request fails

    issues = []
    for w in windows:
        review = cache.get(keys[w.target.id])
        if review is not None:
            issue = _to_issue(w.target, review)
            if issue:
                issues.append(issue)
    return issues
