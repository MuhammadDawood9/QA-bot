"""Measure how well the bot's checks (captions, color and audio edits) match your own QC notes.

    python scripts/evaluate.py --make-template          # draft data/ground_truth.csv from the QC spreadsheet
    python scripts/evaluate.py                          # evaluate (videos looked up in data/samples/)
    python scripts/evaluate.py --videos "D:/QC videos" --tolerance 2
    python scripts/evaluate.py --checks captions        # only the caption checks (or: color, audio)
    python scripts/evaluate.py --checks audio --audio-tolerance 1.5      # podcasts (E2CL)
    python scripts/evaluate.py --checks audio --compare-suppression      # and again without the overlap /
                                                                           # music / effect suppression
    python scripts/evaluate.py --note-lag 5         # also count notes written up to 5 s after the flag
    python scripts/evaluate.py --assume-reviewed    # treat every video as fully reviewed (old behaviour)

data/ground_truth.csv columns: video, timestamp, client, issue_type, note_text (and optionally review)
  video       the video's file name (looked up under --videos, recursively) or a full path
  timestamp   as you wrote it: 00:00:19:59, 0:29, 24:37, 00;10;17;08, or a range 2:04-2:15.
              Leave it empty for a note about the whole video ("fix color grade").
  client      client code, for that client's settings (E3AC)
  issue_type  captions: spelling (also wrong, missing, extra or repeated words, names), punctuation
              (also capitals and spacing), too_short (a lone word, a cut phrase, a bad line break),
              timing (late, early, out of sync, speech with no caption), or caption when the note
              doesn't say which.
              color: overexposed, underexposed, crushed_blacks, flat_contrast, saturation,
              color_cast, skin_tone, shot_mismatch, or color_grading when the note doesn't say which.
              audio (podcasts): stutter, repeated_phrase, abrupt_cut, jump_cut, dead_air, or
              audio_edit when the note doesn't say which. Matched within --audio-tolerance (1.5 s)
              of the flagged moment.
              The bot's own type names (capitalization, wrong_word, caption_late...) work too.
              A video with no notes at all can be listed with an empty timestamp and issue_type
              "none": it then only counts toward false alarms.
  note_text   your comment, shown next to misses so you can look at each one
  review      "full" on any one row of a video: every problem in it was noted, so a flag there that
              matches no note really is a false alarm. Videos without it were reviewed only in part
              (a revision round with 1-2 notes): their unmatched flags may be real problems nobody wrote
              down, so they don't count toward precision. A video listed with issue_type "none" is
              fully reviewed too.

data/flag_labels.csv (--labels): your verdict on single flags, which beats the notes. Every run writes
flags_to_label.csv with every flag (verdicts carried over from the labels file); fill its verdict
column (real, wrong, or minor = right but you'd never comment) and save it as data/flag_labels.csv.
A label applies to the flag of the same video and type nearest its time, within --label-window s.

Matching: a flag matches a note when the note's time (or range) falls inside the flagged caption
or shot, widened by --tolerance seconds on each side. Caption notes are matched only by caption
flags and color notes only by color flags. A caption / color_grading note is matched by a flag
of any type in its area. Per check type:
  recall      notes of that type that some flag of that type matched / notes of that type
              (the "all captions" / "all color" rows: every timed note of the area, matched by
              any flag of the area)
  recall +lag the same, also counting a note written up to --note-lag s after the flagged moment
              (a reviewer types the note after hearing or seeing the problem). Shown apart so the
              strict number stays comparable between runs.
  chance      the recall flags placed at random would get (chance +lag: with the note lag): per note,
              the share of its video's time inside some flag's match window, averaged. Recall near
              chance means the flags land on notes by luck, not because they find the problem.
  judged      flags whose truth is known: labelled, or in a fully reviewed video
  precision   judged flags that are real (labelled real or minor, or else matching a note of that
              type or a generic note) / judged flags
  FP / min    judged flags that aren't real, per minute of fully reviewed video
Notes without a timestamp can't be matched to a moment; they're listed separately with whether
the bot flagged anything at all in that video.

Writes reports/evaluation_<date-time>/: evaluation.md, misses.csv, false_alarms.csv, flags_to_label.csv.
Thresholds are not changed by this script.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import color_settings, load_client, load_config, normalize_client_code, ocr_settings, resolve_path  # noqa: E402
from src.inputs.video_ocr import FULL_FRAME, parse_region  # noqa: E402
from src.models import (AUDIO_ISSUES, CAPITALIZATION, CAPTION_EARLY, CAPTION_LATE, CAPTION_SYNC,  # noqa: E402
                        COLOR_ISSUES, EXTRA_WORD, FILLER, MISSING_WORD, PHRASE_BREAK, PUNCTUATION, REPEATED_WORD,
                        SCREEN_TEXT_MISMATCH, SPACING, SPELLING, TOO_SHORT, UNCAPTIONED_SPEECH, WRONG_WORD, Issue,
                        shot_number)
from src.utils.timecode import TimecodeError, format_timestamp, infer_fps, parse_range  # noqa: E402

GENERIC = {"color_grading", "color", "colour", "colour_grading", ""}
ALL = "all color"
# What a reviewer calls a caption mistake -> the bot's issue types that find it.
CAPTION_FAMILIES = {
    "spelling": {SPELLING, REPEATED_WORD, WRONG_WORD, MISSING_WORD, EXTRA_WORD, SCREEN_TEXT_MISMATCH},
    "punctuation": {PUNCTUATION, CAPITALIZATION, SPACING},
    "too_short": {TOO_SHORT, PHRASE_BREAK},
    "timing": {CAPTION_EARLY, CAPTION_LATE, CAPTION_SYNC, UNCAPTIONED_SPEECH},
}
CAPTION_GENERIC = "caption"
ALL_CAPTIONS = "all captions"
ALIASES = {"too_short_caption": "too_short", "capitals": "punctuation", "capital": "punctuation",
           "sync": "timing", "captions": "caption", "subtitle": "caption", "subtitles": "caption"}
UNUSED_SPAN = 2.0          # seconds a flag with no caption (speech without one) covers after its time
AUDIO_TYPES = sorted(AUDIO_ISSUES - {FILLER})
AUDIO_GENERIC = "audio_edit"
ALL_AUDIO = "all audio"
AUDIO_ALIASES = {"stutter": "stutter", "repeat": "repeated_phrase", "repeated_word": "repeated_phrase",
                 "retake": "repeated_phrase", "abrupt": "abrupt_cut", "cut": "abrupt_cut", "jump": "jump_cut",
                 "silence": "dead_air", "pause": "dead_air", "audio": AUDIO_GENERIC}


def family(kind: str) -> str:
    """A caption issue type's family (wrong_word -> spelling); a color type stays itself."""
    return next((name for name, types in CAPTION_FAMILIES.items() if kind in types), kind)


