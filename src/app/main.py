"""The desktop companion app: a floating mascot plus a tray (menu bar) icon.

    python -m src.app            # run it
    python -m src.app --demo     # also a tray submenu to preview the four mascot states

Drop a video on the mascot (or click it to pick one) and it runs every check in the background,
then shows the issue count, notifies you, and opens the Word report. Right-click the mascot (or
the tray icon) for the client, which checks run, the reports, settings and Quit.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Sequence

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QGuiApplication
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu, QMessageBox, QSystemTrayIcon

from src.app.assets import assets_dir
from src.app.choices import CHECK_TOGGLES, choose_client, file_dialog_filter, qc_options
from src.app.dialogs import SettingsDialog, open_path, show_error
from src.app.mascot import MascotWindow
from src.app.settings import APP_NAME, Settings, log_dir
from src.app.worker import CheckRunner
from src.config import PROJECT_ROOT, is_frozen, load_config
from src.engine import known_clients
from src.platform import current as platform
from src.utils.system import MissingToolError, find_ffmpeg

log = logging.getLogger("qcbot.app")
LOG_FILE_NAME = "videoqcbot.log"


class CompanionApp:
    def __init__(self, app: QApplication, settings: Settings, demo: bool = False, config: dict | None = None):
        self.app = app
        self.settings = settings
        self.demo = demo
        self.config = config if config is not None else load_config()
        self.clients = known_clients(self.config)
        self.status = "Ready: drop a video on me"

        self.mascot = MascotWindow(settings)
        self.mascot.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.mascot.customContextMenuRequested.connect(
            lambda pos: self.build_menu(self.mascot).exec(self.mascot.mapToGlobal(pos)))
        self.mascot.clicked.connect(self.pick_video)
        self.mascot.files_dropped.connect(self.videos_dropped)

        self.runner = CheckRunner()
        self.runner.progress.connect(self._on_progress)
        self.runner.finished.connect(self._on_finished)
        self.runner.failed.connect(self._on_failed)
        self.runner.cancelled.connect(self._on_cancelled)

        self.tray = self._make_tray()
        app.aboutToQuit.connect(self.mascot.remember_position)

    # --- paths -----------------------------------------------------------------------------

    @property
    def default_reports_dir(self) -> Path:
        """The project's reports/ folder when run from source; Documents/Video QC Bot reports when packaged."""
        if is_frozen():
            import platformdirs

            return platformdirs.user_documents_path() / "Video QC Bot reports"
        return Path(self.config["_root"]) / self.config.get("report", {}).get("output_dir", "reports")

    @property
    def reports_dir(self) -> Path:
        chosen = self.settings.get("reports_dir")
        return Path(chosen) if chosen else self.default_reports_dir

    @property
    def log_file(self) -> Path:
        return log_dir() / LOG_FILE_NAME

    # --- menus -----------------------------------------------------------------------------

    def build_menu(self, parent=None) -> QMenu:
        """The right-click menu, built fresh each time so it shows the current state."""
        return self.fill_menu(QMenu(parent))

    def fill_menu(self, menu: QMenu) -> QMenu:
        """The same items for the mascot's right-click menu and the tray icon's menu."""
        status = menu.addAction(self.status)
        status.setEnabled(False)
        menu.addSeparator()

        menu.addAction("Check a video...", self.pick_video).setEnabled(not self.runner.busy)
        cancel = menu.addAction("Cancel check", self.cancel_check)
        cancel.setEnabled(self.runner.busy)

        clients = menu.addMenu("Client")
        guess = clients.addAction("Guess from the file name")
        guess.setCheckable(True)
        guess.setChecked(bool(self.settings.get("guess_client")))
        guess.toggled.connect(lambda on: self.settings.set("guess_client", on))
        clients.addSeparator()
        group = QActionGroup(clients)
        current = self.settings.get("last_client")
        for code in [None, *self.clients]:
            action = clients.addAction("Default rules (no client)" if code is None else code)
            action.setCheckable(True)
            action.setChecked(code == current)
            action.setActionGroup(group)
            action.triggered.connect(lambda _=False, c=code: self.settings.set("last_client", c))

        checks = menu.addMenu("Checks")
        disabled = set(self.settings.get("disabled_checks") or [])
        for key, label in CHECK_TOGGLES:
            action = checks.addAction(label)
            action.setCheckable(True)
            action.setChecked(key not in disabled)
            action.toggled.connect(lambda on, k=key: self.toggle_check(k, on))

        menu.addSeparator()
        last = self.settings.get("last_report")
        menu.addAction("Open last report", self.open_last_report).setEnabled(bool(last) and Path(last).is_file())
        menu.addAction("Open reports folder", self.open_reports_folder)
        menu.addAction("Settings...", self.open_settings)
        login = menu.addAction("Start at login")
        login.setCheckable(True)
        login.setChecked(platform.launch_at_login_enabled())
        login.toggled.connect(self.set_start_at_login)
        if self.demo:
            preview = menu.addMenu("Preview state")
            for label, state, count in (("Idle", "idle", None), ("Working", "working", None),
                                        ("Done (12 issues)", "done", 12), ("Done (150 issues)", "done", 150),
                                        ("Error", "error", None)):
                preview.addAction(label, lambda s=state, c=count: self.mascot.set_state(s, c))
        menu.addSeparator()
        menu.addAction("Quit Video QC Bot", self.quit)
        return menu

    def _make_tray(self) -> QSystemTrayIcon | None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            log.warning("no system tray here; use the mascot's right-click menu")
            return None
        tray = QSystemTrayIcon(platform.tray_icon(assets_dir()), self.app)
        tray.setToolTip("Video QC Bot")
        self._tray_menu = QMenu()
        self._tray_menu.aboutToShow.connect(self._refresh_tray_menu)
        tray.setContextMenu(self._tray_menu)
        tray.activated.connect(self._tray_activated)
        tray.show()
        return tray

    def _refresh_tray_menu(self) -> None:
        for child in self._tray_menu.findChildren(QMenu):       # submenus from the last time it opened
            child.deleteLater()
        self._tray_menu.clear()
        self._tray_menu.addAction("Show mascot", self.show_mascot)
        self._tray_menu.addSeparator()
        self.fill_menu(self._tray_menu)

    def _tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:     # a left click on Windows
            self.show_mascot()

    # --- starting a check ------------------------------------------------------------------

    def pick_video(self) -> None:
        if self.runner.busy:
            self.notify("Already checking", f"Still checking {self.runner.video.name}. Right-click to cancel.")
            return
        start = self.settings.get("last_folder") or str(Path.home())
        name, _ = QFileDialog.getOpenFileName(None, "Choose a video to check", start, file_dialog_filter())
        if name:
            self.start_check(Path(name))

    def videos_dropped(self, videos: list) -> None:
        if self.runner.busy:
            self.notify("Already checking", f"Still checking {self.runner.video.name}. Right-click to cancel.")
            return
        if len(videos) > 1:
            self.notify("One video at a time", f"Checking {videos[0].name}; the other {len(videos) - 1} were left out.")
        self.start_check(Path(videos[0]))

    def start_check(self, video: Path) -> None:
        client, why = choose_client(video, self.settings.get("last_client"), bool(self.settings.get("guess_client")),
                                    self.clients)
        self.settings.set("last_folder", str(video.parent))
        self.status = f"Checking {video.name} for {why}"
        log.info(self.status)
        self.mascot.set_state("working")
        self.mascot.set_progress("Starting", 0.0)
        self.runner.start(video, client, qc_options(self.settings.get("disabled_checks") or []), self.reports_dir)

    def cancel_check(self) -> None:
        if self.runner.busy:
            self.status = f"Cancelling {self.runner.video.name}..."
            self.runner.cancel()

    def toggle_check(self, key: str, on: bool) -> None:
        disabled = set(self.settings.get("disabled_checks") or [])
        disabled.discard(key) if on else disabled.add(key)
        self.settings.set("disabled_checks", sorted(disabled))

    # --- results ---------------------------------------------------------------------------

    def _on_progress(self, stage: str, fraction: float) -> None:
        video = self.runner.video.name if self.runner.video else "the video"
        self.status = f"{stage}: {video}, {fraction:.0%}"
        self.mascot.set_progress(stage, fraction)

    def _on_finished(self, count: int, report: str) -> None:
        video = self.runner.video.name if self.runner.video else "the video"
        self.mascot.set_state("done", count)
        self.settings.set("last_report", report)
        self.status = f"Done: {count} issue{'s' if count != 1 else ''} in {video}"
        self.notify("QC check done", f"{count} issue{'s' if count != 1 else ''} found in {video}.")
        if self.settings.get("open_report"):
            open_path(Path(report))

    def _on_failed(self, message: str) -> None:
        self.mascot.set_state("error")
        self.status = "The last check failed (see Open log file)"
        self.notify("QC check failed", message, error=True)
        show_error(None, message, self.log_file)

    def _on_cancelled(self) -> None:
        self.mascot.set_state("idle")
        self.status = "Check cancelled. Drop a video on me"
        self.notify("QC check cancelled", "Nothing was written.")

    def notify(self, title: str, message: str, error: bool = False) -> None:
        log.info("%s: %s", title, message)
        if self.tray is not None and self.settings.get("notifications"):
            icon = QSystemTrayIcon.MessageIcon.Warning if error else QSystemTrayIcon.MessageIcon.Information
            self.tray.showMessage(title, message, icon, 8000)

    # --- other actions ---------------------------------------------------------------------

    def open_last_report(self) -> None:
        last = self.settings.get("last_report")
        if last and Path(last).is_file():
            open_path(Path(last))

    def open_reports_folder(self) -> None:
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        open_path(self.reports_dir)

    def open_settings(self) -> None:
        SettingsDialog(self.settings, self.default_reports_dir).exec()

    def set_start_at_login(self, enabled: bool) -> None:
        try:
            platform.set_launch_at_login(enabled, launch_command())
            log.info("start at login %s: %s", "on" if enabled else "off", launch_command())
        except OSError as exc:
            log.error("couldn't change start at login: %s", exc)
            QMessageBox.warning(None, "Video QC Bot", f"Couldn't change \"Start at login\":\n{exc}")

    def refresh_start_at_login(self) -> None:
        """If start at login is on, point it at this copy of the app (it may have been moved or updated)."""
        try:
            if platform.launch_at_login_enabled():
                platform.set_launch_at_login(True, launch_command())
        except OSError as exc:
            log.warning("couldn't refresh the start-at-login entry: %s", exc)

    def check_tools(self) -> bool:
        """ffmpeg is needed for audio and screenshots: say how to install it if it's missing."""
        try:
            find_ffmpeg(self.config.get("machine", {}).get("ffmpeg_path"))
            return True
        except MissingToolError:
            log.warning("ffmpeg not found")
            QMessageBox.warning(None, "Video QC Bot: ffmpeg is missing", platform.ffmpeg_install_help())
            return False

    def show_mascot(self) -> None:
        self.mascot.show()
        self.mascot.raise_()

    def quit(self) -> None:
        if self.runner.busy:
            self.runner.cancel()
            self.runner.wait(15_000)
        self.mascot.remember_position()
        if self.tray is not None:
            self.tray.hide()
        self.app.quit()


