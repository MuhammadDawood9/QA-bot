"""Fill data/ground_truth.csv's "video" column from a folder of collected media files.

    python scripts/match_videos.py                      # match against data/samples/
    python scripts/match_videos.py --videos "D:/QC videos" --videos "D:/more"
    python scripts/match_videos.py --min-score 0.6

The ground truth template starts with a placeholder in the "video" column of every row
("TODO <task name>"). Rather than hand-editing all of them, drop the collected videos (or
podcast audio) in a folder and let this script match each file to a task by name.

It reads data/ground_truth.csv and data/ground_truth_videos.csv and writes *new* files, so
nothing you have is overwritten:

    data/ground_truth_filled.csv          every "TODO <task>" replaced by the matched file name
    data/ground_truth_videos_filled.csv   the collection list with its video_file column filled

Then evaluate the bot with the filled truth:

    python scripts/evaluate.py --truth data/ground_truth_filled.csv --videos "D:/QC videos"

Matching is by file name only (the evaluator looks files up by name under --videos), so the
files may sit in any sub-folder. A task is matched to the file whose name it best explains;
revision numbers are kept, so "Wedding (Revision 01)" and "(Revision 02)" don't collide.
Rows whose task has no file yet stay as "TODO <task>".
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_TRUTH = ROOT / "data" / "ground_truth.csv"
DEFAULT_VIDEOS = ROOT / "data" / "ground_truth_videos.csv"

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm", ".mts", ".mpg", ".mpeg"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
MEDIA_EXT = VIDEO_EXT | AUDIO_EXT

# Words that don't tell one task from another; revision numbers and client codes are kept.
STOPWORDS = {"the", "and", "of", "to", "a", "an", "for", "with", "video", "videos",
             "reel", "reels", "episode", "ep", "fx", "p", "final", "export", "render"}

POSSESSIVE = re.compile(r"['\u2019]s\b")


def words(text: str) -> set[str]:
    """Lowercase words of a name, minus punctuation and the words that don't distinguish tasks.

    "David's" -> "david", "Davids" -> "david", "Rev" -> "revision" so a file's loose naming still
    lines up with the task, while revision numbers ("02") are kept so revisions don't collide.
    """
    out = set()
    for token in re.split(r"[^0-9a-z]+", POSSESSIVE.sub("", text.lower())):
        if not token or token in STOPWORDS:
            continue
        if len(token) > 3 and token.endswith("s"):
            token = token[:-1]
        out.add("revision" if token in {"rev", "revision"} else token)
    return out


def task_name(placeholder: str) -> str:
    """"TODO #E3AC ..." -> "#E3AC ..."; anything else is returned unchanged."""
    return re.sub(r"^TODO\s+", "", placeholder).strip()


def media_files(folders: list[Path]) -> list[Path]:
    found: dict[str, Path] = {}
    for folder in folders:
        if not folder.is_dir():
            print(f"note: {folder} isn't a folder, skipping", file=sys.stderr)
            continue
        for path in folder.rglob("*"):
            # Skip macOS junk in zips unpacked on a Mac: the "._name" AppleDouble sidecars and the
            # __MACOSX folder they live in. They share the media extension but hold no audio.
            if any(part == "__MACOSX" for part in path.parts) or path.name.startswith("._"):
                continue
            if path.is_file() and path.suffix.lower() in MEDIA_EXT:
                found.setdefault(path.name.lower(), path)
    return sorted(found.values(), key=lambda p: p.name.lower())


def best_match(task: str, client: str, files: list[Path]) -> tuple[Path | None, float, set]:
    """The file whose name is best explained by the task, its coverage, and the words they share.

    Scored by the share of the *file's* words that also appear in the task, so a task's extra
    words (client code, date, "Revision") don't dilute the score, while a file with words the
    task knows nothing about ("random_clip") is not accepted.
    """
    task_words = words(task) | (words(client) if client else set())
    if not task_words:
        return None, 0.0, set()
    best, best_score, best_shared = None, 0.0, set()
    for path in files:
        file_words = words(path.stem)
        if not file_words:
            continue
        shared = task_words & file_words
        score = len(shared) / len(file_words)
        if score > best_score or (score == best_score and len(shared) > len(best_shared)):
            best, best_score, best_shared = path, score, shared
    return best, best_score, best_shared


def token_tasks(tasks: dict[str, str]) -> dict[str, int]:
    """How many tasks each word appears in, so a lone distinctive word ("316") can match alone."""
    counts: dict[str, int] = {}
    for task, client in tasks.items():
        for word in words(task) | (words(client) if client else set()):
            counts[word] = counts.get(word, 0) + 1
    return counts


def read_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_rows(path: Path, columns: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def match(truth: Path, videos: Path, folders: list[Path], out: Path, videos_out: Path,
          min_score: float) -> int:
    if not truth.is_file():
        raise SystemExit(f"error: {truth} not found")
    rows = read_rows(truth)
    columns = list(rows[0]) if rows else ["video", "timestamp", "client", "issue_type", "note_text"]

    files = media_files(folders)
    print(f"{len(files)} media file(s) under {', '.join(str(f) for f in folders)}")

    # One assignment per task: the best-scoring pair wins, and each file is used once.
    tasks: dict[str, str] = {}                       # task -> client
    for row in rows:
        name = task_name(row.get("video", ""))
        if name:
            tasks.setdefault(name, (row.get("client") or "").strip())
    counts = token_tasks(tasks)

    pairs = []
    for task, client in tasks.items():
        file, score, shared = best_match(task, client, files)
        if file is None or score < min_score:
            continue
        # Two shared words are a safe match; one word is enough only when the file is named with
        # just it and that word names exactly one task (e.g. a bare "316.mp3").
        lone = len(shared) == 1 and len(words(file.stem)) == 1 and counts.get(next(iter(shared)), 0) == 1
        if len(shared) >= 2 or lone:
            pairs.append((-score, -len(shared), task, file))
    pairs.sort()
    assigned: dict[str, Path] = {}
    users: dict[Path, list[str]] = {}                # a file may serve several tasks of one episode
    for _, _, task, file in pairs:
        if task in assigned:
            continue
        assigned[task] = file
        users.setdefault(file, []).append(task)

    for row in rows:
        name = task_name(row.get("video", ""))
        if name in assigned:
            row["video"] = assigned[name].name
    write_rows(out, columns, rows)

    if videos.is_file():
        vrows = read_rows(videos)
        vcolumns = list(vrows[0]) if vrows else ["task_name", "client", "video_file"]
        for vrow in vrows:
            file = assigned.get((vrow.get("task_name") or "").strip())
            if file is not None:
                vrow["video_file"] = file.name
        write_rows(videos_out, vcolumns, vrows)
    else:
        vcolumns = []

    matched = len(assigned)
    print(f"matched {matched} of {len(tasks)} task(s); wrote {out}" + (f" and {videos_out}" if vcolumns else ""))
    for task in sorted(set(tasks) - set(assigned)):
        print(f"  no file yet: {tasks[task]} {task}")
    for file, task_names in users.items():
        if len(task_names) > 1:
            print(f"  shared by {len(task_names)} tasks: {file.name}")
    for path in files:
        if path not in users:
            print(f"  not used:    {path.name}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--truth", type=Path, default=DEFAULT_TRUTH,
                        help="the ground truth to fill (default data/ground_truth.csv)")
    parser.add_argument("--videos", type=Path, default=DEFAULT_VIDEOS,
                        help="the collection list (default data/ground_truth_videos.csv)")
    parser.add_argument("--folder", type=Path, action="append", dest="folders",
                        help="folder to look for media in (repeatable; default data/samples)")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "ground_truth_filled.csv")
    parser.add_argument("--videos-out", type=Path, default=ROOT / "data" / "ground_truth_videos_filled.csv")
    parser.add_argument("--min-score", type=float, default=0.5,
                        help="how much of a file's name the task must explain (default 0.5)")
    args = parser.parse_args(argv)

    folders = args.folders or [ROOT / "data" / "samples"]
    return match(args.truth, args.videos, folders, args.out, args.videos_out, args.min_score)


if __name__ == "__main__":
    raise SystemExit(main())