def area(kind: str) -> str:
    if kind in AUDIO_ISSUES or kind == AUDIO_GENERIC:
        return "audio"
    return "caption" if kind in CAPTION_FAMILIES or kind == CAPTION_GENERIC else "color"


def generic(area_name: str) -> str:
    return {"caption": CAPTION_GENERIC, "audio": AUDIO_GENERIC}.get(area_name, "color_grading")
COLUMNS = ["video", "timestamp", "client", "issue_type", "note_text"]
FULL_REVIEW = {"full", "complete", "yes", "y", "true", "1"}
LABEL_COLUMNS = ["video", "timestamp", "seconds", "issue_type", "message", "matches_note", "verdict", "comment"]
REAL, WRONG, MINOR = "real", "wrong", "minor"
VERDICTS = {"real": REAL, "yes": REAL, "y": REAL, "tp": REAL, "true": REAL, "1": REAL,
            "wrong": WRONG, "no": WRONG, "n": WRONG, "fp": WRONG, "false": WRONG, "0": WRONG,
            "minor": MINOR, "real but minor": MINOR, "valid": MINOR}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mxf", ".m4v", ".mkv", ".avi", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}


@dataclass
class Note:
    video: str
    stamp: str
    client: str
    kind: str                       # a color issue type, or "color_grading" for any
    text: str
    start: float | None = None      # None: about the whole video
    end: float | None = None
    matched_by: list[Issue] = field(default_factory=list)
    late_by: list[Issue] = field(default_factory=list)     # flags it matches only with --note-lag
    review: str = ""                # "full": every problem in this video was noted

    @property
    def area(self) -> str:
        return area(self.kind)


@dataclass
class Flag:
    video: str
    issue: Issue
    start: float                    # the flagged caption or shot (or moment)
    end: float
    matches: list[Note] = field(default_factory=list)
    verdict: str | None = None      # from data/flag_labels.csv: real, wrong or minor
    window: tuple[float, float, float] | None = None    # set by match: (lo, hi, hi with the note lag)

    @property
    def kind(self) -> str:
        return family(self.issue.type)

    @property
    def area(self) -> str:
        return area(self.kind)


# --- reading the ground truth --------------------------------------------------------------

def read_truth(path: Path, fps: float) -> tuple[list[Note], list[str]]:
    notes, problems = [], []
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = set(COLUMNS) - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"error: {path.name} is missing the column(s): {', '.join(sorted(missing))}")
        for n, row in enumerate(reader, start=2):
            video = (row.get("video") or "").strip()
            if not video:
                problems.append(f"line {n}: no video, skipped")
                continue
            kind = (row.get("issue_type") or "").strip().lower()
            kind = AUDIO_ALIASES.get(kind, kind) if kind not in AUDIO_ISSUES else kind
            kind = "color_grading" if kind in GENERIC else family(ALIASES.get(kind, kind))
            if kind not in (COLOR_ISSUES | set(CAPTION_FAMILIES) | AUDIO_ISSUES
                            | {"color_grading", CAPTION_GENERIC, AUDIO_GENERIC, "none"}):
                problems.append(f"line {n}: issue_type {kind!r} isn't a type the bot checks; counted as color_grading")
                kind = "color_grading"
            note = Note(video, (row.get("timestamp") or "").strip(), normalize_client_code(row.get("client")) or "",
                        kind, (row.get("note_text") or "").strip(),
                        review="full" if (row.get("review") or "").strip().lower() in FULL_REVIEW else "")
            if note.stamp:
                try:
                    start, end = parse_range(note.stamp, fps=infer_fps(note.stamp, fps))
                    note.start, note.end = start, (end if end is not None else start)
                except TimecodeError as exc:
                    problems.append(f"line {n}: {exc}; treated as a note about the whole video")
            notes.append(note)
    return notes, problems


def reviewed_videos(notes: list[Note]) -> set[str]:
    """Videos whose every problem was noted: "full" in the review column of any of their rows, or
    listed with issue_type "none" (nothing to note). Call before notes are set aside for a run."""
    return {n.video for n in notes if n.review == "full" or n.kind == "none"}


# --- labels: your verdict on single flags ---------------------------------------------------

