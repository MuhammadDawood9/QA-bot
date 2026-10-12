"""The QC engine's single entry point. The CLI (src/main.py) and the desktop app both call it.

    result = run_qc("clip.mp4", "E8D", progress_cb=print_progress, cancel_event=stop)
    for issue in result:                   # the result is the issue list, sorted by time
        ...
    docx = build_docx_report(result)       # reports/<video>_<date-time>/<video>_QC_<date>.docx

No printing and no UI here: progress goes to progress_cb(stage_name, overall_fraction 0..1),
messages go to the "qcbot" loggers, and problems are raised:
  QCError     the input can't be checked (missing video, bad SRT, bad config, ffmpeg missing);
              its message is written for the user
  Cancelled   cancel_event was set (src.utils.progress.Cancelled)
"""

from __future__ import annotations

import csv
import logging
import threading
import warnings
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Sequence

from src.audio.transcribe import get_transcript
from src.checks import CHECKS
from src.checks.context import CheckContext
from src.config import (check_settings, color_settings, load_client, load_config, load_yaml, normalize_client_code,
                        ocr_settings, resolve_path)
from src.inputs.srt_loader import SrtError, load_srt
from src.inputs.video_ocr import FULL_FRAME, OcrError, parse_region, read_burned_in_captions, read_screen_text
from src.models import (CAPITALIZATION, EXTRA_WORD, MISSING_WORD, PUNCTUATION, REPEATED_WORD, SCREEN_TEXT_MISMATCH,
                        SPACING, SPELLING, WRONG_WORD, Caption, Issue)
from src.report.builder import ReportInfo, build_report
from src.utils.dictionary import load_word_list
from src.utils.glossary import Glossary
from src.utils.progress import Cancelled, ProgressCallback, Stage
from src.utils.system import MissingToolError, find_ffmpeg
from src.video.color import color_issues, hdr_transfer, read_color

log = logging.getLogger("qcbot.engine")

__all__ = ["run_qc", "build_docx_report", "write_report", "QCResult", "QCOptions", "QCError", "Cancelled",
           "STAGES"]

# Issues that depend on reading every letter right, so an OCR misread can cause them.
TEXT_ISSUES = {SPELLING, REPEATED_WORD, CAPITALIZATION, SPACING, PUNCTUATION, WRONG_WORD, MISSING_WORD, EXTRA_WORD,
               SCREEN_TEXT_MISMATCH}
OCR_DOUBT_NOTE = " (The OCR wasn't sure of this caption's text: check the screenshot before fixing.)"
OCR_WEAK_SPOT_NOTE = " (Read by OCR, which can miss spaces and mix up commas and full stops: check the screenshot.)"

# The stages of a run, in order, with roughly how much of a typical run's time each takes.
# progress_cb gets these names; a stage that doesn't apply (OCR when an SRT is given) is skipped over.
STAGES = [
    ("Reading captions", 0.40),
    ("Reading on-screen text", 0.08),
    ("Transcribing audio", 0.17),
    ("Checking captions", 0.15),
    ("Checking color", 0.20),
]


class QCError(Exception):
    """The video (or caption file, or setup) can't be checked. The message is meant for the user."""


@dataclass
class QCOptions:
    """Everything about a run besides the video and the client."""
    srt_path: Path | None = None             # use this caption file instead of reading the captions off the video
    use_llm: bool = True                     # the punctuation check (Anthropic API, costs money)
    check_color: bool = True
    disabled_checks: frozenset[str] = frozenset()    # names from src.checks.CHECKS, or "color"
    config_path: Path | None = None          # default: config.yaml next to src/


class QCResult(list):
    """The issues (a list, sorted by time), plus what a report or a summary needs."""

    def __init__(self, issues: Sequence[Issue], *, video: Path | None, srt: Path | None, captions: list[Caption],
                 config: dict, client: dict, context: CheckContext, checks: list[str]):
        super().__init__(issues)
        self.video, self.srt, self.captions = video, srt, captions
        self.config, self.client, self.context, self.checks = config, client, context, checks

    @property
    def issues(self) -> list[Issue]:
        return list(self)


# --- the run -------------------------------------------------------------------------------

