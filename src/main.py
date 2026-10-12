"""Command line entry point.

    python -m src.main --video video.mp4 --client E8D          (captions read from the video)
    python -m src.main --srt captions.srt --client E8D [--video video.mp4]
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import warnings
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from src.checks import CHECKS
from src.checks.context import CheckContext
from src.config import check_settings, load_client, load_config, load_yaml, ocr_settings, resolve_path
from src.inputs.srt_loader import SrtError, load_srt, write_srt
from src.inputs.video_ocr import OcrError, read_burned_in_captions
from src.audio.transcribe import get_transcript
from src.models import (CAPITALIZATION, EXTRA_WORD, MISSING_WORD, PUNCTUATION, REPEATED_WORD, SPACING,
                        SPELLING, WRONG_WORD, Caption, Issue)
from src.report.builder import ReportInfo, build_report
from src.utils.dictionary import load_word_list
from src.utils.glossary import Glossary
from src.utils.system import MissingToolError, find_ffmpeg
from src.utils.timecode import format_timestamp

# Issues that depend on reading every letter right, so an OCR misread can cause them.
TEXT_ISSUES = {SPELLING, REPEATED_WORD, CAPITALIZATION, SPACING, PUNCTUATION, WRONG_WORD, MISSING_WORD, EXTRA_WORD}
OCR_DOUBT_NOTE = " (The OCR wasn't sure of this caption's text: check the screenshot before fixing.)"
OCR_WEAK_SPOT_NOTE = " (Read by OCR, which can miss spaces and mix up commas and full stops: check the screenshot.)"


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
    ocr = ocr_settings(config, client)

    rules_path = resolve_path(config, "rules_file")
    rules = load_yaml(rules_path).get("rules", []) if rules_path.is_file() else []
    examples = load_examples(resolve_path(config, "examples_file"))
    if settings.get("punctuation", {}).get("enabled") and not (rules and examples):
        warnings.warn("rules.yaml or data/examples.csv is missing, so the punctuation prompt has no "
                      "rules or real examples. Run: python scripts/prepare_data.py", stacklevel=2)

    return CheckContext(glossary=glossary, language=language, extra_words=extra_words, settings=settings,
                        client=client, rules=rules, examples=examples,
                        llm_cache_file=resolve_path(config, "llm_cache_file"),
                        ocr_low_confidence=float(ocr.get("low_confidence", 0.75)),
                        ocr_always_doubtful=tuple(ocr.get("always_doubtful") or ()))


def run_checks(captions: Sequence[Caption], config: dict, client: dict,
               context: CheckContext | None = None) -> list[Issue]:
    context = context or build_context(config, client)
    issues: list[Issue] = []
    by_id = {c.id: c for c in captions}
    for name, check in CHECKS.items():
        settings = context.settings_for(name)
        if settings.get("enabled", False):
            min_confidence = settings.get("min_confidence", 0.0)
            found = (mark_ocr_doubt(i, by_id.get(i.caption_id), context.ocr_low_confidence,
                                    context.ocr_always_doubtful)
                     for i in check(captions, settings, context))
            issues.extend(i for i in found if i.confidence >= min_confidence)
    return sorted(issues, key=lambda issue: (issue.timestamp, issue.caption_id))


def mark_ocr_doubt(issue: Issue, caption: Caption | None, low_confidence: float,
                   always_doubtful: Sequence[str] = ()) -> Issue:
    """A text issue on a caption read by OCR may be the OCR's mistake, not the editor's: when the
    OCR was unsure of the caption, or the issue is of a type the OCR often gets wrong (spaces,
    commas vs full stops), make it low severity and say so in the message. When the OCR was
    unsure, the issue's confidence is also capped at the OCR's."""
    if caption is None or caption.source != "ocr" or issue.type not in TEXT_ISSUES:
        return issue
    unsure = caption.ocr_confidence is not None and caption.ocr_confidence < low_confidence
    if not unsure and issue.type not in always_doubtful:
        return issue
    if unsure:
        return replace(issue, severity="low", confidence=min(issue.confidence, caption.ocr_confidence),
                       message=issue.message + OCR_DOUBT_NOTE)
    return replace(issue, severity="low", message=issue.message + OCR_WEAK_SPOT_NOTE)


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
    parser.add_argument("--srt", type=Path, default=None,
                        help="caption file (.srt); leave it out to read the captions burned into --video")
    parser.add_argument("--client", help="client code, e.g. E8D (loads clients/E8D.yaml)")
    parser.add_argument("--config", type=Path, default=None, help="config file (default: config.yaml)")
    parser.add_argument("--json", action="store_true", help="print issues as JSON instead of text")
    parser.add_argument("--no-llm", action="store_true",
                        help="skip the punctuation check (no Anthropic API calls, no cost)")
    parser.add_argument("--video", type=Path, default=None,
                        help="the video: captions are read from it (OCR) when there is no --srt, and compared "
                             "with what is said (Whisper); both cached per video")
    parser.add_argument("--save-srt", type=Path, default=None, metavar="FILE",
                        help="also save the captions as an .srt (useful to see what the OCR read)")
    parser.add_argument("--report", action="store_true",
                        help="also write a report folder (report.md, report.docx, screenshots, issues.csv) under reports/")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # Caption text can contain curly quotes and em dashes; don't crash on a cp1252 console.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    warnings.formatwarning = lambda msg, *_args, **_kw: f"warning: {msg}\n"

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.srt is None and args.video is None:
        parser.error("give --video (captions are read from it) or --srt")
    try:
        config = load_config(args.config)
        client = load_client(config, args.client)
        if args.srt is not None:
            captions = load_srt(args.srt)
        else:
            captions = read_burned_in_captions(args.video, ocr_settings(config, client),
                                               resolve_path(config, "ocr_cache_dir"))
        if args.save_srt is not None:
            print(f"captions saved: {write_srt(captions, args.save_srt)}", file=sys.stderr)
    except (OSError, SrtError, OcrError) as exc:
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
            context.stats["transcript"] = {"model": config.get("machine", {}).get("whisper_model_size", "base")}
        except (OSError, RuntimeError) as exc:      # missing video, no ffmpeg, unreadable audio
            print(f"error: {exc}", file=sys.stderr)
            return 2
    issues = run_checks(captions, config, client, context)

    if args.json:
        print(json.dumps([issue.to_dict() for issue in issues], indent=2, ensure_ascii=False))
    else:
        header = (f"{caption_source_name(args)} | client {client.get('client') or 'default'} | "
                  f"{len(captions)} captions | {len(issues)} issue{'s' if len(issues) != 1 else ''}")
        print(format_issues(issues, header))
    summary = format_llm_summary(context)
    if summary:
        print(summary, file=sys.stderr)   # stderr, so --json output stays clean

    if args.report:
        for path in write_report(captions, issues, config, client, context, args):
            print(f"report: {path}", file=sys.stderr)
    return 0


def caption_source_name(args) -> str:
    return args.srt.name if args.srt is not None else f"{args.video.name} (captions read from the video)"


def check_status_lines(context: CheckContext, args) -> list[str]:
    """For the report header: which checks ran, and why any were skipped."""
    names = {"too_short": "Too short / phrase break", "spelling": "Spelling and repeated words",
             "capitalization": "Capitalization", "spacing": "Spacing",
             "punctuation": "Punctuation (Anthropic API)", "transcript": "Captions vs audio (Whisper)"}
    lines = []
    for name in CHECKS:
        settings = context.settings_for(name)
        title = names.get(name, name)
        if name == "punctuation":
            stats = context.stats.get("punctuation", {})
            if args.no_llm:
                lines.append(f"{title}: skipped (--no-llm)")
            elif not settings.get("enabled"):
                lines.append(f"{title}: off in config")
            elif stats.get("skipped"):
                lines.append(f"{title}: skipped ({stats['skipped']})")
            else:
                lines.append(f"{title}: {settings.get('model')}, {stats.get('windows', 0)} captions "
                             f"({stats.get('cached', 0)} from cache, {stats.get('failed', 0)} not reviewed)")
        elif name == "transcript":
            if context.transcript is None:
                lines.append(f"{title}: not run (no --video)")
            else:
                model = context.stats.get("transcript", {}).get("model", "?")
                lines.append(f"{title}: Whisper '{model}', {len(context.transcript)} words heard")
        else:
            lines.append(f"{title}: {'ran' if settings.get('enabled') else 'off in config'}")
    return lines


def write_report(captions, issues, config: dict, client: dict, context: CheckContext, args) -> list[Path]:
    settings = dict(config.get("report", {}))
    settings["output_dir"] = Path(config["_root"]) / settings.get("output_dir", "reports")
    ffmpeg = None
    if args.video is not None and settings.get("include_screenshots", True):
        try:
            ffmpeg = find_ffmpeg(config.get("machine", {}).get("ffmpeg_path"))
        except MissingToolError as exc:
            warnings.warn(f"no screenshots in the report: {exc}", stacklevel=2)
    rules_path = resolve_path(config, "rules_file")
    rules_version = str(load_yaml(rules_path).get("version", "")) if rules_path.is_file() else None
    info = ReportInfo(srt=args.srt, video=args.video, client=client.get("client"),
                      rules_version=rules_version, checks=check_status_lines(context, args))
    return build_report(captions, issues, info, settings, ffmpeg)


if __name__ == "__main__":
    sys.exit(main())
