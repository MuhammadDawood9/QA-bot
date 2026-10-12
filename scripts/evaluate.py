"""Measure how well the color checks match your own QC notes.

    python scripts/evaluate.py --make-template          # draft data/ground_truth.csv from the QC spreadsheet
    python scripts/evaluate.py                          # evaluate (videos looked up in data/samples/)
    python scripts/evaluate.py --videos "D:/QC videos" --tolerance 2

data/ground_truth.csv columns: video, timestamp, client, issue_type, note_text
  video       the video's file name (looked up under --videos, recursively) or a full path
  timestamp   as you wrote it: 00:00:19:59, 0:29, 24:37, 00;10;17;08, or a range 2:04-2:15.
              Leave it empty for a note about the whole video ("fix color grade").
  client      client code, for that client's settings (E3AC)
  issue_type  one of the bot's color types (overexposed, underexposed, crushed_blacks, flat_contrast,
              saturation, color_cast, skin_tone, shot_mismatch), or color_grading when the note
              doesn't say which. A video with no notes at all can be listed with an empty timestamp
              and issue_type "none": it then only counts toward false alarms.
  note_text   your comment, shown next to misses so you can look at each one

Matching: a flag matches a note when the note's time (or range) falls inside the flagged shot,
widened by --tolerance seconds on each side. A color_grading note is matched by a flag of any
color type. Per check type:
  recall      notes of that type that some flag of that type matched / notes of that type
              (the "all color" row: every timed note, matched by a flag of any color type)
  precision   flags of that type that match a note of that type or a color_grading note / all flags
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
from src.models import COLOR_ISSUES, Issue, shot_number  # noqa: E402
from src.utils.timecode import TimecodeError, format_timestamp, infer_fps, parse_range  # noqa: E402

GENERIC = {"color_grading", "color", "colour", "colour_grading", ""}
ALL = "all color"
COLUMNS = ["video", "timestamp", "client", "issue_type", "note_text"]
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mxf", ".m4v", ".mkv", ".avi"}


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


@dataclass
class Flag:
    video: str
    issue: Issue
    start: float                    # the flagged shot (or moment)
    end: float
    matches: list[Note] = field(default_factory=list)


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
            kind = "color_grading" if kind in GENERIC else kind
            if kind not in COLOR_ISSUES | {"color_grading", "none"}:
                problems.append(f"line {n}: issue_type {kind!r} isn't a color type; counted as color_grading")
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


# --- matching and scoring -------------------------------------------------------------------

def match(notes: list[Note], flags: list[Flag], tolerance: float) -> None:
    for flag in flags:
        lo, hi = flag.start - tolerance, flag.end + tolerance
        for note in notes:
            if note.start is None or note.kind == "none":
                continue
            if note.end >= lo and note.start <= hi:              # overlap, ranges included
                note.matched_by.append(flag.issue)
                flag.matches.append(note)


def score(notes: list[Note], flags: list[Flag], minutes: float) -> list[dict]:
    rows = []
    timed = [n for n in notes if n.start is not None and n.kind != "none"]
    for kind in sorted(COLOR_ISSUES) + [ALL]:
        if kind == ALL:
            relevant = timed
            found = [n for n in relevant if n.matched_by]
            mine = flags
            true = [f for f in mine if f.matches]
        else:
            relevant = [n for n in timed if n.kind == kind]
            found = [n for n in relevant if any(i.type == kind for i in n.matched_by)]
            mine = [f for f in flags if f.issue.type == kind]
            true = [f for f in mine if any(n.kind in (kind, "color_grading") for n in f.matches)]
        rows.append({
            "type": kind, "notes": len(relevant), "found": len(found), "flags": len(mine), "true": len(true),
            "recall": len(found) / len(relevant) if relevant else None,
            "precision": len(true) / len(mine) if mine else None,
            "fp_per_min": (len(mine) - len(true)) / minutes if minutes else None,
        })
    return rows


def pct(x) -> str:
    return "n/a" if x is None else f"{x:.0%}"


# --- output ---------------------------------------------------------------------------------

def write_outputs(folder: Path, rows: list[dict], notes: list[Note], flags: list[Flag], videos: dict,
                  problems: list[str], tolerance: float) -> Path:
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
        w.writerow(["video", "timestamp", "seconds", "shot", "issue_type", "severity", "message"])
        for fl in false_alarms:
            w.writerow([fl.video, format_timestamp(fl.issue.timestamp), f"{fl.issue.timestamp:.2f}",
                        fl.issue.original, fl.issue.type, fl.issue.severity, fl.issue.message])

    total_min = sum(v["minutes"] for v in videos.values())
    lines = [f"# Color check evaluation, {dt.datetime.now():%Y-%m-%d %H:%M}", "",
             f"{len(videos)} video(s), {total_min:.1f} minutes; {sum(1 for n in notes if n.start is not None)} timed "
             f"note(s), {len(untimed)} untimed; matching tolerance ±{tolerance:g} s around each flagged shot.", "",
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
                  and not any(n.kind in (f.issue.type, "color_grading") for n in f.matches)]
    lines += ["", f"## Right moment, different type ({len(other_type)})", "",
              "Flags on a noted moment whose note names another problem. They count as found for "
              "\"all color\" but as false alarms in their own row.", ""]
    lines += [f"- **{f.video}** {format_timestamp(f.issue.timestamp)} {f.issue.original} [{f.issue.type}] "
              f"vs note [{', '.join(sorted({n.kind for n in f.matches}))}]: {f.issue.message}"
              for f in other_type] or ["- none"]
    if untimed:
        lines += ["", f"## Notes without a timestamp ({len(untimed)})", ""]
        for n in untimed:
            count = videos.get(n.video, {}).get("flags", 0)
            lines.append(f"- **{n.video}** [{n.kind}] {n.text} (bot flagged {count} shot(s) in this video)")
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


def make_template(xlsx: Path, out: Path) -> int:
    import openpyxl

    if out.exists():
        raise SystemExit(f"error: {out} already exists; move it away first so your edits aren't overwritten")
    sheet = openpyxl.load_workbook(xlsx, read_only=True)["All QC notes (1531)"]
    rows = list(sheet.iter_rows(values_only=True))
    header = [str(h) for h in rows[0]]
    col = {name: header.index(name) for name in ("task_name", "client", "video_timestamp", "category", "comment_text")}
    written = 0
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for r in rows[1:]:
            if r[col["category"]] != "color_grading":
                continue
            text = str(r[col["comment_text"]] or "").strip()
            kind = next((k for k, pattern in KEYWORDS if re.search(pattern, text, re.I)), "color_grading")
            stamp = str(r[col["video_timestamp"]] or "").strip()
            # The video column starts as the task name: replace it with the video's file name.
            w.writerow([f"TODO {r[col['task_name']]}".strip(), stamp, r[col["client"]] or "", kind, text])
            written += 1
    print(f"wrote {out} with {written} color notes. Replace each 'TODO <task name>' with the video's file "
          f"name, check the guessed issue_type, and delete rows for videos you don't have.")
    return 0


# --- main -----------------------------------------------------------------------------------

def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Evaluate the color checks against your QC notes.")
    parser.add_argument("--truth", type=Path, default=ROOT / "data" / "ground_truth.csv")
    parser.add_argument("--videos", type=Path, action="append",
                        help="folder to look for the videos in (repeatable; default data/samples and data)")
    parser.add_argument("--tolerance", type=float, default=2.0, help="seconds either side of a flagged shot")
    parser.add_argument("--make-template", action="store_true",
                        help="write a draft ground_truth.csv from the color notes in the QC spreadsheet")
    parser.add_argument("--xlsx", type=Path, default=ROOT / "data" / "QC_Review_Analysis_Jun-Oct_2026.xlsx")
    args = parser.parse_args(argv)

    if args.make_template:
        return make_template(args.xlsx, args.truth)
    if not args.truth.is_file():
        raise SystemExit(f"error: {args.truth} not found. Make a draft with --make-template")

    config = load_config()
    notes, problems = read_truth(args.truth, float(config.get("timecode", {}).get("fps", 30)))
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
        print(f"checking {path.name} (client {client or 'default'})...")
        flags, length, took = color_flags(path, client, config)
        match(video_notes, flags, args.tolerance)
        all_flags += flags
        videos[name] = {"minutes": length / 60, "flags": len(flags), "notes": len(video_notes), "took": took}

    evaluated = [n for n in notes if n.kind != "none" or n.video in videos]
    minutes = sum(v["minutes"] for v in videos.values())
    rows = score(evaluated, all_flags, minutes)
    folder = Path(config["_root"]) / config.get("report", {}).get("output_dir", "reports") / \
        f"evaluation_{dt.datetime.now():%Y%m%d-%H%M%S}"
    report = write_outputs(folder, rows, evaluated, all_flags, videos, problems, args.tolerance)

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
