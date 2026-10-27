"""macOS (Intel and Apple Silicon).

Two things Qt can't do on its own, done with the Objective-C runtime through ctypes (no extra
package needed):
  - keep the app out of the Dock and the app switcher (activation policy "accessory"), the way
    menu bar apps behave. A packaged app also sets LSUIElement in its Info.plist (Step 4).
  - let the mascot window follow you to every desktop (Space) and sit beside full-screen apps
    where macOS allows it.
Every native call is wrapped: if one fails, the app still runs, just with the Qt defaults.

Limitation (documented in the README): macOS gives each full-screen app its own Space. The
mascot is marked "full-screen auxiliary", which lets it appear there for some apps, but macOS
doesn't guarantee it; it may stay on the desktop until you leave full screen.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
from pathlib import Path

log = logging.getLogger("qcbot.app")

name = "macos"

# AppKit constants
_ACTIVATION_POLICY_ACCESSORY = 1
_CAN_JOIN_ALL_SPACES = 1 << 0
_STATIONARY = 1 << 4
_FULL_SCREEN_AUXILIARY = 1 << 8


def floating_window_flags():
    """Frameless and always on top. A Tool window keeps it out of the window list; on macOS a
    Tool window would hide whenever another app is active, which prepare_floating_window stops."""
    from PySide6.QtCore import Qt

    return Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint


def prepare_floating_window(widget) -> None:
    from PySide6.QtCore import Qt

    from PySide6.QtGui import QGuiApplication

    widget.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)   # stay visible when inactive
    if QGuiApplication.platformName() != "cocoa":
        return      # e.g. offscreen in the tests: winId() isn't an NSView, and messaging it crashes
    try:
        objc = _Objc()
        ns_view = ctypes.c_void_p(int(widget.winId()))
        ns_window = objc.send(ns_view, "window")
        if ns_window:
            objc.send(ns_window, "setCollectionBehavior:", _CAN_JOIN_ALL_SPACES | _STATIONARY | _FULL_SCREEN_AUXILIARY,
                      argtypes=[ctypes.c_ulong], restype=None)
            objc.send(ns_window, "setHidesOnDeactivate:", False, argtypes=[ctypes.c_bool], restype=None)
    except (OSError, AttributeError, ValueError) as exc:
        log.warning("couldn't set the mascot to show on every desktop: %s", exc)


def after_app_created(app) -> None:
    """No Dock icon, no app switcher entry: the mascot and the menu bar icon are the app."""
    try:
        objc = _Objc()
        ns_app = objc.send(objc.cls("NSApplication"), "sharedApplication")
        objc.send(ns_app, "setActivationPolicy:", _ACTIVATION_POLICY_ACCESSORY,
                  argtypes=[ctypes.c_long], restype=ctypes.c_bool)
    except (OSError, AttributeError, ValueError) as exc:
        log.warning("couldn't hide the Dock icon: %s", exc)


def tray_icon(assets_dir: Path):
    """The black "template" image, which macOS recolors for light and dark menu bars."""
    from PySide6.QtGui import QIcon

    icon = QIcon()
    for name_ in ("trayTemplate.png", "trayTemplate@2x.png"):
        path = assets_dir / "tray" / name_
        if path.is_file():
            icon.addFile(str(path))
    icon.setIsMask(True)
    return icon


class _Objc:
    """Just enough of the Objective-C runtime to send a few messages."""

    def __init__(self):
        path = ctypes.util.find_library("objc")
        if not path:
            raise OSError("the Objective-C runtime wasn't found")
        self._lib = ctypes.cdll.LoadLibrary(path)
        self._lib.objc_getClass.restype = ctypes.c_void_p
        self._lib.objc_getClass.argtypes = [ctypes.c_char_p]
        self._lib.sel_registerName.restype = ctypes.c_void_p
        self._lib.sel_registerName.argtypes = [ctypes.c_char_p]

    def cls(self, class_name: str) -> ctypes.c_void_p:
        return ctypes.c_void_p(self._lib.objc_getClass(class_name.encode()))

    def send(self, receiver, selector: str, *args, argtypes=(), restype=ctypes.c_void_p):
        # objc_msgSend must be called through a prototype with the exact argument types: on Apple
        # Silicon a variadic call passes them in the wrong registers.
        prototype = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)
        send = prototype(("objc_msgSend", self._lib))
        result = send(receiver, ctypes.c_void_p(self._lib.sel_registerName(selector.encode())), *args)
        return ctypes.c_void_p(result) if restype is ctypes.c_void_p else result


def ffmpeg_install_help() -> str:
    return ("ffmpeg isn't installed, so videos can't be read.\n\n"
            "To install it:\n"
            "1. Open Terminal (Cmd-Space, type Terminal).\n"
            "2. If you don't have Homebrew yet, install it from https://brew.sh\n"
            "3. Run:  brew install ffmpeg\n"
            "4. Quit and restart Video QC Bot.\n\n"
            "If it's installed somewhere unusual, put the full path to ffmpeg in "
            "machine.ffmpeg_path in config.yaml.")


# --- start at login: a LaunchAgent in ~/Library/LaunchAgents (this user only) ---

AGENT_LABEL = "com.videoqcbot.companion"


def _agent_path(agents_dir: Path | None = None) -> Path:
    return (agents_dir or Path.home() / "Library" / "LaunchAgents") / f"{AGENT_LABEL}.plist"


def launch_at_login_enabled(agents_dir: Path | None = None) -> bool:
    return _agent_path(agents_dir).is_file()


def set_launch_at_login(enabled: bool, command: list[str], agents_dir: Path | None = None) -> None:
    """Write or remove the LaunchAgent; macOS runs it at the next login. Raises OSError."""
    import plistlib

    path = _agent_path(agents_dir)
    if not enabled:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    agent = {
        "Label": AGENT_LABEL,
        "ProgramArguments": [str(part) for part in command],
        "RunAtLoad": True,
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": "Aqua",              # only in a graphical login, not over ssh
    }
    with path.open("wb") as f:
        plistlib.dump(agent, f)


# Apps started from Finder or at login get a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin), so
# tools installed with Homebrew aren't found unless their folders are added.
TOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")      # Homebrew on Apple Silicon, on Intel


def extend_path() -> list[str]:
    """Add Homebrew's folders to this process's PATH (for ffmpeg). Returns the folders added."""
    import os

    current = os.environ.get("PATH", "").split(os.pathsep)
    added = [d for d in TOOL_DIRS if d not in current and Path(d).is_dir()]
    if added:
        os.environ["PATH"] = os.pathsep.join(current + added)
    return added


# --- the machine: memory, priority, processes, folders (used by the engine too; nothing here imports Qt) ---

def total_ram_bytes() -> int | None:
    import os

    try:
        return int(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
    except (OSError, ValueError, AttributeError):
        return None


def available_ram_bytes() -> int | None:
    """Free + inactive + speculative pages from vm_stat (what macOS can hand out without swapping)."""
    import re
    import subprocess

    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    size = re.search(r"page size of (\d+) bytes", out)
    pages = 0
    for label in ("Pages free", "Pages inactive", "Pages speculative"):
        m = re.search(rf"{label}:\s+(\d+)", out)
        pages += int(m.group(1)) if m else 0
    return pages * int(size.group(1)) if size and pages else None


def lower_priority() -> bool:
    """Lower this process's priority (nice 10; ffmpeg inherits it), so editing and playback on the
    same Mac stay smooth. macOS doesn't let a normal user raise it back, so it stays for the
    app's lifetime; the app is idle between checks anyway."""
    import os

    try:
        if os.getpriority(os.PRIO_PROCESS, 0) < 10:
            os.setpriority(os.PRIO_PROCESS, 0, 10)
        return True
    except (OSError, AttributeError):
        return False


def pid_alive(pid: int) -> bool:
    import os

    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True                  # PermissionError: another user's process
    return True


def machine_dir() -> Path:
    """A folder every user of this Mac can write (for the one-check-at-a-time lock)."""
    return Path("/Users/Shared/VideoQCBot")


def bootstrap_dirs(executable: Path, frozen: bool) -> list[Path]:
    """Where IT can put videoqcbot-bootstrap.yaml, first match wins: next to "Video QC Bot.app",
    inside the app (Contents/Resources), then /Library/Application Support/VideoQCBot
    (machine-wide, e.g. pushed by an MDM)."""
    dirs = []
    if frozen:
        exe = Path(executable)
        app = next((p for p in exe.parents if p.suffix == ".app"), None)
        dirs += [app.parent, app / "Contents" / "Resources"] if app is not None else [exe.parent]
    return dirs + [Path("/Library/Application Support/VideoQCBot")]