@dataclass
class Label:
    video: str
    seconds: float
    issue_type: str
    verdict: str
    comment: str = ""


def read_labels(path: Path) -> tuple[list[Label], list[str]]:
    """The labelled rows of a flags_to_label.csv saved as data/flag_labels.csv; rows with no verdict
    are skipped, unknown verdicts are listed as problems."""
    labels, problems = [], []
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = {"video", "seconds", "issue_type", "verdict"} - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"error: {path.name} is missing the column(s): {', '.join(sorted(missing))}")
        for n, row in enumerate(reader, start=2):
            raw = (row.get("verdict") or "").strip().lower()
            if not raw:
                continue
            verdict = VERDICTS.get(raw)
            if verdict is None:
                problems.append(f"{path.name} line {n}: verdict {raw!r} isn't real, wrong or minor; skipped")
                continue
            try:
                seconds = float(row["seconds"])
            except (TypeError, ValueError):
                problems.append(f"{path.name} line {n}: seconds {row.get('seconds')!r} isn't a number; skipped")
                continue
            labels.append(Label((row.get("video") or "").strip(), seconds, (row.get("issue_type") or "").strip(),
                                verdict, (row.get("comment") or "").strip()))
    return labels, problems


def apply_labels(flags: list[Flag], labels: list[Label], window: float) -> list[Label]:
    """Give each label's verdict to the flag of the same video and issue type nearest its time
    (within `window` seconds, one label per flag). Returns the labels that found no flag."""
    unused = []
    for label in labels:
        candidates = [f for f in flags if f.verdict is None and f.video == label.video
                      and f.issue.type == label.issue_type and abs(f.issue.timestamp - label.seconds) <= window]
        if not candidates:
            unused.append(label)
            continue
        min(candidates, key=lambda f: abs(f.issue.timestamp - label.seconds)).verdict = label.verdict
    return unused


def write_label_sheet(path: Path, flags: list[Flag]) -> None:
    """Every flag, one row each, for you to label: fill verdict, save as data/flag_labels.csv."""
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(LABEL_COLUMNS)
        for fl in sorted(flags, key=lambda x: (x.video, x.issue.timestamp, x.issue.type)):
            note = "; ".join(f"[{n.kind}] {n.text}" for n in fl.matches)
            w.writerow([fl.video, format_timestamp(fl.issue.timestamp), f"{fl.issue.timestamp:.2f}", fl.issue.type,
                        fl.issue.message, note, fl.verdict or "", ""])


def find_video(name: str, folders: list[Path]) -> Path | None:
    path = Path(name)
    if path.is_absolute():
        return path if path.is_file() else None
    for folder in folders:
        if (folder / path).is_file():
            return folder / path
    for folder in folders:
        for candidate in folder.rglob(path.name):
            if candidate.is_file():
                return candidate
    return None


# --- running the checks ---------------------------------------------------------------------

def color_flags(video: Path, client_code: str, config: dict) -> tuple[list[Flag], float, float]:
    """The color issues for one video as flags with their shot spans; the video's length and the
    analysis time in seconds (0 when the measurements came from the cache)."""
    from src.video.color import color_issues, hdr_transfer, read_color

    client = load_client(config, client_code or None)
    settings = color_settings(config, client)
    region = parse_region(ocr_settings(config, client).get("region", "full"))
    if hdr_transfer(video):
        print(f"  {video.name}: HDR, color isn't measured; skipped")
        return [], 0.0, 0.0
    started = time.monotonic()
    shots = read_color(video, settings, None if region == FULL_FRAME else region,
                       resolve_path(config, "color_cache_dir"), resolve_path(config, "models_dir"))
    took = time.monotonic() - started
    spans = {s.number: (s.start, s.end) for s in shots}
    length = max((s.end for s in shots), default=0.0)
    flags = []
    for issue in color_issues(shots, settings):
        start, end = spans.get(shot_number(issue.caption_id), (issue.timestamp, issue.timestamp))
        flags.append(Flag(video.name, issue, start, end))
    return flags, length, took


def caption_flags(video: Path, client_code: str, config: dict) -> tuple[list[Flag], float, float]:
    """The caption issues for one video (every caption check, the paid punctuation one off), as
    flags spanning their caption; the video's length as far as the captions show, and the time."""
    from src.engine import QCError, QCOptions, run_qc

    started = time.monotonic()
    try:
        result = run_qc(video, client_code or None, options=QCOptions(use_llm=False, check_color=False),
                        config=config)
    except QCError as exc:
        print(f"  {video.name}: captions not checked: {exc}")
        return [], 0.0, 0.0
    took = time.monotonic() - started
    spans = {c.id: (c.start, c.end) for c in result.captions}
    length = max([c.end for c in result.captions] + [i.timestamp for i in result.issues] + [0.0])
    flags = []
    for issue in result.issues:
        if issue.type in COLOR_ISSUES:
            continue
        if issue.type == CAPTION_SYNC:
            start, end = 0.0, length                      # one issue about the whole video
        else:
            start, end = spans.get(issue.caption_id, (issue.timestamp, issue.timestamp + UNUSED_SPAN))
        flags.append(Flag(video.name, issue, start, end))
    return flags, length, took


