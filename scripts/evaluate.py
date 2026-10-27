"""Measure how well the bot's checks (captions, color and audio edits) match your own QC notes.

    python scripts/evaluate.py --make-template          # draft data/ground_truth.csv from the QC spreadsheet
    python scripts/evaluate.py                          # evaluate (videos looked up in data/samples/)
    python scripts/evaluate.py --videos "D:/QC videos" --tolerance 2
    python scripts/evaluate.py --checks captions        # only the caption checks (or: color, audio)
    python scripts/evaluate.py --checks audio --audio-tolerance 1.5      # podcasts (E2CL)
    python scripts/evaluate.py --checks audio --compare-suppression      # and again without the overlap /
                                                                           # music / effect suppression

data/ground_truth.csv columns: video, timestamp, client, issue_type, note_text
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

Matching: a flag matches a note when the note's time (or range) falls inside the flagged caption
or shot, widened by --tolerance seconds on each side. Caption notes are matched only by caption
flags and color notes only by color flags. A caption / color_grading note is matched by a flag
of any type in its area. Per check type:
  recall      notes of that type that some flag of that type matched / notes of that type
              (the "all captions" / "all color" rows: every timed note of the area, matched by
              any flag of the area)
  precision   flags of that type that match a note of that type or a generic note / all flags
  FP / min    flags that match no note, per minute of evaluated video
Notes without a timestamp can't be matched to a moment; they're listed separately with whether
the bot flagged anything at all in that video.

Writes reports/evaluation_<date-time>/: evaluation.md, misses.csv, false_alarms.csv.
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
                        kind, (row.get("note_text") or "").strip())
            if note.stamp:
                try:
                    start, end = parse_range(note.stamp, fps=infer_fps(note.stamp, fps))
                    note.start, note.end = start, (end if end is not None else start)
                except TimecodeError as exc:
                    problems.append(f"line {n}: {exc}; treated as a note about the whole video")
            notes.append(note)
    return notes, problems


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

def match(notes: list[Note], flags: list[Flag], tolerance: float, audio_tolerance: float | None = None) -> None:
    for flag in flags:
        tol = audio_tolerance if audio_tolerance is not None and flag.area == "audio" else tolerance
        lo, hi = flag.start - tol, flag.end + tol
        for note in notes:
            if note.start is None or note.kind == "none" or note.area != flag.area:
                continue
            if note.end >= lo and note.start <= hi:              # overlap, ranges included
                note.matched_by.append(flag.issue)
                flag.matches.append(note)


def score(notes: list[Note], flags: list[Flag], minutes: float) -> list[dict]:
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
                mine = area_flags
                true = [f for f in mine if f.matches]
            else:
                relevant = [n for n in area_notes if n.kind == kind]
                found = [n for n in relevant if any(family(i.type) == kind for i in n.matched_by)]
                mine = [f for f in area_flags if f.kind == kind]
                true = [f for f in mine if any(n.kind in (kind, generic(name)) for n in f.matches)]
            rows.append({
                "type": kind, "area": name, "notes": len(relevant), "found": len(found), "flags": len(mine),
                "true": len(true), "recall": len(found) / len(relevant) if relevant else None,
                "precision": len(true) / len(mine) if mine else None,
                "fp_per_min": (len(mine) - len(true)) / minutes if minutes else None,
            })
    return rows


def pct(x) -> str:
    return "n/a" if x is None else f"{x:.0%}"


# --- output ---------------------------------------------------------------------------------

def write_outputs(folder: Path, rows: list[dict], notes: list[Note], flags: list[Flag], videos: dict,
                  problems: list[str], tolerance: float, audio_tolerance: float = 1.5) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    misses = [n for n in notes if n.start is not None and n.kind != "none" and not n.matched_by]
    false_alarms = [f for f in flags if not f.matches]
    untimed = [n for n in notes if n.start is None and n.kind != "none"]

    with (folder / "misses.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["video", "timestamp", "seconds", "client", "issue_type", "note_text"])
        for n in misses:
            w.writerow([n.video, n.stamp, f"{n.start:.2f}", n.client, n.kind, n.text])
    with (folder / "false_alarms.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["video", "timestamp", "seconds", "caption or shot", "issue_type", "severity", "message"])
        for fl in false_alarms:
            w.writerow([fl.video, format_timestamp(fl.issue.timestamp), f"{fl.issue.timestamp:.2f}",
                        fl.issue.original, fl.issue.type, fl.issue.severity, fl.issue.message])

    total_min = sum(v["minutes"] for v in videos.values())
    lines = [f"# Check evaluation, {dt.datetime.now():%Y-%m-%d %H:%M}", "",
             f"{len(videos)} video(s), {total_min:.1f} minutes; {sum(1 for n in notes if n.start is not None)} timed "
             f"note(s), {len(untimed)} untimed; matching tolerance ±{tolerance:g} s around each flagged caption or shot, "
             f"±{audio_tolerance:g} s around each audio flag.",
             "", "Caption flags come from every caption check except the paid punctuation one (no API calls).", "",
             "| Check | Notes | Found | Recall | Flags | True | Precision | False alarms / min |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        if r["notes"] or r["flags"]:
            fp = "n/a" if r["fp_per_min"] is None else f"{r['fp_per_min']:.2f}"
            lines.append(f"| {r['type']} | {r['notes']} | {r['found']} | {pct(r['recall'])} | {r['flags']} | "
                         f"{r['true']} | {pct(r['precision'])} | {fp} |")
    lines += ["", "## Videos", "", "| Video | Minutes | Flags | Notes | Analysis time |", "|---|---:|---:|---:|---|"]
    for name, v in videos.items():
        took = "from cache" if v["took"] == 0 else f"{v['took']:.0f} s ({v['took'] / max(v['minutes'], 1e-9):.0f} s per minute)"
        lines.append(f"| {name} | {v['minutes']:.1f} | {v['flags']} | {v['notes']} | {took} |")
    lines += ["", f"## Misses ({len(misses)})", "", "Notes no flag of the right type matched.", ""]
    lines += [f"- **{n.video}** {n.stamp} [{n.kind}] {n.text}" for n in misses] or ["- none"]
    lines += ["", f"## False alarms ({len(false_alarms)})", "", "Flags that match no note.", ""]
    lines += [f"- **{f.video}** {format_timestamp(f.issue.timestamp)} {f.issue.original} [{f.issue.type}, "
              f"{f.issue.severity}] {f.issue.message}" for f in false_alarms] or ["- none"]
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
        w.writerow(COLUMNS)
        for task, stamp, client, kind, text in rows:
            # The video column starts as the task name: replace it with the video's file name.
            w.writerow([f"TODO {task}".strip(), stamp, client, kind, text])

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
    print("Replace each 'TODO <task name>' with the video's file name, check the guessed issue_type, and "
          "delete rows for videos you don't have.")
    return 0


def compare_suppression(report: Path, rows: list[dict], notes: list[Note], by_video: dict, folders: list[Path],
                        config: dict, tolerance: float, minutes: float) -> None:
    """The audio rows again with suppression off; a before/after table in evaluation.md and on screen."""
    from dataclasses import replace

    fresh = [replace(n, matched_by=[]) for n in notes]
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
    off = {r["type"]: r for r in score(fresh, flags, minutes) if r["area"] == "audio"}
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
        match(video_notes, flags, args.tolerance, args.audio_tolerance)
        all_flags += flags
        videos[name] = {"minutes": length / 60, "flags": len(flags), "notes": len(video_notes), "took": took}

    evaluated = [n for n in notes if n.kind != "none" or n.video in videos]
    minutes = sum(v["minutes"] for v in videos.values())
    rows = score(evaluated, all_flags, minutes)
    folder = Path(config["_root"]) / config.get("report", {}).get("output_dir", "reports") / \
        f"evaluation_{dt.datetime.now():%Y%m%d-%H%M%S}"
    report = write_outputs(folder, rows, evaluated, all_flags, videos, problems, args.tolerance, args.audio_tolerance)
    if args.compare_suppression and "audio" in wanted:
        compare_suppression(report, rows, evaluated, by_video, folders, config, args.audio_tolerance, minutes)

    print()
    print(f"{'check':<16}{'notes':>6}{'found':>6}{'recall':>8}{'flags':>7}{'true':>6}{'precision':>11}{'FP/min':>8}")
    for r in rows:
        if r["notes"] or r["flags"]:
            fp = "n/a" if r["fp_per_min"] is None else f"{r['fp_per_min']:.2f}"
            print(f"{r['type']:<16}{r['notes']:>6}{r['found']:>6}{pct(r['recall']):>8}{r['flags']:>7}{r['true']:>6}"
                  f"{pct(r['precision']):>11}{fp:>8}")
    for p in problems:
        print(f"note: {p}")
    print(f"\nfull results, misses and false alarms: {report.parent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
