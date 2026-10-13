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


# --- start at login: a value under HKEY_CURRENT_USER\...\Run (this user only, no admin rights) ---

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "VideoQCBot"


def launch_at_login_enabled(key_path: str = RUN_KEY) -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
            return True
    except FileNotFoundError:
        return False


def set_launch_at_login(enabled: bool, command: list[str], key_path: str = RUN_KEY) -> None:
    """Add or remove the Run entry. `command` is the program and its arguments. Raises OSError."""
    import subprocess
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, subprocess.list2cmdline(command))
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass


def extend_path() -> list[str]:
    """Nothing to add: winget puts ffmpeg on the user's PATH, which apps started from the Start
    Menu or at login see."""
    return []


# --- the machine: memory, priority, processes, folders (used by the engine too; nothing here imports Qt) ---

def _kernel32():
    import ctypes

    return ctypes.WinDLL("kernel32", use_last_error=True)


def _memory_status():
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not _kernel32().GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return status


def total_ram_bytes() -> int | None:
    try:
        status = _memory_status()
        return int(status.ullTotalPhys) if status else None
    except OSError:
        return None


def available_ram_bytes() -> int | None:
    try:
        status = _memory_status()
        return int(status.ullAvailPhys) if status else None
    except OSError:
        return None


BELOW_NORMAL_PRIORITY_CLASS = 0x00004000


def lower_priority() -> bool:
    """Run this process (and the ffmpeg it starts, which inherits it) below normal priority, so
    editing and playback on the same PC stay smooth. No admin rights needed."""
    try:
        k = _kernel32()
        return bool(k.SetPriorityClass(k.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS))
    except (OSError, AttributeError):
        return False


def pid_alive(pid: int) -> bool:
    """Whether a process with this id is still running (any user's)."""
    import ctypes

    process_query_limited_information, still_active, access_denied = 0x1000, 259, 5
    try:
        k = _kernel32()
        handle = k.OpenProcess(process_query_limited_information, False, int(pid))
        if not handle:
            return ctypes.get_last_error() == access_denied     # exists, but it's someone else's
        try:
            code = ctypes.c_ulong()
            if not k.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == still_active
        finally:
            k.CloseHandle(handle)
    except (OSError, AttributeError):
        return True                      # can't tell: assume it runs (never break a live lock)


def machine_dir() -> Path:
    """A folder every user of this PC can write: %PROGRAMDATA%\\VideoQCBot (for the
    one-check-at-a-time lock)."""
    import os

    return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "VideoQCBot"


def bootstrap_dirs(executable: Path, frozen: bool) -> list[Path]:
    """Where IT can put videoqcbot-bootstrap.yaml, first match wins: next to VideoQCBot.exe, then
    %PROGRAMDATA%\\VideoQCBot (machine-wide, e.g. pushed by Intune or a GPO)."""
    dirs = [Path(executable).parent] if frozen else []
    return dirs + [machine_dir()]