def audio_flags(path: Path, client_code: str, config: dict, overrides: dict | None = None) -> tuple[list[Flag], float, float]:
    """The audio-edit issues for one podcast (or video), each a moment. overrides: laid over the
    client's audio settings (e.g. {"suppression": False})."""
    from src.engine import QCError, QCOptions, audio_settings, run_qc
    from src.config import load_client

    started = time.monotonic()
    client = load_client(config, client_code or None)
    if not audio_settings(config, client, audio_only=True).get("enabled"):
        print(f"  {path.name}: the audio checks are off for client {client_code or 'default'}")
    try:
        result = run_qc(path, client_code or None, config=config,
                        options=QCOptions(use_llm=False, check_color=False, audio_overrides=overrides or {}))
    except QCError as exc:
        print(f"  {path.name}: audio not checked: {exc}")
        return [], 0.0, 0.0
    took = time.monotonic() - started
    length = float((result.context.stats.get("audio") or {}).get("duration", 0.0))
    flags = [Flag(path.name, i, i.timestamp, i.timestamp) for i in result.issues if i.type in AUDIO_ISSUES - {FILLER}]
    return flags, length, took


# --- matching and scoring -------------------------------------------------------------------

def match(notes: list[Note], flags: list[Flag], tolerance: float, audio_tolerance: float | None = None,
          note_lag: float = 0.0) -> None:
    """Strict matches (the note inside the flag's span +-tolerance) go to note.matched_by and
    flag.matches; a note up to note_lag s later than that only to note.late_by (recall +lag)."""
    for flag in flags:
        tol = audio_tolerance if audio_tolerance is not None and flag.area == "audio" else tolerance
        lo, hi = flag.start - tol, flag.end + tol
        flag.window = (lo, hi, hi + max(note_lag, 0.0))
        for note in notes:
            if note.start is None or note.kind == "none" or note.area != flag.area:
                continue
            if note.end >= lo and note.start <= hi:              # overlap, ranges included
                note.matched_by.append(flag.issue)
                flag.matches.append(note)
            elif note_lag > 0 and note.end >= lo and note.start <= hi + note_lag:
                note.late_by.append(flag.issue)


def is_real(flag: Flag, kind: str | None, area_name: str) -> bool:
    """A judged flag's truth: its label, else whether it matches a note (of `kind` or a generic
    one; any note of the area when kind is None)."""
    if flag.verdict is not None:
        return flag.verdict in (REAL, MINOR)
    if kind is None:
        return bool(flag.matches)
    return any(n.kind in (kind, generic(area_name)) for n in flag.matches)


def coverage(flags: list[Flag], length: float, lagged: bool) -> float:
    """Share of a video's time (0..length s) inside at least one flag's match window: the chance
    that a note at a random moment would be matched by these flags."""
    if length <= 0:
        return 0.0
    spans = sorted((max(f.window[0], 0.0), min(f.window[2] if lagged else f.window[1], length))
                   for f in flags if f.window is not None)
    covered, reach = 0.0, 0.0
    for lo, hi in spans:
        lo = max(lo, reach)
        if hi > lo:
            covered, reach = covered + hi - lo, hi
    return covered / length


def chance_recall(relevant: list[Note], flags: list[Flag], lengths: dict[str, float], lagged: bool) -> float | None:
    """The recall flags placed at random would get: per note, the share of its video covered by
    the flag windows (the same number of flags, the same window sizes), averaged over the notes."""
    if not relevant or any(n.video not in lengths for n in relevant):
        return None
    by_video: dict[str, list[Flag]] = defaultdict(list)
    for f in flags:
        by_video[f.video].append(f)
    cover = {v: coverage(by_video[v], lengths[v], lagged) for v in {n.video for n in relevant}}
    return sum(cover[n.video] for n in relevant) / len(relevant)


def score(notes: list[Note], flags: list[Flag], minutes: float, reviewed: set[str] | None = None,
          lengths: dict[str, float] | None = None) -> list[dict]:
    """Per check type. reviewed: the fully reviewed videos (None = every video): only their flags,
    and labelled flags, count toward precision; minutes is their length, for false alarms / min.
    lengths: each video's length in seconds, for the chance columns (None: not computed)."""
    rows = []
    timed = [n for n in notes if n.start is not None and n.kind != "none"]
    for name, kinds, everything in (("caption", list(CAPTION_FAMILIES), ALL_CAPTIONS),
                                    ("color", sorted(COLOR_ISSUES), ALL), ("audio", AUDIO_TYPES, ALL_AUDIO)):
        area_notes = [n for n in timed if n.area == name]
        area_flags = [f for f in flags if f.area == name]
        for kind in kinds + [everything]:
            if kind == everything:
                relevant = area_notes
                found = [n for n in relevant if n.matched_by]
                late = [n for n in relevant if n.matched_by or n.late_by]
                mine = area_flags
            else:
                relevant = [n for n in area_notes if n.kind == kind]
                found = [n for n in relevant if any(family(i.type) == kind for i in n.matched_by)]
                late = [n for n in relevant if any(family(i.type) == kind for i in n.matched_by + n.late_by)]
                mine = [f for f in area_flags if f.kind == kind]
            judged = [f for f in mine if f.verdict is not None or reviewed is None or f.video in reviewed]
            true = [f for f in judged if is_real(f, None if kind == everything else kind, name)]
            false_in_reviewed = [f for f in judged if (reviewed is None or f.video in reviewed) and f not in true]
            rows.append({
                "type": kind, "area": name, "notes": len(relevant), "found": len(found), "found_lag": len(late),
                "flags": len(mine), "judged": len(judged), "true": len(true),
                "recall": len(found) / len(relevant) if relevant else None,
                "recall_lag": len(late) / len(relevant) if relevant else None,
                "chance": chance_recall(relevant, mine, lengths, False) if lengths is not None else None,
                "chance_lag": chance_recall(relevant, mine, lengths, True) if lengths is not None else None,
                "precision": len(true) / len(judged) if judged else None,
                "fp_per_min": len(false_in_reviewed) / minutes if minutes else None,
            })
    return rows


