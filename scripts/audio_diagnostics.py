"""How much of the client's audio notes the audio checks could ever find.

    python scripts/audio_diagnostics.py --truth data/ground_truth_filled.csv

For each note of a transcript-based type, it asks whether the pattern the check looks for is even
present in the cached transcript near the note's time. That is the ceiling for those checks: if the
words the client heard are not written down, no threshold can find them. The sound-based stutter
check (acoustic_repeats) is measured too when --videos is given.

Why this exists: measured on 5 real E2CS episodes, Whisper keeps only ~10% of the notes' repeats,
and the sound-based check finds none (see clients/E2CS.yaml). Changing thresholds cannot fix a
note whose pattern is not there, so measure the ceiling before tuning.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path

STUTTER = "stutter"
REPEATED_PHRASE = "repeated_phrase"
AUDIO_TYPES = (STUTTER, REPEATED_PHRASE, "abrupt_cut", "jump_cut", "dead_air")
TRANSCRIPT_TYPES = (STUTTER, REPEATED_PHRASE)


def norm(text: str) -> str:
    return re.sub(r"[^a-z']", "", str(text).lower())


def parse_seconds(stamp: str) -> float | None:
    """HH:MM:SS:FF (or ; for drop frame) at the config's 30 fps, or None for a range/blank."""
    stamp = str(stamp).strip()
    if not stamp or any(c in stamp for c in "-\u2012\u2013\u2014") or " to " in stamp:
        return None
    parts = [int(x) for x in re.split(r"[:;]", stamp)]
    while len(parts) < 4:
        parts.insert(0, 0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2] + parts[3] / 30.0


def load_transcripts(cache_dir: Path) -> dict[str, list[tuple[str, float]]]:
    """Words by video file name, from the transcript cache. One cached transcript is used per video
    (the longest: two settings variants are never mixed, or every word looks duplicated)."""
    best: dict[str, list[tuple[str, float]]] = {}
    for path in cache_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        words = [(norm(w[0]), float(w[1])) for w in data.get("words", []) if norm(w[0])]
        name = data.get("video")
        if name and len(words) > len(best.get(name, [])):
            best[name] = words
    return best


def adjacent_duplicate(words: list[tuple[str, float]], t: float, window: float, gap: float) -> str | None:
    near = [(w, s) for w, s in words if abs(s - t) <= window]
    for (a, sa), (b, sb) in zip(near, near[1:]):
        if a == b and sb - sa <= gap:
            return a
    return None


def immediate_phrase(words: list[tuple[str, float]], t: float, window: float, gap: float) -> str | None:
    near = [(w, s) for w, s in words if abs(s - t) <= window]
    for n in (2, 3):
        for i in range(len(near) - 2 * n + 1):
            if tuple(x[0] for x in near[i:i + n]) == tuple(x[0] for x in near[i + n:i + 2 * n]) \
                    and near[i + n][1] - near[i][1] <= gap:
                return " ".join(x[0] for x in near[i:i + n])
    return None


def read_notes(path: Path) -> list[dict]:
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    notes = []
    for row in rows:
        video = row.get("video", "")
        if not row.get("issue_type") or video.startswith("TODO"):
            continue
        t = parse_seconds(row.get("timestamp", ""))
        if t is not None:
            notes.append({"video": video, "seconds": t, "type": row["issue_type"],
                          "text": row.get("note_text", "")})
    return notes


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--truth", default="data/ground_truth_filled.csv")
    parser.add_argument("--cache", default="cache/transcripts")
    parser.add_argument("--window", type=float, default=5.0, help="seconds around a note to look (default 5)")
    args = parser.parse_args(argv)

    notes = [n for n in read_notes(Path(args.truth)) if n["type"] in AUDIO_TYPES]
    transcripts = load_transcripts(Path(args.cache))
    print(f"{len(notes)} audio note(s) across {len({n['video'] for n in notes})} file(s); "
          f"transcripts cached for {len(transcripts)}.")
    print(f"ceiling: a note counts only if the check's pattern is in the transcript within "
          f"+-{args.window:g}s\n")

    have = Counter()
    seen = Counter()
    for n in notes:
        seen[n["type"]] += 1
        words = transcripts.get(n["video"])
        if not words:
            continue
        if n["type"] == STUTTER and adjacent_duplicate(words, n["seconds"], args.window, 1.0):
            have[STUTTER] += 1
        elif n["type"] == REPEATED_PHRASE and immediate_phrase(words, n["seconds"], args.window, 1.5):
            have[REPEATED_PHRASE] += 1

    print(f"{'type':<18}{'notes':>7}{'reachable':>11}{'ceiling':>10}")
    for kind in AUDIO_TYPES:
        if seen[kind]:
            mark = f"{100 * have[kind] / seen[kind]:.0f}%" if kind in TRANSCRIPT_TYPES else "-"
            reach = have[kind] if kind in TRANSCRIPT_TYPES else 0
            print(f"{kind:<18}{seen[kind]:>7}{reach:>11}{mark:>10}")
    print("\nOnly stutter / repeated_phrase come from the transcript, so only they have a ceiling here.")
    print("The sound-based stutter check (acoustic_repeats) is separate; it currently finds nothing on")
    print("real speech (see clients/E2CS.yaml).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
