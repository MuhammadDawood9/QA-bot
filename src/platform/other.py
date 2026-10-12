"""Any other OS (Linux): the same behavior as Windows, without promises. Not a supported target."""

from __future__ import annotations

from src.platform.windows import (after_app_created, extend_path, ffmpeg_install_help,  # noqa: F401
                                  floating_window_flags,
                                  prepare_floating_window, tray_icon)

name = "other"


def launch_at_login_enabled() -> bool:
    return False


def set_launch_at_login(enabled: bool, command: list[str]) -> None:
    raise OSError("starting at login isn't supported on this system")