def pct(x) -> str:
    return "n/a" if x is None else f"{x:.0%}"


# --- output ---------------------------------------------------------------------------------

def write_outputs(folder: Path, rows: list[dict], notes: list[Note], flags: list[Flag], videos: dict,
                  problems: list[str], tolerance: float, audio_tolerance: float = 1.5, note_lag: float = 0.0,
                  reviewed: set[str] | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    misses = [n for n in notes if n.start is not None and n.kind != "none" and not n.matched_by]
    false_alarms = [f for f in flags if not f.matches and f.verdict not in (REAL, MINOR)]
    counts = lambda f: f.verdict == WRONG or reviewed is None or f.video in reviewed      # noqa: E731
    write_label_sheet(folder / "flags_to_label.csv", flags)
    untimed = [n for n in notes if n.start is None and n.kind != "none"]

    with (folder / "misses.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["video", "timestamp", "seconds", "client", "issue_type", "note_text"])
        for n in misses:
            w.writerow([n.video, n.stamp, f"{n.start:.2f}", n.client, n.kind, n.text])
    with (folder / "false_alarms.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["video", "timestamp", "seconds", "caption or shot", "issue_type", "severity", "message",
                    "counted"])
        for fl in false_alarms:
            w.writerow([fl.video, format_timestamp(fl.issue.timestamp), f"{fl.issue.timestamp:.2f}",
                        fl.issue.original, fl.issue.type, fl.issue.severity, fl.issue.message,
                        "yes" if counts(fl) else "no: partly reviewed video"])

    total_min = sum(v["minutes"] for v in videos.values())
    full = [v for v in videos if reviewed is None or v in reviewed]
    labelled = sum(1 for f in flags if f.verdict is not None)
    if reviewed is None:
        judged_text = "every video is treated as fully reviewed (--assume-reviewed)"
    elif full:
        judged_text = f"{len(full)} fully reviewed video(s) of {len(videos)}"
    else:
        judged_text = "no video is marked fully reviewed (\"full\" in the ground truth's review column)"
    lines = [f"# Check evaluation, {dt.datetime.now():%Y-%m-%d %H:%M}", "",
             f"{len(videos)} video(s), {total_min:.1f} minutes; {sum(1 for n in notes if n.start is not None)} timed "
             f"note(s), {len(untimed)} untimed; matching tolerance ±{tolerance:g} s around each flagged caption or shot, "
             f"±{audio_tolerance:g} s around each audio flag.",
             "", "Caption flags come from every caption check except the paid punctuation one (no API calls).", "",
             f"**Precision** counts judged flags only: {judged_text}, plus {labelled} labelled flag(s). A flag in a "
             "partly reviewed video that matches no note may be a real problem nobody wrote down; label it in "
             "flags_to_label.csv to count it.",
             ("**Recall +lag** also counts a note written up to "
              f"{note_lag:g} s after the flagged moment." if note_lag else "**Recall +lag** is off (--note-lag 0)."),
             "**Chance** is the recall the same flags would get if they were placed at random (the share of each "
             "video covered by their match windows). Only recall well above chance means a check finds problems.",
             "",
             "| Check | Notes | Found | Recall | Chance | Recall +lag | Chance +lag | Flags | Judged | True | Precision "
             "| False alarms / min |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        if r["notes"] or r["flags"]:
            fp = "n/a" if r["fp_per_min"] is None else f"{r['fp_per_min']:.2f}"
            lag, chance_lag = (pct(r["recall_lag"]), pct(r["chance_lag"])) if note_lag else ("-", "-")
            lines.append(f"| {r['type']} | {r['notes']} | {r['found']} | {pct(r['recall'])} | {pct(r['chance'])} | "
                         f"{lag} | {chance_lag} | {r['flags']} | {r['judged']} | {r['true']} | "
                         f"{pct(r['precision'])} | {fp} |")
    lines += ["", "## Videos", "", "| Video | Minutes | Flags | Notes | Reviewed | Analysis time |",
              "|---|---:|---:|---:|---|---|"]
    for name, v in videos.items():
        took = "from cache" if v["took"] == 0 else f"{v['took']:.0f} s ({v['took'] / max(v['minutes'], 1e-9):.0f} s per minute)"
        lines.append(f"| {name} | {v['minutes']:.1f} | {v['flags']} | {v['notes']} | "
                     f"{'full' if name in full else 'partly'} | {took} |")
    lines += ["", f"## Misses ({len(misses)})", "", "Notes no flag of the right type matched.", ""]
    lines += [f"- **{n.video}** {n.stamp} [{n.kind}] {n.text}" for n in misses] or ["- none"]
    lines += ["", f"## False alarms ({len(false_alarms)})", "",
              "Flags that match no note and aren't labelled real. Those marked (not counted) are in a partly "
              "reviewed video, so they don't lower precision.", ""]
    lines += [f"- **{f.video}** {format_timestamp(f.issue.timestamp)} {f.issue.original} [{f.issue.type}, "
              f"{f.issue.severity}] {f.issue.message}" + ("" if counts(f) else " (not counted)")
              for f in false_alarms] or ["- none"]
    other_type = [f for f in flags if f.matches
                  and not any(n.kind in (f.kind, generic(f.area)) for n in f.matches)]
    lines += ["", f"## Right moment, different type ({len(other_type)})", "",
              "Flags on a noted moment whose note names another problem. They count as found for "
              "\"all captions\" / \"all color\" but as false alarms in their own row.", ""]
    lines += [f"- **{f.video}** {format_timestamp(f.issue.timestamp)} {f.issue.original} [{f.issue.type}] "
              f"vs note [{', '.join(sorted({n.kind for n in f.matches}))}]: {f.issue.message}"
              for f in other_type] or ["- none"]
    if untimed:
        lines += ["", f"## Notes without a timestamp ({len(untimed)})", ""]
        for n in untimed:
            count = videos.get(n.video, {}).get("flags", 0)
            lines.append(f"- **{n.video}** [{n.kind}] {n.text} (bot flagged {count} thing(s) in this video)")
    if problems:
        lines += ["", "## Ground truth problems", ""] + [f"- {p}" for p in problems]
    (folder / "evaluation.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return folder / "evaluation.md"


# --- a draft ground truth from the spreadsheet ------------------------------------------------

KEYWORDS = [
    ("skin_tone", r"skin"),
    ("overexposed", r"overexpos|too bright|blown|clipp|burn"),
    ("underexposed", r"underexpos|too (much )?dark|too dim"),
    ("crushed_blacks", r"crush"),
    ("flat_contrast", r"flat|contrast|lifted|washed"),
    ("color_cast", r"magenta|green|purple|tint|cast|white balance|warm|cool|yellow|orange|blue"),
    ("saturation", r"saturat|dull|vibran|grey|gray"),
    ("shot_mismatch", r"match|different|alien|consisten|camera changes|color change"),
]


# Audio-edit notes ("Stutter on word got", "Word repeat", "Abrupt cut", "Silence not removed") ->
# the bot's audio issue types; the first pattern that matches wins.
AUDIO_KEYWORDS = [
    ("stutter", r"stutter|false start"),
    ("repeated_phrase", r"repeat|twice|retake|said again"),
    ("abrupt_cut", r"abrupt|cut off|cutoff|chopped|click|\bpop\b|weird sound|glitch"),
    ("jump_cut", r"jump|room tone|background|tone change|breath|volume|level"),
    ("dead_air", r"silence|dead air|pause|gap"),
]
# Podcast clients whose audio-edit notes go into the template. E2CL is the audio podcast client;
# the spreadsheet's podcast notes so far are under E2CS (Market podcast episodes).
AUDIO_CLIENTS = ("E2CL", "E2CS")


def audio_kind(text: str) -> str | None:
    return next((k for k, pattern in AUDIO_KEYWORDS if re.search(pattern, text, re.I)), None)


# The spreadsheet's caption categories -> ground truth types. creative_direction and other are
# requests the bot can't check (new text, animations), so they're left out.
CAPTION_CATEGORIES = {"spelling": "spelling", "punctuation": "punctuation", "too_short_caption": "too_short"}


def _sheet(book, prefix: str) -> tuple[dict, list]:
    name = next((n for n in book.sheetnames if n.startswith(prefix)), None)
    if name is None:
        raise SystemExit(f"error: the spreadsheet has no sheet named {prefix}...")
    rows = list(book[name].iter_rows(values_only=True))
    header = [str(h) for h in rows[0]]
    return {h: i for i, h in enumerate(header)}, rows[1:]


def template_rows(xlsx: Path, audio_clients=AUDIO_CLIENTS) -> tuple[list[list], int]:
    """Ground truth rows from the spreadsheet (caption mistakes, color notes, then the podcast
    clients' audio-edit notes), with the task name where the video goes; and how many caption
    notes were left out as not checkable."""
    from src.config import normalize_client_code
    import openpyxl

    book = openpyxl.load_workbook(xlsx, read_only=True)
    out, skipped = [], 0
    col, rows = _sheet(book, "Caption mistakes")
    for r in rows:
        kind = CAPTION_CATEGORIES.get(str(r[col["category"]] or ""))
        if kind is None:
            skipped += 1
            continue
        text = str(r[col["comment_text"]] or "").strip()
        wrote, should = r[col["editor_wrote"]], r[col["should_be"]]
        if wrote or should:
            text += f" [editor wrote: {wrote or '?'}; should be: {should or '?'}]"
        out.append([r[col["task_name"]], str(r[col["video_timestamp"]] or "").strip(), r[col["client"]] or "",
                    kind, text])
    col, rows = _sheet(book, "All QC notes")
    for r in rows:
        if r[col["category"]] != "color_grading":
            continue
        text = str(r[col["comment_text"]] or "").strip()
        kind = next((k for k, pattern in KEYWORDS if re.search(pattern, text, re.I)), "color_grading")
        out.append([r[col["task_name"]], str(r[col["video_timestamp"]] or "").strip(), r[col["client"]] or "",
                    kind, text])
    wanted = {normalize_client_code(c) for c in audio_clients}
    for r in rows:
        client = normalize_client_code(str(r[col["client"]] or "")) or ""
        if not client:
            m = re.search(r"#\s*([A-Za-z0-9_]+)", str(r[col["task_name"]] or ""))
            client = normalize_client_code(m.group(1)) if m else ""
        if client not in wanted or r[col["category"]] == "color_grading":
            continue
        text = str(r[col["comment_text"]] or "").strip()
        kind = audio_kind(text)
        if kind:
            out.append([r[col["task_name"]], str(r[col["video_timestamp"]] or "").strip(), client, kind, text])
    return out, skipped


def make_template(xlsx: Path, out: Path) -> int:
    if out.exists():
        raise SystemExit(f"error: {out} already exists; move it away first so your edits aren't overwritten")
    rows, skipped = template_rows(xlsx)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS + ["review"])
        for task, stamp, client, kind, text in rows:
            # The video column starts as the task name: replace it with the video's file name.
            w.writerow([f"TODO {task}".strip(), stamp, client, kind, text, ""])

    # Which videos to collect, the ones with the most checkable notes first.
    tasks: dict[str, dict] = {}
    for task, stamp, client, kind, _ in rows:
        t = tasks.setdefault(task, {"client": client, "caption": 0, "color": 0, "audio": 0, "timed": 0})
        t[area(kind)] += 1
        t["timed"] += bool(stamp)
    needed = out.with_name(out.stem + "_videos.csv")
    with needed.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["task_name", "client", "caption_notes", "color_notes", "audio_notes", "timed_notes", "video_file"])
        for task, t in sorted(tasks.items(), key=lambda kv: (-kv[1]["timed"], kv[0])):
            w.writerow([task, t["client"], t["caption"], t["color"], t["audio"], t["timed"], ""])
    captions = sum(1 for r in rows if area(r[3]) == "caption")
    audio = sum(1 for r in rows if area(r[3]) == "audio")
    print(f"wrote {out}: {captions} caption notes ({skipped} creative or other requests left out: the bot can't "
          f"check them), {len(rows) - captions - audio} color notes and {audio} audio-edit notes "
          f"(clients {', '.join(AUDIO_CLIENTS)}), from {len(tasks)} videos.")
    print(f"wrote {needed}: the videos to collect, most useful first.")
    print("Replace each 'TODO <task name>' with the video's file name, check the guessed issue_type, delete rows "
          "for videos you don't have, and put \"full\" in the review column of videos whose every problem was noted.")
    return 0


def compare_suppression(report: Path, rows: list[dict], notes: list[Note], by_video: dict, folders: list[Path],
                        config: dict, tolerance: float, minutes: float, reviewed: set[str] | None = None,
                        labels: list[Label] | None = None, label_window: float = 0.5) -> None:
    """The audio rows again with suppression off; a before/after table in evaluation.md and on screen."""
    from dataclasses import replace

    fresh = [replace(n, matched_by=[], late_by=[]) for n in notes]
    flags: list[Flag] = []
    for name, video_notes in by_video.items():
        path = find_video(name, folders)
        if path is None or not any(n.area == "audio" for n in video_notes) and path.suffix.lower() not in {
                ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}:
            continue
        client = next((n.client for n in video_notes if n.client), "")
        print(f"checking {path.name} again with the suppression off...")
        found, _, _ = audio_flags(path, client, config, {"suppression": False})
        match([n for n in fresh if n.video == name], found, 2.0, tolerance)
        flags += found
    apply_labels(flags, labels or [], label_window)
    off = {r["type"]: r for r in score(fresh, flags, minutes, reviewed) if r["area"] == "audio"}
    on = {r["type"]: r for r in rows if r["area"] == "audio"}
    lines = ["", "## Audio: with and without the suppression", "",
             "Suppression: overlap zones, music, effects and the client's cues are left alone; room tone and "
             "level changes count only with stronger evidence; repeats by a different voice are not stutters.", "",
             "| Check | Notes | Recall off | Recall on | Flags off | Flags on | Precision off | Precision on | "
             "False alarms/min off | on |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    print(f"\n{'audio check':<16}{'notes':>6}{'recall off':>11}{'on':>6}{'flags off':>10}{'on':>5}"
          f"{'precision off':>14}{'on':>6}")
    for kind in AUDIO_TYPES + [ALL_AUDIO]:
        a, b = off.get(kind), on.get(kind)
        if not a or not b or not (a["notes"] or a["flags"] or b["flags"]):
            continue
        fp = lambda r: "n/a" if r["fp_per_min"] is None else f"{r['fp_per_min']:.2f}"     # noqa: E731
        lines.append(f"| {kind} | {a['notes']} | {pct(a['recall'])} | {pct(b['recall'])} | {a['flags']} | {b['flags']} | "
                     f"{pct(a['precision'])} | {pct(b['precision'])} | {fp(a)} | {fp(b)} |")
        print(f"{kind:<16}{a['notes']:>6}{pct(a['recall']):>11}{pct(b['recall']):>6}{a['flags']:>10}{b['flags']:>5}"
              f"{pct(a['precision']):>14}{pct(b['precision']):>6}")
    with report.open("a", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


# --- main -----------------------------------------------------------------------------------

def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Evaluate the caption and color checks against your QC notes.")
    parser.add_argument("--truth", type=Path, default=ROOT / "data" / "ground_truth.csv")
    parser.add_argument("--videos", type=Path, action="append",
                        help="folder to look for the videos in (repeatable; default data/samples and data)")
    parser.add_argument("--tolerance", type=float, default=2.0, help="seconds either side of a flagged shot")
    parser.add_argument("--checks", choices=("all", "captions", "color", "audio"), default="all",
                        help="which checks to evaluate (captions and audio run speech recognition: slower)")
    parser.add_argument("--audio-tolerance", type=float, default=1.5, help="seconds either side of an audio flag")
    parser.add_argument("--note-lag", type=float, default=5.0,
                        help="for the recall +lag column: a note may be up to this many seconds after the flag "
                             "(0 = off)")
    parser.add_argument("--labels", type=Path, default=ROOT / "data" / "flag_labels.csv",
                        help="your verdicts on single flags (a filled-in flags_to_label.csv)")
    parser.add_argument("--label-window", type=float, default=0.5,
                        help="seconds between a label's time and the flag it belongs to")
    parser.add_argument("--assume-reviewed", action="store_true",
                        help="treat every video as fully reviewed: every unmatched flag counts as a false alarm")
    parser.add_argument("--compare-suppression", action="store_true",
                        help="also run the audio checks with the overlap / music / effect suppression off, and "
                             "compare recall and precision")
    parser.add_argument("--make-template", action="store_true",
                        help="write a draft ground_truth.csv (and the list of videos to collect) from the QC "
                             "spreadsheet")
    parser.add_argument("--xlsx", type=Path, default=ROOT / "data" / "QC_Review_Analysis_Jun-Oct_2026.xlsx")
    args = parser.parse_args(argv)

    if args.make_template:
        return make_template(args.xlsx, args.truth)
    if not args.truth.is_file():
        raise SystemExit(f"error: {args.truth} not found. Make a draft with --make-template")

    config = load_config()
    notes, problems = read_truth(args.truth, float(config.get("timecode", {}).get("fps", 30)))
    reviewed = None if args.assume_reviewed else reviewed_videos(notes)
    labels: list[Label] = []
    if args.labels.is_file():
        labels, label_problems = read_labels(args.labels)
        problems += label_problems
    wanted = {"all": {"caption", "color", "audio"}, "captions": {"caption"}, "color": {"color"},
              "audio": {"audio"}}[args.checks]
    for n in notes:
        if n.kind != "none" and n.area not in wanted:
            n.kind = "none"                                         # not part of this run
    folders = args.videos or [ROOT / "data" / "samples", ROOT / "data"]
    by_video: dict[str, list[Note]] = defaultdict(list)
    for n in notes:
        by_video[n.video].append(n)

    all_flags: list[Flag] = []
    videos: dict[str, dict] = {}
    for name, video_notes in by_video.items():
        path = find_video(name, folders)
        if path is None:
            problems.append(f"{name}: video not found under {', '.join(str(f) for f in folders)}; skipped")
            for n in video_notes:
                n.kind = "none"                                     # not evaluated
            continue
        client = next((n.client for n in video_notes if n.client), "")
        areas = {n.area for n in video_notes if n.kind != "none"} or set(wanted)      # no notes: both count
        print(f"checking {path.name} (client {client or 'default'}; {', '.join(sorted(areas))})...")
        flags, length, took = [], 0.0, 0.0
        if path.suffix.lower() in {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}:
            areas &= {"audio", "caption"}                                       # no picture in an audio file
        for check_name, check in (("caption", caption_flags), ("color", color_flags), ("audio", audio_flags)):
            if check_name in areas & wanted:
                found, seconds, spent = check(path, client, config)
                flags += found
                length, took = max(length, seconds), took + spent
        match(video_notes, flags, args.tolerance, args.audio_tolerance, args.note_lag)
        all_flags += flags
        videos[name] = {"minutes": length / 60, "flags": len(flags), "notes": len(video_notes), "took": took}

    unused = apply_labels(all_flags, labels, args.label_window)
    if unused:
        problems.append(f"{len(unused)} label(s) in {args.labels.name} match no flag of this run (the checks "
                        "changed, or that video wasn't evaluated)")
    evaluated = [n for n in notes if n.kind != "none" or n.video in videos]
    minutes = sum(v["minutes"] for name, v in videos.items() if reviewed is None or name in reviewed)
    lengths = {name: v["minutes"] * 60 for name, v in videos.items()}
    rows = score(evaluated, all_flags, minutes, reviewed, lengths)
    folder = Path(config["_root"]) / config.get("report", {}).get("output_dir", "reports") / \
        f"evaluation_{dt.datetime.now():%Y%m%d-%H%M%S}"
    report = write_outputs(folder, rows, evaluated, all_flags, videos, problems, args.tolerance, args.audio_tolerance,
                           args.note_lag, reviewed)
    if args.compare_suppression and "audio" in wanted:
        compare_suppression(report, rows, evaluated, by_video, folders, config, args.audio_tolerance, minutes,
                            reviewed, labels, args.label_window)

    print()
    print(f"{'check':<16}{'notes':>6}{'found':>6}{'recall':>8}{'chance':>8}{'+lag':>6}{'chance':>8}{'flags':>7}"
          f"{'judged':>7}{'true':>6}{'precision':>11}{'FP/min':>8}")
    for r in rows:
        if r["notes"] or r["flags"]:
            fp = "n/a" if r["fp_per_min"] is None else f"{r['fp_per_min']:.2f}"
            lag, chance_lag = (pct(r["recall_lag"]), pct(r["chance_lag"])) if args.note_lag else ("-", "-")
            print(f"{r['type']:<16}{r['notes']:>6}{r['found']:>6}{pct(r['recall']):>8}{pct(r['chance']):>8}{lag:>6}"
                  f"{chance_lag:>8}{r['flags']:>7}{r['judged']:>7}{r['true']:>6}{pct(r['precision']):>11}{fp:>8}")
    for p in problems:
        print(f"note: {p}")
    if reviewed is not None and not any(v in reviewed for v in videos):
        print("\nnote: no evaluated video is marked fully reviewed, so precision uses labelled flags only. Put "
              "\"full\" in the review column of the ground truth for videos whose every problem was noted.")
    print(f"\nfull results, misses and false alarms: {report.parent}")
    print(f"to label flags: fill the verdict column of {report.parent / 'flags_to_label.csv'} and save it as "
          f"{args.labels}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
