"""Command line entry point.

    python -m src.main --srt captions.srt --client E8D [--video video.mp4]
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import warnings
from pathlib import Path
from typing import Sequence

from src.checks import CHECKS
from src.checks.context import CheckContext
from src.config import check_settings, load_client, load_config, load_yaml, resolve_path
from src.inputs.srt_loader import SrtError, load_srt
from src.audio.transcribe import get_transcript
from src.models import Caption, Issue
from src.utils.dictionary import load_word_list
from src.utils.glossary import Glossary
from src.utils.timecode import format_timestamp


def load_examples(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def build_context(config: dict, client: dict) -> CheckContext:
    """Everything the checks share: glossary (glossary.txt + the client's "glossary:" list),
    word lists, every check's merged settings, and for the punctuation check the rules,
    real examples and API cache location."""
    glossary = Glossary.load(resolve_path(config, "glossary_file"), extra=client.get("glossary") or [])
    settings = {name: check_settings(config, client, name) for name in CHECKS}
    language = settings.get("spelling", {}).get("language", "en")
    extra_words = load_word_list(resolve_path(config, "extra_words_file"))

    rules_path = resolve_path(config, "rules_file")
    rules = load_yaml(rules_path).get("rules", []) if rules_path.is_file() else []
    examples = load_examples(resolve_path(config, "examples_file"))
    if settings.get("punctuation", {}).get("enabled") and not (rules and examples):
        warnings.warn("rules.yaml or data/examples.csv is missing, so the punctuation prompt has no "
                      "rules or real examples. Run: python scripts/prepare_data.py", stacklevel=2)

    return CheckContext(glossary=glossary, language=language, extra_words=extra_words, settings=settings,
                        client=client, rules=rules, examples=examples,
                        llm_cache_file=resolve_path(config, "llm_cache_file"))


def run_checks(captions: Sequence[Caption], config: dict, client: dict,
               context: CheckContext | None = None) -> list[Issue]:
    context = context or build_context(config, client)
    issues: list[Issue] = []
    for name, check in CHECKS.items():
        settings = context.settings_for(name)
        if settings.get("enabled", False):
            min_confidence = settings.get("min_confidence", 0.0)
            issues.extend(i for i in check(captions, settings, context) if i.confidence >= min_confidence)
    return sorted(issues, key=lambda issue: (issue.timestamp, issue.caption_id))


def format_llm_summary(context: CheckContext) -> str | None:
    """One line on what the punctuation check did and roughly what it cost."""
    stats = context.stats.get("punctuation")
    if not stats:
        return None
    line = f"punctuation: {stats['windows']} captions, {stats['cached']} answered from cache"
    usage = getattr(context.llm, "usage", None)
    if usage is not None and usage.requests:
        prices = context.settings_for("punctuation").get("price_per_mtok", {})
        line += (f", {usage.requests} API request{'s' if usage.requests != 1 else ''}"
                 f", about ${usage.cost(prices):.4f}")
    if stats.get("failed"):
        line += f", {stats['failed']} not reviewed (see warnings)"
    if stats.get("skipped"):
        line += f" (skipped: {stats['skipped']})"
    return line


def format_issues(issues: Sequence[Issue], header: str) -> str:
    lines = [header, ""]
    if not issues:
        lines.append("No issues found.")
    for issue in issues:
        lines.append(f"{format_timestamp(issue.timestamp)}  #{issue.caption_id:<4} "
                     f"{issue.type:<14} {issue.severity:<6} \"{issue.original}\"")
        lines.append(f"    {issue.message}")
        if issue.suggestion:
            lines.append(f"    -> {issue.suggestion}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.main", description="Video QC Bot: caption checks.")
    parser.add_argument("--srt", required=True, type=Path, help="caption file (.srt)")
    parser.add_argument("--client", help="client code, e.g. E8D (loads clients/E8D.yaml)")
    parser.add_argument("--config", type=Path, default=None, help="config file (default: config.yaml)")
    parser.add_argument("--json", action="store_true", help="print issues as JSON instead of text")
    parser.add_argument("--no-llm", action="store_true",
                        help="skip the punctuation check (no Anthropic API calls, no cost)")
    parser.add_argument("--video", type=Path, default=None,
                        help="the video, to compare captions with what is said (Whisper; cached per video)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # Caption text can contain curly quotes and em dashes; don't crash on a cp1252 console.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    warnings.formatwarning = lambda msg, *_args, **_kw: f"warning: {msg}\n"

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        client = load_client(config, args.client)
        captions = load_srt(args.srt)
    except (OSError, SrtError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    context = build_context(config, client)
    if args.no_llm:
        context.settings["punctuation"] = {**context.settings_for("punctuation"), "enabled": False}
    if args.video is not None and context.settings_for("transcript").get("enabled", False):
        try:
            context.transcript = get_transcript(
                args.video, config.get("machine", {}), context.settings_for("transcript"),
                context.glossary.all_terms(), resolve_path(config, "transcript_cache_dir"),
                resolve_path(config, "models_dir"))
        except (OSError, RuntimeError) as exc:      # missing video, no ffmpeg, unreadable audio
            print(f"error: {exc}", file=sys.stderr)
            return 2
    issues = run_checks(captions, config, client, context)

    if args.json:
        print(json.dumps([issue.to_dict() for issue in issues], indent=2, ensure_ascii=False))
    else:
        header = (f"{args.srt.name} | client {client.get('client') or 'default'} | "
                  f"{len(captions)} captions | {len(issues)} issue{'s' if len(issues) != 1 else ''}")
        print(format_issues(issues, header))
    summary = format_llm_summary(context)
    if summary:
        print(summary, file=sys.stderr)   # stderr, so --json output stays clean
    return 0


if __name__ == "__main__":
    sys.exit(main())
