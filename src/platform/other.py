"""Any other OS (Linux): the same behavior as Windows, without promises. Not a supported target."""

from __future__ import annotations

from src.platform.windows import after_app_created, floating_window_flags, prepare_floating_window, tray_icon  # noqa: F401

name = "other"
