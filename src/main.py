"""Command line entry point. A thin wrapper around src.engine.run_qc, which the desktop app uses too.

    python -m src.main --video video.mp4 --client E8D          (captions read from the video)
    python -m src.main --srt captions.srt --client E8D [--video video.mp4]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import warnings
from pathlib import Path
from typing import Sequence

from src.checks.context import CheckContext
from src.engine import (QCError, QCOptions, build_context, mark_ocr_doubt, run_checks, run_qc,  # noqa: F401
                        write_report)
from src.inputs.srt_loader import write_srt
from src.models import Issue
from src.utils.progress import Cancelled
from src.utils.timecode import format_timestamp


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
        cid = f"#{issue.caption_id}" if issue.caption_id > 0 else "-"     # negative: no caption / a shot
        lines.append(f"{format_timestamp(issue.timestamp)}  {cid:<5} "
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
    parser.add_argument("--no-color", action="store_true", help="skip the color grading checks")
    parser.add_argument("--no-delivery", action="store_true",
                        help="skip the delivery checks (format, loudness, clipping, black / frozen / flash frames)")
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


class TerminalProgress:
    """Prints a line when a stage starts and at every 10% of the whole run, on stderr."""

    def __init__(self):
        self.stage, self.next_mark = None, 0.1

    def __call__(self, stage: str, fraction: float) -> None:
        if stage != self.stage:
            self.stage = stage
            print(f"{stage}...", file=sys.stderr)
        if fraction >= self.next_mark:
            print(f"  {fraction:.0%} done", file=sys.stderr)
            self.next_mark = (int(fraction * 10) + 1) / 10


def setup_logging() -> None:
    """Warnings and errors from everything; the engine's progress messages ("transcript: from
    cache") as plain lines."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    qcbot = logging.getLogger("qcbot")
    qcbot.setLevel(logging.INFO)
    qcbot.propagate = False                  # its own handler below, so nothing is printed twice
    if not any(getattr(h, "qcbot_cli", False) for h in qcbot.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_PlainInfo())
        handler.qcbot_cli = True
        qcbot.addHandler(handler)


class _PlainInfo(logging.Formatter):
    """Progress messages as plain lines; warnings and errors with their level in front."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        return message if record.levelno == logging.INFO else f"{record.levelname}: {message}"


def main(argv: Sequence[str] | None = None) -> int:
    # Caption text can contain curly quotes and em dashes; don't crash on a cp1252 console.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    warnings.formatwarning = lambda msg, *_args, **_kw: f"warning: {msg}\n"
    setup_logging()

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.srt is None and args.video is None:
        parser.error("give --video (captions are read from it) or --srt")

    if args.config is None:
        from src.shared import current_floor

        floor = current_floor()
        if floor.shared and not floor.sync():
            print(f"warning: {floor.status_text()}", file=sys.stderr)
    options = QCOptions(srt_path=args.srt, use_llm=not args.no_llm, check_color=not args.no_color,
                        disabled_checks=frozenset({"delivery"}) if args.no_delivery else frozenset(),
                        config_path=args.config)
    try:
        result = run_qc(args.video, args.client, TerminalProgress(), options=options)
    except QCError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (Cancelled, KeyboardInterrupt):
        print("cancelled", file=sys.stderr)
        return 130

    if args.save_srt is not None:
        print(f"captions saved: {write_srt(result.captions, args.save_srt)}", file=sys.stderr)
    if args.json:
        print(json.dumps([issue.to_dict() for issue in result], indent=2, ensure_ascii=False))
    else:
        source = args.srt.name if args.srt is not None else f"{args.video.name} (captions read from the video)"
        header = (f"{source} | client {result.client.get('client') or 'default'} | "
                  f"{len(result.captions)} captions | {len(result)} issue{'s' if len(result) != 1 else ''}")
        print(format_issues(result, header))
    summary = format_llm_summary(result.context)
    if summary:
        print(summary, file=sys.stderr)   # stderr, so --json output stays clean

    if args.report:
        for path in write_report(result):
            print(f"report: {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
