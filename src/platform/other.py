"""Any other OS (Linux): the same behavior as Windows, without promises. Not a supported target."""

from __future__ import annotations

from src.platform.windows import (after_app_created, ffmpeg_install_help, floating_window_flags,  # noqa: F401
                                  prepare_floating_window, tray_icon)

name = "other"
