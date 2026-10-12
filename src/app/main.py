"""The desktop companion app: a floating mascot plus a tray (menu bar) icon.

    python -m src.app            # run it
    python -m src.app --demo     # also a tray submenu to preview the four mascot states

Step 2 of the app: the window, its states and the tray. Checking videos (drag and drop, the
right-click menu, the background worker) comes in Step 3.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from src.app.assets import assets_dir
from src.app.mascot import MascotWindow
from src.app.settings import APP_NAME, Settings
from src.platform import current as platform

log = logging.getLogger("qcbot.app")


class CompanionApp:
    def __init__(self, app: QApplication, settings: Settings, demo: bool = False):
        self.app = app
        self.settings = settings
        self.mascot = MascotWindow(settings)
        self.mascot.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.mascot.customContextMenuRequested.connect(self._show_mascot_menu)
        self.tray = self._make_tray(demo)
        app.aboutToQuit.connect(self.mascot.remember_position)

    # --- tray ----------------------------------------------------------------------------

    def _make_tray(self, demo: bool) -> QSystemTrayIcon | None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            log.warning("no system tray here; quit from the mascot's right-click menu")
            return None
        tray = QSystemTrayIcon(platform.tray_icon(assets_dir()), self.app)
        tray.setToolTip("Video QC Bot")
        menu = QMenu()
        show = QAction("Show mascot", menu)
        show.triggered.connect(self.show_mascot)
        menu.addAction(show)
        if demo:
            preview = menu.addMenu("Preview state")
            for label, state, count in (("Idle", "idle", None), ("Working", "working", None),
                                        ("Done (12 issues)", "done", 12), ("Done (150 issues)", "done", 150),
                                        ("Error", "error", None)):
                action = QAction(label, preview)
                action.triggered.connect(lambda _=False, s=state, c=count: self.mascot.set_state(s, c))
                preview.addAction(action)
        menu.addSeparator()
        quit_action = QAction("Quit Video QC Bot", menu)
        quit_action.triggered.connect(self.quit)
        menu.addAction(quit_action)
        tray.setContextMenu(menu)
        tray.activated.connect(self._tray_activated)
        self._tray_menu = menu                      # keep a reference: Qt doesn't own it
        tray.show()
        return tray

    def _tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:     # a left click on Windows
            self.show_mascot()

    def _show_mascot_menu(self, pos) -> None:
        """Until Step 3's full menu: Quit, so the app can always be closed even without a tray."""
        menu = QMenu(self.mascot)
        menu.addAction("Quit Video QC Bot", self.quit)
        menu.exec(self.mascot.mapToGlobal(pos))

    # --- actions -------------------------------------------------------------------------

    def show_mascot(self) -> None:
        self.mascot.show()
        self.mascot.raise_()

    def quit(self) -> None:
        self.mascot.remember_position()
        if self.tray is not None:
            self.tray.hide()
        self.app.quit()


def make_application(argv: Sequence[str]) -> QApplication:
    # Sharp on high-DPI and Retina screens at fractional scales (125%, 150%): no rounding.
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication(list(argv))
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName("Video QC Bot")
    app.setQuitOnLastWindowClosed(False)            # the tray keeps it running
    platform.after_app_created(app)
    return app


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    parser = argparse.ArgumentParser(prog="python -m src.app", description="Video QC Bot desktop companion.")
    parser.add_argument("--demo", action="store_true", help="add a tray submenu to preview the mascot states")
    args = parser.parse_args(argv[1:])
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    app = make_application(argv)
    companion = CompanionApp(app, Settings(), demo=args.demo)
    companion.show_mascot()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
