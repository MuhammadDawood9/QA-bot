"""Everything that differs between Windows and macOS, behind one interface.

    from src.platform import current
    flags = current.floating_window_flags()

The app's UI code only talks to `current`; it never checks sys.platform itself. The engine
never imports this package. Each OS module (windows.py, macos.py, other.py) provides:

    name                                   "windows", "macos" or "other"
    floating_window_flags()                Qt window flags for the always-on-top mascot
    prepare_floating_window(widget)        anything to do once the window exists
    after_app_created(app)                 e.g. keep the app out of the macOS Dock
    tray_icon(assets_dir) -> QIcon         the tray / menu bar icon
    ffmpeg_install_help() -> str           how to install ffmpeg on this OS, for the startup check
"""

from __future__ import annotations

import sys

if sys.platform == "win32":
    from src.platform import windows as current
elif sys.platform == "darwin":
    from src.platform import macos as current
else:
    from src.platform import other as current

__all__ = ["current"]
