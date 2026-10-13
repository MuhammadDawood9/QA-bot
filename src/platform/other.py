"""Any other OS (Linux): the same behavior as Windows, without promises. Not a supported target."""

from __future__ import annotations

import tempfile
from pathlib import Path

# The machine helpers are plain POSIX on macOS (vm_stat aside, which just returns None when missing).
from src.platform.macos import available_ram_bytes, lower_priority, pid_alive, total_ram_bytes  # noqa: F401
from src.platform.windows import (after_app_created, extend_path, ffmpeg_install_help,  # noqa: F401
                                  floating_window_flags,
                                  prepare_floating_window, tray_icon)

name = "other"


def launch_at_login_enabled() -> bool:
    return False


def set_launch_at_login(enabled: bool, command: list[str]) -> None:
    raise OSError("starting at login isn't supported on this system")


def machine_dir() -> Path:
    return Path(tempfile.gettempdir()) / "VideoQCBot"


def bootstrap_dirs(executable: Path, frozen: bool) -> list[Path]:
    return ([Path(executable).parent] if frozen else []) + [Path("/etc/videoqcbot")]
