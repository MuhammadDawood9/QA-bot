"""The app's small dialogs: settings, and a readable error message with a way to the log."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget)

from src.app.settings import Settings, config_dir, log_dir


def open_path(path: Path) -> bool:
    """Open a file or folder with the OS's default app (Word, Explorer, Finder)."""
    return QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, default_reports_dir: Path, parent: QWidget | None = None):
        super().__init__(parent)
        self.settings = settings
        self.default_reports_dir = default_reports_dir
        self.setWindowTitle("Video QC Bot settings")
        self.setMinimumWidth(520)

        self.reports = QLineEdit(settings.get("reports_dir") or "")
        self.reports.setPlaceholderText(str(default_reports_dir))
        browse = QPushButton("Choose...")
        browse.clicked.connect(self._choose_reports)
        row = QHBoxLayout()
        row.addWidget(self.reports, 1)
        row.addWidget(browse)

        self.open_report = QCheckBox("Open the report when a check finishes")
        self.open_report.setChecked(bool(settings.get("open_report")))
        self.notifications = QCheckBox("Show a desktop notification when a check finishes or fails")
        self.notifications.setChecked(bool(settings.get("notifications")))
        self.guess = QCheckBox("Take the client from the file name when it contains a client code")
        self.guess.setChecked(bool(settings.get("guess_client")))

        form = QFormLayout()
        form.addRow("Reports folder:", row)
        links = QLabel(f'<a href="logs">Open the log folder</a> &nbsp;·&nbsp; <a href="config">Open the settings folder</a>')
        links.linkActivated.connect(lambda which: open_path(log_dir() if which == "logs" else config_dir()))

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(QLabel("Leave the reports folder empty to use the project's reports/ folder."))
        for widget in (self.open_report, self.notifications, self.guess, links):
            layout.addWidget(widget)
        layout.addWidget(buttons)

    def _choose_reports(self) -> None:
        start = self.reports.text() or str(self.default_reports_dir)
        folder = QFileDialog.getExistingDirectory(self, "Folder for the reports", start)
        if folder:
            self.reports.setText(str(Path(folder)))

    def accept(self) -> None:
        text = self.reports.text().strip()
        self.settings.set("reports_dir", str(Path(text)) if text else None, save=False)
        self.settings.set("open_report", self.open_report.isChecked(), save=False)
        self.settings.set("notifications", self.notifications.isChecked(), save=False)
        self.settings.set("guess_client", self.guess.isChecked(), save=False)
        self.settings.save()
        super().accept()


def show_error(parent: QWidget | None, message: str, log_file: Path) -> None:
    """The error in plain words, with a button to open the log file."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("Video QC Bot")
    box.setText("The check didn't finish.")
    box.setInformativeText(message)
    open_log = box.addButton("Open log file", QMessageBox.ButtonRole.ActionRole)
    box.addButton(QMessageBox.StandardButton.Close)
    box.exec()
    if box.clickedButton() is open_log:
        open_path(log_file)