def run_qc(video_path: str | Path | None, client: str | None = None,
           progress_cb: ProgressCallback | None = None, cancel_event: threading.Event | None = None,
           *, options: QCOptions | None = None, config: dict | None = None) -> QCResult:
    """Run every check on a video and return the issue list.

    video_path   the video; its burned-in captions are read with OCR unless options.srt_path is
                 given. May be None only with an SRT (then only the caption text checks run).
    client       client code ("E8D", "8d"...) for clients/<CODE>.yaml, or None for the defaults.
    progress_cb  called with (stage name, overall fraction 0..1) as the run goes.
    cancel_event when set, the run stops at the next frame or batch and raises Cancelled.
    """
    options = options or QCOptions()
    video = Path(video_path) if video_path is not None else None
    srt = Path(options.srt_path) if options.srt_path is not None else None
    if video is None and srt is None:
        raise QCError("Give a video (its captions are read from the picture) or a caption file.")
    if video is not None and not video.is_file():
        raise QCError(f"Video not found: {video}")
    stages = _stages(progress_cb, cancel_event)

    try:
        config = config if config is not None else load_config(options.config_path)
        client_data = load_client(config, client)
        context = build_context(config, client_data)
        _apply_options(context, options)

        stage = stages["Reading captions"]
        stage(0.0)
        if srt is not None:
            captions = load_srt(srt)
        else:
            captions = read_burned_in_captions(video, ocr_settings(config, client_data),
                                               resolve_path(config, "ocr_cache_dir"), progress=stage)
        stage(1.0)

        stage = stages["Reading on-screen text"]
        stage(0.0)
        if video is not None and context.settings_for("screen_text").get("enabled", False):
            try:
                context.screen_text = read_screen_text(video, ocr_settings(config, client_data), captions,
                                                       resolve_path(config, "ocr_cache_dir"), progress=stage)
            except (OSError, OcrError) as exc:
                warnings.warn(f"on-screen text not read, so names in graphics aren't compared: {exc}", stacklevel=2)
        stage(1.0)

        stage = stages["Transcribing audio"]
        stage(0.0)
        if video is not None and context.settings_for("transcript").get("enabled", False):
            context.transcript = get_transcript(
                video, config.get("machine", {}), context.settings_for("transcript"), context.glossary.all_terms(),
                resolve_path(config, "transcript_cache_dir"), resolve_path(config, "models_dir"), progress=stage)
            context.stats["transcript"] = {"model": config.get("machine", {}).get("whisper_model_size", "base")}
        stage(1.0)

        issues = run_checks(captions, config, client_data, context, stage=stages["Checking captions"])

        stage = stages["Checking color"]
        stage(0.0)
        issues += _color(video, config, client_data, context, options, stage)
        stage(1.0)
    except (Cancelled, QCError):
        raise
    except (SrtError, OcrError, MissingToolError) as exc:
        raise QCError(str(exc)) from exc
    except (OSError, RuntimeError) as exc:      # missing file, unreadable video or audio, ffmpeg failed
        raise QCError(f"{type(exc).__name__}: {exc}") from exc

    issues = sorted(issues, key=lambda i: (i.timestamp, i.caption_id))
    return QCResult(issues, video=video, srt=srt, captions=captions, config=config, client=client_data,
                    context=context, checks=check_status_lines(context, video, options))


def _stages(progress_cb, cancel_event) -> dict[str, Stage]:
    out, start = {}, 0.0
    for name, span in STAGES:
        out[name] = Stage(progress_cb, cancel_event, name, start, span)
        start += span
    return out


def _apply_options(context: CheckContext, options: QCOptions) -> None:
    off = set(options.disabled_checks)
    if not options.use_llm:
        off.add("punctuation")
    for name in off & set(CHECKS):
        context.settings[name] = {**context.settings_for(name), "enabled": False}


def _color(video: Path | None, config: dict, client: dict, context: CheckContext, options: QCOptions,
           stage: Stage) -> list[Issue]:
    """Color grading issues, shot by shot. Burned-in captions are left out of the measurements
    when the client has a caption region."""
    settings = color_settings(config, client)
    if (video is None or not options.check_color or "color" in options.disabled_checks
            or not settings.get("enabled", False)):
        return []
    hdr = hdr_transfer(video)
    if hdr:
        warnings.warn(f"{video.name} is HDR ({hdr}): color grading isn't checked, because every measurement "
                      f"assumes SDR Rec.709. Check an SDR export instead.", stacklevel=2)
        context.stats["color"] = {"skipped": f"HDR video ({hdr})"}
        return []
    try:
        region = parse_region(ocr_settings(config, client).get("region", "full"))
        shots = read_color(video, settings, None if region == FULL_FRAME else region,
                           resolve_path(config, "color_cache_dir"), resolve_path(config, "models_dir"), stage)
    except (OSError, OcrError) as exc:
        warnings.warn(f"color not checked: {exc}", stacklevel=2)
        return []
    context.stats["color"] = {"shots": len(shots), "faces": sum(1 for s in shots if s.value("skin_hue") is not None)}
    return color_issues(shots, settings)


# --- caption checks ------------------------------------------------------------------------

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
               context: CheckContext | None = None, stage: Stage | None = None) -> list[Issue]:
    context = context or build_context(config, client)
    issues: list[Issue] = []
    by_id = {c.id: c for c in captions}
    enabled = [(name, check) for name, check in CHECKS.items() if context.settings_for(name).get("enabled", False)]
    for n, (name, check) in enumerate(enabled):
        settings = context.settings_for(name)
        if stage is not None:
            stage(n / len(enabled))
            # A slow check (the punctuation API calls) reports its own progress inside its share.
            context.progress = Stage(stage.callback, stage.cancel_event, stage.name,
                                     stage.start + stage.span * n / len(enabled), stage.span / len(enabled))
        min_confidence = settings.get("min_confidence", 0.0)
        found = (mark_ocr_doubt(i, by_id.get(i.caption_id), context.ocr_low_confidence, context.ocr_always_doubtful)
                 for i in check(captions, settings, context))
        issues.extend(i for i in found if i.confidence >= min_confidence)
    context.progress = None
    if stage is not None:
        stage(1.0)
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


