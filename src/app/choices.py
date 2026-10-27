"""The app's decisions that don't need a window, so they're easy to test: which files are videos,
which client a video is for, and which checks run."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

from src.engine import QCOptions, guess_client

VIDEO_EXTENSIONS = (".mp4", ".mov", ".m4v", ".mkv", ".avi", ".mxf", ".webm")
AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")     # podcasts: the audio checks only
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS + AUDIO_EXTENSIONS

# (key, menu label). Keys are the engine's check names, plus "color", "audio" and "delivery".
CHECK_TOGGLES: list[tuple[str, str]] = [
    ("too_short", "Too short / phrase breaks"),
    ("spelling", "Spelling and repeated words"),
    ("capitalization", "Capitalization"),
    ("spacing", "Spacing"),
    ("punctuation_style", "Question marks and full-stop style"),
    ("punctuation", "Punctuation (AI, costs money)"),
    ("transcript", "Captions vs audio"),
    ("screen_text", "Names vs on-screen graphics"),
    ("color", "Color grading"),
    ("audio", "Audio edits (stutters, cuts, dead air)"),
    ("delivery", "Delivery (format, loudness, black frames)"),
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
    """A file the app checks: a video, or an audio file (podcast)."""
    return path.suffix.lower() in MEDIA_EXTENSIONS


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
    videos = " ".join(f"*{ext}" for ext in VIDEO_EXTENSIONS)
    audio = " ".join(f"*{ext}" for ext in AUDIO_EXTENSIONS)
    return f"Videos and audio ({videos} {audio});;Videos ({videos});;Audio ({audio});;All files (*)"


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
                     disabled_checks=off - {"color"})          # "audio", "delivery" stay: the engine reads them