def launch_command() -> list[str]:
    """How to start this app: the packaged program itself, or Python running run_app.pyw (no
    console window on Windows: pythonw.exe)."""
    if is_frozen():
        return [sys.executable]
    python = Path(sys.executable)
    windowless = python.with_name("pythonw.exe")
    return [str(windowless if windowless.is_file() else python), str(PROJECT_ROOT / "run_app.pyw")]


def selftest() -> int:
    """Load every heavy library and data file the engine needs, then exit: checks a packaged build
    without clicking through it. Prints one line per item and returns 0 when all are there."""
    import importlib

    from src.app.assets import STATES, assets_dir
    from src.config import resolve_path

    def report(line: str) -> None:
        print(line)                 # nothing shows in a windowed build, so it also goes to the log
        log.info("selftest: %s", line)

    problems = 0
    for module in ("faster_whisper", "ctranslate2", "onnxruntime", "rapidocr_onnxruntime", "cv2", "av", "docx",
                   "spellchecker", "rapidfuzz", "openpyxl", "anthropic", "src.engine"):
        try:
            importlib.import_module(module)
            report(f"ok      import {module}")
        except Exception as exc:                          # report every failure, not just the first
            problems += 1
            report(f"FAILED  import {module}: {type(exc).__name__}: {exc}")
    config = load_config()
    checks = [("config", Path(config["_root"]) / "config.yaml"), ("rules", resolve_path(config, "rules_file")),
              ("clients", resolve_path(config, "clients_dir")), ("glossary", resolve_path(config, "glossary_file"))]
    checks += [(f"mascot {s}", assets_dir() / "mascot" / f"{s}.png") for s in STATES]
    for name, path in checks:
        found = path.exists()
        problems += not found
        report(f"{'ok' if found else 'MISSING':<7} {name}: {path}")
    from spellchecker import SpellChecker
    known = "hello" in SpellChecker()
    problems += not known
    report(f"{'ok' if known else 'FAILED':<7} spell checker word list")
    from rapidocr_onnxruntime import RapidOCR
    RapidOCR()
    report("ok      OCR models load")
    report("all good" if problems == 0 else f"{problems} problem(s)")
    return 0 if problems == 0 else 1


