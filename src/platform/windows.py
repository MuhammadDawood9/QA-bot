"""Windows 10 / 11."""

from __future__ import annotations

from pathlib import Path

name = "windows"


def floating_window_flags():
    """Frameless, always on top, and a Tool window, so it has no taskbar button."""
    from PySide6.QtCore import Qt

    return Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint


def prepare_floating_window(widget) -> None:
    """Nothing extra: the flags above already keep it on top and out of the taskbar."""


def after_app_created(app) -> None:
    """Nothing to do: a Tool window has no taskbar button, and Windows has no Dock."""


def tray_icon(assets_dir: Path):
    """The color tray icon (Windows shows tray icons in color), 1x and 2x for high-DPI."""
    from PySide6.QtGui import QIcon

    icon = QIcon()
    for name_ in ("tray.png", "tray@2x.png"):
        path = assets_dir / "tray" / name_
        if path.is_file():
            icon.addFile(str(path))
    return icon


def ffmpeg_install_help() -> str:
    return ("ffmpeg isn't installed, so videos can't be read.\n\n"
            "To install it:\n"
            "1. Open PowerShell (Windows key, type PowerShell).\n"
            "2. Run:  winget install ffmpeg\n"
            "3. Quit and restart Video QC Bot.\n\n"
            "If it's installed somewhere unusual, put the full path to ffmpeg.exe in "
            "machine.ffmpeg_path in config.yaml.")
