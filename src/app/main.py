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
import threading
import time
from pathlib import Path
from typing import Sequence

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QGuiApplication
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu, QMessageBox, QSystemTrayIcon

from src.app.assets import assets_dir, available_skins, load_skin
from src.app.choices import choose_client, file_dialog_filter, qc_options, visible_toggles
from src.app.dialogs import SettingsDialog, open_path, show_error
from src.app.mascot import MascotWindow
from src.app.settings import APP_NAME, Settings, log_dir
from src.app.worker import CheckRunner
from src.config import PROJECT_ROOT, is_frozen, load_config
from src.engine import known_clients
from src.platform import current as platform
from src.shared import current_floor, role_for, set_floor
from src.utils.safe_io import current_user
from src.utils.system import MissingToolError, find_ffmpeg
from src.version import __version__

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
        self.mascot.cue_sheets_dropped.connect(self.cue_sheets_dropped)
        self.mascot.files_dropped.connect(self.videos_dropped)
        self.queue: list[Path] = []                 # videos waiting for the current check to end

        self.runner = CheckRunner()
        self.runner.progress.connect(self._on_progress)
        self.runner.finished.connect(self._on_finished)
        self.runner.failed.connect(self._on_failed)
        self.runner.cancelled.connect(self._on_cancelled)
        self.runner.idle.connect(self._start_next)
        self.runner.warning.connect(lambda message: self.notify("Video QC Bot", message, error=True))

        # The shared folder: refreshed in the background every minute (and before each check, in the
        # worker), so edits from other PCs apply without a restart. Watched for going offline.
        self.floor = current_floor()
        self.editor_unlocked = False             # the editor PIN was entered this session
        self._seen = (self.floor.online, self.floor.last_sync)
        self._syncing = False
        self.sync_timer = QTimer()
        self.sync_timer.timeout.connect(self.sync_in_background)
        self.watch_timer = QTimer()
        self.watch_timer.timeout.connect(self._watch_floor)
        if self.floor.shared:
            self.sync_timer.start(60_000)
            self.watch_timer.start(2_000)
            self.sync_in_background()

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
        if self.floor.shared and (self.floor.last_sync is None or time.time() - self.floor.last_sync > 30):
            self.sync_in_background()
        version = menu.addAction(f"Video QC Bot {__version__}")
        version.setEnabled(False)
        status = menu.addAction(self.status)
        status.setEnabled(False)
        if self.floor.shared and self.floor.online is False:
            offline = menu.addAction(self.floor.status_text())
            offline.setEnabled(False)
        menu.addSeparator()

        menu.addAction("Check a video...", self.pick_video)        # while busy: added to the queue
        menu.addAction("Attach a cue sheet to an episode...", self.attach_cue_sheet_dialog)
        cancel = menu.addAction("Cancel check", self.cancel_check)
        cancel.setEnabled(self.runner.busy)
        clear = menu.addAction(f"Clear queue ({len(self.queue)} waiting)", self.clear_queue)
        clear.setEnabled(bool(self.queue))

        clients = menu.addMenu("Client")
        guess = clients.addAction("Guess from the file name")
        guess.setCheckable(True)
        guess.setChecked(bool(self.settings.get("guess_client")))
        guess.toggled.connect(lambda on: self.settings.set("guess_client", on))
        clients.addSeparator()
        group = QActionGroup(clients)
        current = self.settings.get("last_client")
        names = self.client_names()
        for code in [None, *self.clients]:
            name = names.get(code)
            label = "Default rules (no client)" if code is None else (f"{code}  ({name})" if name and name != code
                                                                    else code)
            action = clients.addAction(label)
            action.setCheckable(True)
            action.setChecked(code == current)
            action.setActionGroup(group)
            action.triggered.connect(lambda _=False, c=code: self.settings.set("last_client", c))
        clients.addSeparator()
        role = self.role
        for label, slot, enabled in (("Add new client...", self.add_client, role.editor),
                                     ("Edit client...", self.edit_client, role.editor and bool(self.clients))):
            action = clients.addAction(label, slot)
            action.setEnabled(enabled)
            if not role.editor:
                action.setToolTip(f"Editors only ({role.why})")
        clients.addAction("Get client template", self.save_client_template)
        clients.setToolTipsVisible(True)

        skins = menu.addMenu("Skin")
        skin_group = QActionGroup(skins)
        for key in available_skins():
            action = skins.addAction(load_skin(key).name)
            action.setCheckable(True)
            action.setChecked(key == self.mascot.skin.key)
            action.setActionGroup(skin_group)
            action.triggered.connect(lambda _=False, k=key: self.mascot.set_skin(k))
        skins.addSeparator()
        idle_blink = skins.addAction("Blink while idle")
        idle_blink.setCheckable(True)
        idle_blink.setChecked(self.mascot.idle_blink)
        idle_blink.toggled.connect(self.mascot.set_idle_blink)

        checks = menu.addMenu("Checks")
        disabled = set(self.settings.get("disabled_checks") or [])
        for key, label in visible_toggles(self.config):
            action = checks.addAction(label)
            action.setCheckable(True)
            action.setChecked(key not in disabled)
            action.toggled.connect(lambda on, k=key: self.toggle_check(k, on))

        menu.addSeparator()
        last = self.settings.get("last_report")
        menu.addAction("Open last report", self.open_last_report).setEnabled(bool(last) and Path(last).is_file())
        menu.addAction("Open reports folder", self.open_reports_folder)
        word = "Edit" if self.role.editor else "View"
        menu.addAction(f"{word} ignore list", self.open_ignore_list)
        menu.addAction(f"{word} glossary", self.open_glossary)
        menu.addAction("Settings...", self.open_settings)
        if self.floor.shared:
            if self.editor_unlocked:
                menu.addAction("Leave editor mode", self.leave_editor_mode)
            elif not self.role.editor:
                menu.addAction("Editor mode...", self.enter_editor_mode)
        menu.addAction("Check for update", self.check_for_update)
        menu.addAction("Copy diagnostics", self.copy_diagnostics)
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
        start = self.settings.get("last_folder") or str(Path.home())
        names, _ = QFileDialog.getOpenFileNames(None, "Choose videos or audio files to check", start,
                                                file_dialog_filter())
        if names:
            self.enqueue([Path(n) for n in names])

    def videos_dropped(self, videos: list) -> None:
        self.enqueue([Path(v) for v in videos])

    def enqueue(self, videos: list[Path]) -> None:
        """Check these videos one after the other: now if nothing is running, else after the
        current check and the ones already waiting. A video already running or waiting is skipped."""
        current = self.runner.video if self.runner.busy else None
        new = [v for v in dict.fromkeys(videos) if v != current and v not in self.queue]
        if not new:
            return
        self.queue += new
        if self.runner.busy:
            self.notify("Added to the queue", f"{self._count(new)} waiting to be checked after "
                                              f"{self.runner.video.name} ({len(self.queue)} in the queue).")
            return
        self._start_next()
        if self.queue:
            self.notify("Checking a batch", f"Checking {self.runner.video.name}; {self._count(self.queue)} waiting.")

    @staticmethod
    def _count(videos: list) -> str:
        return f"{len(videos)} video{'s' if len(videos) != 1 else ''}"

    def _start_next(self) -> None:
        if self.queue and not self.runner.busy:
            self.start_check(self.queue.pop(0))

    def clear_queue(self) -> None:
        if self.queue:
            log.info("queue cleared: %s", ", ".join(v.name for v in self.queue))
            self.queue.clear()
            self.status = f"Checking {self.runner.video.name}; queue cleared" if self.runner.busy else \
                "Queue cleared. Drop a video on me"

    def start_check(self, video: Path) -> None:
        client, why = choose_client(video, self.settings.get("last_client"), bool(self.settings.get("guess_client")),
                                    self.clients)
        self.settings.set("last_folder", str(video.parent))
        self.status = f"Checking {video.name} for {why}"
        log.info(self.status)
        self.mascot.set_state("working")
        self.mascot.set_progress("Starting", 0.0)
        from dataclasses import replace

        from src.audio.regions import find_cue_sheet

        options = qc_options(self.settings.get("disabled_checks") or [])
        cue = find_cue_sheet(video, self.cue_sheet_dir)
        if cue is not None:
            options = replace(options, cue_sheet=cue)
            log.info("cue sheet for %s: %s", video.name, cue)
        self.runner.start(video, client, options, self.reports_dir, shared_reports=self.shared_reports_dir())

    def cancel_check(self) -> None:
        if self.runner.busy:
            self.status = f"Cancelling {self.runner.video.name}..."
            self.runner.cancel()

    def toggle_check(self, key: str, on: bool) -> None:
        disabled = set(self.settings.get("disabled_checks") or [])
        disabled.discard(key) if on else disabled.add(key)
        self.settings.set("disabled_checks", sorted(disabled))

    # --- results ---------------------------------------------------------------------------

    def _waiting(self) -> str:
        return f" ({len(self.queue)} more waiting)" if self.queue else ""

    def _on_progress(self, stage: str, fraction: float) -> None:
        video = self.runner.video.name if self.runner.video else "the video"
        self.status = f"{stage}: {video}, {fraction:.0%}{self._waiting()}"
        self.mascot.set_progress(stage, fraction)

    def _on_finished(self, count: int, report: str) -> None:
        video = self.runner.video.name if self.runner.video else "the video"
        self.mascot.set_state("done", count)
        self.settings.set("last_report", report)
        self.status = f"Done: {count} issue{'s' if count != 1 else ''} in {video}{self._waiting()}"
        self.notify("QC check done", f"{count} issue{'s' if count != 1 else ''} found in {video}."
                                     + (f" Next: {self.queue[0].name}{self._waiting()}." if self.queue else ""))
        if self.settings.get("open_report") and not self.queue:     # in a batch, only the last one opens
            open_path(Path(report))

    def _on_failed(self, message: str) -> None:
        video = self.runner.video.name if self.runner.video else "the video"
        self.mascot.set_state("error")
        self.status = f"The check of {video} failed (see Open log file){self._waiting()}"
        self.notify("QC check failed", f"{video}: {message}", error=True)
        if not self.queue:                  # in a batch, the notification is enough: the rest carries on
            show_error(None, message, self.log_file)

    def _on_cancelled(self) -> None:
        self.mascot.set_state("idle")
        self.status = ("Check cancelled; going on with the queue" if self.queue
                       else "Check cancelled. Drop a video on me")
        self.notify("QC check cancelled", "Nothing was written." + (f" Next: {self.queue[0].name}." if self.queue else ""))

    def notify(self, title: str, message: str, error: bool = False) -> None:
        log.info("%s: %s", title, message)
        if self.tray is not None and self.settings.get("notifications"):
            icon = QSystemTrayIcon.MessageIcon.Warning if error else QSystemTrayIcon.MessageIcon.Information
            self.tray.showMessage(title, message, icon, 8000)

    # --- cue sheets (podcasts) ----------------------------------------------------------------

    @property
    def cue_sheet_dir(self) -> Path:
        """Where a cue sheet goes when the episode's own folder can't be written."""
        return Path(self.config.get("_local_root") or self.config["_root"]) / "cue_sheets"

    def cue_sheets_dropped(self, media: list, csvs: list) -> None:
        """An episode dropped together with its cue sheet (start,end,type,note): kept next to the
        episode as <episode>.cues.csv, so later checks of it use it too."""
        from src.audio.regions import attach_cue_sheet, match_cue_sheets

        for episode, csv_file in match_cue_sheets([Path(m) for m in media], [Path(c) for c in csvs]).items():
            try:
                where, problems = attach_cue_sheet(episode, csv_file, self.cue_sheet_dir)
            except (OSError, ValueError) as exc:
                self.notify("Cue sheet not used", f"{Path(csv_file).name}: {exc}", error=True)
                continue
            note = f" ({len(problems)} line(s) skipped)" if problems else ""
            self.notify("Cue sheet attached", f"{Path(episode).name} uses {where.name}{note}.")

    def attach_cue_sheet_dialog(self) -> None:
        from src.audio.regions import attach_cue_sheet

        start = self.settings.get("last_folder") or str(Path.home())
        episode, _ = QFileDialog.getOpenFileName(None, "The episode", start, file_dialog_filter())
        if not episode:
            return
        csv_file, _ = QFileDialog.getOpenFileName(None, "Its cue sheet (start,end,type,note)", str(Path(episode).parent),
                                                  "Cue sheet (*.csv)")
        if not csv_file:
            return
        try:
            where, problems = attach_cue_sheet(Path(episode), Path(csv_file), self.cue_sheet_dir)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(None, "Video QC Bot", f"The cue sheet wasn't attached: {exc}")
            return
        QMessageBox.information(None, "Video QC Bot", f"{Path(episode).name} will use {where}."
                                + (f"\n\n{len(problems)} line(s) skipped:\n" + "\n".join(problems[:10]) if problems else ""))

    # --- clients ---------------------------------------------------------------------------

    def client_store(self):
        """Clients are read from and saved to the shared folder itself (not the cached copy), under
        the shared lock. Raises SharedUnavailable when it can't be reached."""
        from src.clients.store import ClientStore

        return ClientStore(self.config, lock=self.floor.lock, root=self.floor.write_root)

    def client_names(self) -> dict:
        """From the cached copy: building a menu never waits for the network."""
        from src.clients.store import ClientStore

        try:
            return ClientStore(self.config).names()
        except OSError as exc:
            log.warning("couldn't read the client names: %s", exc)
            return {}

    def refresh_clients(self) -> None:
        self.config = load_config()
        self.clients = known_clients(self.config)

    def _editor_or_say_why(self) -> bool:
        role = self.role
        if not role.editor:
            QMessageBox.information(None, "Video QC Bot", f"Only editors can change clients ({role.why}).")
        return role.editor

    def _store_or_say_why(self):
        from src.shared import SharedUnavailable

        try:
            return self.client_store()
        except SharedUnavailable as exc:
            QMessageBox.warning(None, "Video QC Bot", str(exc))
            return None

    def add_client(self) -> None:
        from src.app.client_dialogs import ClientDialog

        store = self._store_or_say_why() if self._editor_or_say_why() else None
        if store is None:
            return
        dialog = ClientDialog(store, self.config, None, last_folder=self.settings.get("last_folder"))
        if dialog.exec() and dialog.saved_code:
            self.floor.sync()
            self.refresh_clients()
            self.settings.set("last_client", dialog.saved_code)
            self.notify("Client added", f"{dialog.saved_code} is saved and selected.")

    def edit_client(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        from src.app.client_dialogs import ClientDialog

        if not self._editor_or_say_why():
            return
        self.refresh_clients()
        if not self.clients:
            return
        current = self.settings.get("last_client")
        names = self.client_names()
        labels = [f"{c}  ({names[c]})" if names.get(c, c) != c else c for c in self.clients]
        start = self.clients.index(current) if current in self.clients else 0
        label, ok = QInputDialog.getItem(None, "Edit client", "Client:", labels, start, False)
        if not ok:
            return
        code = self.clients[labels.index(label)]
        store = self._store_or_say_why()
        if store is None:
            return
        dialog = ClientDialog(store, self.config, code, last_folder=self.settings.get("last_folder"))
        if dialog.exec() and dialog.saved_code:
            self.floor.sync()
            self.refresh_clients()
            self.notify("Client saved", f"{code}: changes saved (previous version backed up).")

    def save_client_template(self) -> None:
        import platformdirs

        from src.clients.importers import TEMPLATE_NAME, make_template

        start = str(platformdirs.user_documents_path() / TEMPLATE_NAME)
        name, _ = QFileDialog.getSaveFileName(None, "Save the client template", start, "Excel workbook (*.xlsx)")
        if not name:
            return
        try:
            path = make_template(Path(name))
        except OSError as exc:
            QMessageBox.warning(None, "Video QC Bot", f"Couldn't save the template: {exc}")
            return
        open_path(path.parent)

    # --- other actions ---------------------------------------------------------------------

    def open_last_report(self) -> None:
        last = self.settings.get("last_report")
        if last and Path(last).is_file():
            open_path(Path(last))

    def open_reports_folder(self) -> None:
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        open_path(self.reports_dir)

    def _edit_shared_file(self, key: str, title: str, starter: str = "") -> None:
        """A shared text file in the app's own editor: saved under the shared lock, with a backup
        and a changelog line. View only for non-editors and when the shared folder is offline."""
        from src.app.floor_dialogs import TextFileDialog

        name = (self.config.get("paths") or {}).get(key) or key
        role = self.role
        if TextFileDialog(self.floor, name, title, role.editor, role.why, starter=starter).exec():
            self.refresh_clients()

    def open_glossary(self) -> None:
        """glossary.txt: names and brands the spelling check accepts."""
        self._edit_shared_file("glossary_file", "Glossary")

    def open_ignore_list(self) -> None:
        """ignore.yaml: flags you decided are fine."""
        self._edit_shared_file("ignore_file", "Ignore list",
                               starter="# Flags you decided are fine; copy lines from a report's \"Hide a flag next "
                                       "time\".\nignore:\n")

    def open_settings(self) -> None:
        from src.config import save_local_settings
        from src.machine import choose_whisper_model, cpu_count, ram_gb

        local_model = ((self.config.get("_machine") or {}).get("whisper_model_size") == "auto" and "auto") or \
            self.config.get("machine", {}).get("whisper_model_size", "auto")
        shared_reports = self.floor.setup.shared_reports if self.floor.shared else None
        dialog = SettingsDialog(
            self.settings, self.default_reports_dir, floor_text=self.floor.status_text(),
            on_change_floor=self.change_floor, whisper_model=local_model,
            auto_model=choose_whisper_model(cpu_count(), ram_gb()), shared_reports=shared_reports,
            shared_copy_default=self.shared_copy_default(),
            on_set_pin=self.set_editor_pin if self.floor.shared and self.role.editor else None)
        if dialog.exec() and dialog.whisper_choice is not None and dialog.whisper_choice != local_model:
            local = self.config.get("_local_root") or self.config["_root"]
            save_local_settings(local, {"machine": {"whisper_model_size": dialog.whisper_choice}})
            self.refresh_clients()

    # --- the floor ---------------------------------------------------------------------------

    @property
    def role(self):
        return role_for(self.floor, current_user(), self.editor_unlocked)

    def sync_in_background(self) -> None:
        if not self.floor.shared or self._syncing:
            return
        self._syncing = True

        def run():
            try:
                self.floor.sync()
            finally:
                self._syncing = False

        threading.Thread(target=run, daemon=True, name="shared-sync").start()

    def _watch_floor(self) -> None:
        """On the main thread: react to what the background sync found."""
        seen = (self.floor.online, self.floor.last_sync)
        if seen == self._seen:
            return
        was_online = self._seen[0]
        self._seen = seen
        if self.floor.online is False and was_online is not False:
            self.notify("Offline, using cached rules", f"The shared folder can't be reached. Checks use the rules "
                                                       f"copied {self.floor.cache_time() or 'earlier'}.", error=True)
        elif self.floor.online and was_online is False:
            self.notify("Back online", "The shared rules are up to date again.")
        if self.floor.online:
            self.refresh_clients()
        self._update_tray_tip()

    def _update_tray_tip(self) -> None:
        if self.tray is not None:
            offline = self.floor.shared and self.floor.online is False
            self.tray.setToolTip("Video QC Bot (offline, using cached rules)" if offline else "Video QC Bot")

    def shared_copy_default(self) -> bool:
        return bool(((self.config.get("_shared_settings") or {}).get("reports") or {}).get("copy_to_shared"))

    def shared_reports_dir(self) -> Path | None:
        if not self.floor.shared or self.floor.setup.shared_reports is None:
            return None
        chosen = self.settings.get("copy_reports_to_shared")
        return self.floor.setup.shared_reports if (self.shared_copy_default() if chosen is None else chosen) else None

    def change_floor(self, parent=None) -> None:
        from src.app.floor_dialogs import SetupDialog

        if SetupDialog(self.floor.shared_dir, parent).exec():
            set_floor(None)
            self.floor = current_floor()
            self.floor.sync()
            self.editor_unlocked = False
            for timer in (self.sync_timer, self.watch_timer):
                timer.stop()
            if self.floor.shared:
                self.sync_timer.start(60_000)
                self.watch_timer.start(2_000)
            self.refresh_clients()
            self._update_tray_tip()

    def enter_editor_mode(self) -> None:
        from PySide6.QtWidgets import QInputDialog, QLineEdit

        from src.shared import shared_settings, verify_pin

        stored = shared_settings(self.floor.data_root).get("editor_pin")
        if not stored:
            QMessageBox.information(None, "Video QC Bot", "No editor PIN is set up. Ask an editor to add your login "
                                    f"name ({current_user()}) to the editors list, or to set a PIN.")
            return
        pin, ok = QInputDialog.getText(None, "Editor mode", "Editor PIN:", QLineEdit.EchoMode.Password)
        if not ok:
            return
        if verify_pin(pin, stored):
            self.editor_unlocked = True
            self.notify("Editor mode", "You can add and edit clients until you quit or leave editor mode.")
        else:
            QMessageBox.warning(None, "Video QC Bot", "That PIN isn't right.")

    def leave_editor_mode(self) -> None:
        self.editor_unlocked = False

    def set_editor_pin(self, parent=None) -> None:
        from PySide6.QtWidgets import QInputDialog, QLineEdit

        from src.shared import LockBusy, SharedUnavailable, set_editor_pin

        pin, ok = QInputDialog.getText(parent, "Editor PIN", "New editor PIN (at least 4 characters). Everyone who "
                                       "knows it can edit clients:", QLineEdit.EchoMode.Password)
        if not ok:
            return
        again, ok = QInputDialog.getText(parent, "Editor PIN", "The same PIN again:", QLineEdit.EchoMode.Password)
        if not ok:
            return
        if pin != again:
            QMessageBox.warning(parent, "Video QC Bot", "The two PINs aren't the same; nothing was changed.")
            return
        try:
            set_editor_pin(self.floor, pin)
        except (ValueError, LockBusy, SharedUnavailable, OSError) as exc:
            QMessageBox.warning(parent, "Video QC Bot", str(exc))
            return
        QMessageBox.information(parent, "Video QC Bot", "Editor PIN saved (stored hashed in the shared settings).")

    def check_for_update(self, quiet: bool = False) -> None:
        """Compare with latest.json in the shared folder; say where the new installer is. Never installs."""
        from src.version import check_for_update

        latest = Path(self.config["_root"]) / "latest.json"
        info = check_for_update(latest)
        if info is None:
            if not quiet:
                where = "" if latest.is_file() else " (no latest.json in the shared folder)"
                QMessageBox.information(None, "Video QC Bot", f"Video QC Bot {__version__} is the newest version{where}.")
            return
        if quiet:
            if self.settings.get("last_update_seen") == info.version:
                return
            self.settings.set("last_update_seen", info.version)
            self.notify("Update available", f"Video QC Bot {info.version} is available. Right-click > Check for update.")
            return
        box = QMessageBox()
        box.setWindowTitle("Video QC Bot")
        box.setText(f"Video QC Bot {info.version} is available (you have {__version__}).")
        box.setInformativeText((info.notes + "\n\n" if info.notes else "") +
                               (f"Installer: {info.installer}\n\nClose Video QC Bot, then run the new installer "
                                f"(or ask IT)." if info.installer else "Ask IT for the new installer."))
        show = box.addButton("Show the installer", QMessageBox.ButtonRole.ActionRole) if info.installer else None
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if show is not None and box.clickedButton() is show:
            target = Path(info.installer)
            open_path(target.parent if target.suffix else target)

    def copy_diagnostics(self) -> None:
        from src.diagnostics import collect

        role = self.role
        text = collect(self.config, self.settings.data, self.log_file, f"{role.editor} ({role.why})")
        QApplication.clipboard().setText(text)
        try:
            (self.log_file.parent / "diagnostics.txt").write_text(text, encoding="utf-8")
        except OSError:
            pass
        self.notify("Diagnostics copied", "Paste them into your message to support. Also saved as diagnostics.txt "
                                          "in the log folder.")

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
        self._update_tray_tip()
        self.mascot.show()
        self.mascot.raise_()

    def quit(self) -> None:
        self.queue.clear()
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
                   "spellchecker", "rapidfuzz", "openpyxl", "anthropic", "pypdf", "imageio_ffmpeg", "src.engine"):
        try:
            importlib.import_module(module)
            report(f"ok      import {module}")
        except Exception as exc:                          # report every failure, not just the first
            problems += 1
            report(f"FAILED  import {module}: {type(exc).__name__}: {exc}")
    config = load_config()
    checks = [("config", Path(config["_root"]) / "config.yaml"), ("rules", resolve_path(config, "rules_file")),
              ("clients", resolve_path(config, "clients_dir")), ("glossary", resolve_path(config, "glossary_file"))]
    skins = available_skins()
    checks += [("mascot skins", assets_dir() / "mascot" / (skins[0] if skins else "(none)"))]
    checks += [(f"skin {k} {s}", assets_dir() / "mascot" / k / f"{s}.png") for k in skins for s in STATES]
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
    from src.utils.system import bundled_ffmpeg
    ff = bundled_ffmpeg()
    problems += ff is None and is_frozen()
    report(f"{'ok' if ff else 'MISSING':<7} bundled ffmpeg: {ff}")
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

    platform.lower_priority()
    current_floor().sync()
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
    log.info("starting Video QC Bot %s; log file %s", __version__, log_file)
    if platform.lower_priority():
        log.info("running below normal CPU priority, so editing on this PC stays smooth")

    app = make_application(argv)
    if current_floor().setup.needs_setup:
        from src.app.floor_dialogs import SetupDialog

        SetupDialog().exec()                 # cancelled: asked again next time; works alone meanwhile
        set_floor(None)
    floor = current_floor()
    log.info("rules: %s", floor.setup.source)
    floor.sync()
    companion = CompanionApp(app, Settings(), demo=args.demo)
    companion.show_mascot()
    companion.refresh_start_at_login()
    QTimer.singleShot(500, companion.check_tools)
    QTimer.singleShot(3000, lambda: companion.check_for_update(quiet=True))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