def check_without_window(video: Path, client: str | None) -> int:
    """The whole pipeline from the packaged app, no window: proves a build can really check videos.
    Progress and the result go to the log (and the console, when there is one)."""
    from src.app.choices import qc_options
    from src.engine import QCError, build_docx_report, run_qc

    last = {"stage": None}

    def progress(stage: str, fraction: float) -> None:
        if stage != last["stage"]:
            last["stage"] = stage
            log.info("check: %s", stage)

    try:
        result = run_qc(video, client, progress, options=qc_options(["punctuation"]))
        report = build_docx_report(result)
    except QCError as exc:
        log.error("check failed: %s", exc)
        return 2
    log.info("check: %d issues, report %s", len(result), report)
    for issue in result:
        log.info("check: %s %s %s", round(issue.timestamp, 2), issue.type, issue.message[:90])
    return 0


def setup_logging() -> Path:
    """Everything (engine messages, warnings, errors with their tracebacks) goes to a log file in
    the OS log folder; it rolls over at 1 MB and keeps 3 old files."""
    folder = log_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / LOG_FILE_NAME
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    if sys.stderr is not None:                              # None under pythonw (no console)
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        root.addHandler(console)
    logging.captureWarnings(True)                           # the engine's warnings.warn(...) go to the log too
    return path


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
    parser.add_argument("--demo", action="store_true", help="add a menu to preview the mascot states")
    parser.add_argument("--selftest", action="store_true", help="check that every library and data file loads, then exit")
    parser.add_argument("--check", type=Path, metavar="VIDEO",
                        help="check one video without the window, write the Word report, then exit")
    parser.add_argument("--client", help="client code for --check")
    args = parser.parse_args(argv[1:])
    log_file = setup_logging()
    added = platform.extend_path()
    if added:
        log.info("added to PATH for finding ffmpeg: %s", ", ".join(added))
    if args.selftest:
        return selftest()
    if args.check:
        return check_without_window(args.check, args.client)
    log.info("starting; log file %s", log_file)

    app = make_application(argv)
    companion = CompanionApp(app, Settings(), demo=args.demo)
    companion.show_mascot()
    companion.refresh_start_at_login()
    QTimer.singleShot(500, companion.check_tools)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
