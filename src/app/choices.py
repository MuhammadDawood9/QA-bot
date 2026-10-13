"""The app's decisions that don't need a window, so they're easy to test: which files are videos,
which client a video is for, and which checks run."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

from src.engine import QCOptions, guess_client

VIDEO_EXTENSIONS = (".mp4", ".mov", ".m4v", ".mkv", ".avi", ".mxf", ".webm")

# (key, menu label). Keys are the engine's check names, plus "color".
CHECK_TOGGLES: list[tuple[str, str]] = [
    ("too_short", "Too short / phrase breaks"),
    ("spelling", "Spelling and repeated words"),
    ("capitalization", "Capitalization"),
    ("spacing", "Spacing"),
    ("punctuation", "Punctuation (AI, costs money)"),
    ("transcript", "Captions vs audio"),
    ("screen_text", "Names vs on-screen graphics"),
    ("color", "Color grading"),
]
OFF_BY_DEFAULT = {"punctuation"}          # the Anthropic API is paid per video; switched on by choice
# Shown in the Checks menu only when an admin turned them on in config.yaml (checks.<name>.enabled).
CONFIG_GATED = {"punctuation"}


def visible_toggles(config: dict) -> list[tuple[str, str]]:
    """The Checks menu: every toggle, minus the API check while config.yaml has it off."""
    checks = config.get("checks") or {}
    return [(key, label) for key, label in CHECK_TOGGLES
            if key not in CONFIG_GATED or (checks.get(key) or {}).get("enabled", False)]


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def videos_in(paths: Iterable[Path]) -> list[Path]:
    """The video files among dropped paths, each once. A dropped folder gives the videos directly
    inside it, by name (not its subfolders); other files are ignored."""
    found: list[Path] = []
    for p in paths:
        if p.is_dir():
            found += sorted((c for c in p.iterdir() if c.is_file() and is_video(c)), key=lambda c: c.name.lower())
        elif p.is_file() and is_video(p):
            found.append(p)
    return list(dict.fromkeys(found))


def file_dialog_filter() -> str:
    patterns = " ".join(f"*{ext}" for ext in VIDEO_EXTENSIONS)
    return f"Videos ({patterns});;All files (*)"


def choose_client(video: Path, last_client: str | None, guess: bool, known: Sequence[str]) -> tuple[str | None, str]:
    """The client to check a video for, and why: the code in its file name (if guessing is on and
    there is one), otherwise the last client chosen, otherwise the default rules."""
    if guess:
        found = guess_client(video, known)
        if found:
            return found, f"{found} (from the file name)"
    if last_client:
        return last_client, f"{last_client} (last used)"
    return None, "default rules"


def qc_options(disabled: Iterable[str]) -> QCOptions:
    """Engine options from the checks switched off in the menu."""
    off = frozenset(disabled)
    return QCOptions(use_llm="punctuation" not in off, check_color="color" not in off,
                     disabled_checks=off - {"color"})
