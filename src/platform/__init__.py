"""Everything that differs between Windows and macOS, behind one interface.

    from src.platform import current
    flags = current.floating_window_flags()

The app's UI code only talks to `current`; it never checks sys.platform itself. The engine
only uses the machine helpers at the end of the list. Each OS module (windows.py, macos.py, other.py) provides:

    name                                   "windows", "macos" or "other"
    floating_window_flags()                Qt window flags for the always-on-top mascot
    prepare_floating_window(widget)        anything to do once the window exists
    after_app_created(app)                 e.g. keep the app out of the macOS Dock
    tray_icon(assets_dir) -> QIcon         the tray / menu bar icon
    ffmpeg_install_help() -> str           how to install ffmpeg on this OS, for the startup check
    launch_at_login_enabled() -> bool      whether the app starts when you log in
    set_launch_at_login(enabled, command)  turn that on (with the command to run) or off
    extend_path() -> list[str]             add the folders this OS installs tools in (ffmpeg) to PATH

and, for the engine as well (these import nothing from Qt):

    total_ram_bytes(), available_ram_bytes()     None when the OS won't say
    lower_priority() -> bool               below-normal CPU priority for this process and its children
    pid_alive(pid) -> bool                 whether a process is still running (for stale locks)
    machine_dir() -> Path                  a folder every user of this computer can write
    bootstrap_dirs(executable, frozen)     where IT may put videoqcbot-bootstrap.yaml
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