def check_status_lines(context: CheckContext, video: Path | None, options: QCOptions) -> list[str]:
    """For the report header: which checks ran, and why any were skipped."""
    names = {"too_short": "Too short / phrase break", "spelling": "Spelling and repeated words",
             "capitalization": "Capitalization", "spacing": "Spacing",
             "punctuation": "Punctuation (Anthropic API)", "transcript": "Captions vs audio (Whisper)",
             "screen_text": "Names vs on-screen graphics"}
    lines = []
    for name in CHECKS:
        settings = context.settings_for(name)
        title = names.get(name, name)
        if name in options.disabled_checks:
            lines.append(f"{title}: switched off for this run")
        elif name == "punctuation":
            stats = context.stats.get("punctuation", {})
            if not options.use_llm:
                lines.append(f"{title}: skipped (--no-llm)")
            elif not settings.get("enabled"):
                lines.append(f"{title}: off in config")
            elif stats.get("skipped"):
                lines.append(f"{title}: skipped ({stats['skipped']})")
            else:
                lines.append(f"{title}: {settings.get('model')}, {stats.get('windows', 0)} captions "
                             f"({stats.get('cached', 0)} from cache, {stats.get('failed', 0)} not reviewed)")
        elif name == "screen_text":
            if context.screen_text is None:
                lines.append(f"{title}: not run (no video)" if settings.get("enabled") else f"{title}: off in config")
            else:
                shown = ", ".join(f'"{t}"' for t in context.screen_text[:5]) or "none found"
                lines.append(f"{title}: on-screen text read: {shown}")
        elif name == "transcript":
            if context.transcript is None:
                lines.append(f"{title}: not run (no video)")
            else:
                model = context.stats.get("transcript", {}).get("model", "?")
                lines.append(f"{title}: Whisper '{model}', {len(context.transcript)} words heard")
        else:
            lines.append(f"{title}: {'ran' if settings.get('enabled') else 'off in config'}")
    color = context.stats.get("color")
    if color and color.get("skipped"):
        lines.append(f"Color grading: skipped ({color['skipped']})")
    elif color:
        lines.append(f"Color grading: {color['shots']} shots measured, {color['faces']} with faces (skin tones)")
    elif video is None:
        lines.append("Color grading: not run (no video)")
    elif not options.check_color or "color" in options.disabled_checks:
        lines.append("Color grading: switched off for this run")
    else:
        lines.append("Color grading: off in config")
    return lines


# --- reports -------------------------------------------------------------------------------

def write_report(result: QCResult, formats: Sequence[str] | None = None,
                 output_dir: Path | None = None) -> list[Path]:
    """Write the report folder (reports/<video>_<date-time>/) in the given formats ("md", "docx";
    default: report.formats in config.yaml), plus issues.csv if report.write_csv is on.
    Screenshots need the video and ffmpeg; without ffmpeg the report is written without them."""
    config = result.config
    settings = dict(config.get("report", {}))
    if formats is not None:
        settings["formats"] = list(formats)
    settings["output_dir"] = Path(output_dir) if output_dir else Path(config["_root"]) / settings.get("output_dir", "reports")
    ffmpeg = None
    if result.video is not None and settings.get("include_screenshots", True):
        try:
            ffmpeg = find_ffmpeg(config.get("machine", {}).get("ffmpeg_path"))
        except MissingToolError as exc:
            warnings.warn(f"no screenshots in the report: {exc}", stacklevel=2)
    rules_path = resolve_path(config, "rules_file")
    rules_version = str(load_yaml(rules_path).get("version", "")) if rules_path.is_file() else None
    info = ReportInfo(srt=result.srt, video=result.video, client=result.client.get("client"),
                      rules_version=rules_version, checks=result.checks)
    return build_report(result.captions, list(result), info, settings, ffmpeg)


def build_docx_report(result: QCResult, output_dir: Path | None = None) -> Path:
    """The Word report only (summary, then a timestamped table with screenshots). Returns its path."""
    written = write_report(result, formats=["docx"], output_dir=output_dir)
    return next(p for p in written if p.suffix == ".docx")


def guess_client(video: Path, known: Sequence[str]) -> str | None:
    """A client code found in the file name ("E8D_reel_01.mp4", "8d - reel.mov"), if it is one of
    `known`. The longest match wins, so "E2OCA" beats "E2O"."""
    import re

    stem = Path(video).stem.upper()
    found = []
    for code in known:
        bare = code[1:] if code.startswith("E") else code
        if re.search(rf"(?<![A-Z0-9])E?{re.escape(bare)}(?![A-Z0-9])", stem):
            found.append(code)
    return max(found, key=len) if found else None


def known_clients(config: dict | None = None) -> list[str]:
    """Client codes that have a clients/<CODE>.yaml."""
    config = config or load_config()
    folder = resolve_path(config, "clients_dir")
    return sorted(normalize_client_code(p.stem) for p in folder.glob("*.yaml"))
