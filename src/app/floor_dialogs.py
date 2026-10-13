"""Dialogs for working on the QC floor: the first-run setup (where the shared data folder is), editing
a shared file safely (ignore list, glossary), and the editor PIN.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from PySide6.QtWidgets import (QButtonGroup, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPlainTextEdit, QPushButton, QRadioButton, QVBoxLayout, QWidget)

from src.shared import (Floor, LockBusy, SharedUnavailable, looks_empty, save_user_setup, seed_shared,
                        write_shared_text)

log = logging.getLogger("qcbot.app")


class SetupDialog(QDialog):
    """Where the shared data folder is. Shown on the first run when IT hasn't set one with a
    bootstrap file, and from Settings > Change."""

    def __init__(self, current: Path | None = None, parent: QWidget | None = None, user_file: Path | None = None):
        super().__init__(parent)
        self.user_file = user_file
        self.chosen: Path | None = None
        self.setWindowTitle("Video QC Bot: set up")
        self.setMinimumWidth(560)
        self.shared = QRadioButton("Use the team's shared folder (clients, glossary, rules shared by every PC)")
        self.alone = QRadioButton("Work alone on this PC (rules kept on this PC only)")
        group = QButtonGroup(self)
        group.addButton(self.shared)
        group.addButton(self.alone)
        self.folder = QLineEdit(str(current) if current else "")
        self.folder.setPlaceholderText(r"e.g. \\qcserver\QC\VideoQCBot, or a synced OneDrive / SharePoint folder")
        browse = QPushButton("Choose...")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.folder, 1)
        row.addWidget(browse)
        self.shared.setChecked(True)
        self.folder.textEdited.connect(lambda _t: self.shared.setChecked(True))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Where should Video QC Bot keep the client rules? Ask your team lead or IT for the "
                                "shared folder. Checks still run when the folder can't be reached (from a copy "
                                "on this PC)."))
        layout.addWidget(self.shared)
        layout.addLayout(row)
        layout.addWidget(self.alone)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "The shared data folder", self.folder.text() or str(Path.home()))
        if folder:
            self.folder.setText(str(Path(folder)))
            self.shared.setChecked(True)

    def accept(self) -> None:
        if self.alone.isChecked():
            save_user_setup(None, alone=True, user_file=self.user_file)
            self.chosen = None
            super().accept()
            return
        text = self.folder.text().strip()
        if not text:
            QMessageBox.warning(self, "Video QC Bot", "Choose the shared folder, or pick \"Work alone on this PC\".")
            return
        folder = Path(text)
        from src.shared import _reachable

        if not _reachable(folder, 5.0):
            QMessageBox.warning(self, "Video QC Bot", f"{folder} can't be reached. Check the drive is connected (or "
                                                      f"the folder is synced) and try again.")
            return
        if looks_empty(folder):
            answer = QMessageBox.question(self, "Video QC Bot", f"{folder} has no Video QC Bot files yet. Set it up "
                                          "with the starting rules and clients now?")
            if answer != QMessageBox.StandardButton.Yes:
                return
            try:
                copied = seed_shared(folder)
                log.info("shared folder %s set up: %s", folder, ", ".join(copied))
            except OSError as exc:
                QMessageBox.warning(self, "Video QC Bot", f"Couldn't write to {folder}: {exc}")
                return
        save_user_setup(folder, user_file=self.user_file)
        self.chosen = folder
        super().accept()


class TextFileDialog(QDialog):
    """Edit one shared text file (ignore.yaml, glossary.txt) inside the app, so every save takes the
    shared lock, backs up the old file and goes in the changelog. Read-only when not an editor or offline."""

    def __init__(self, floor: Floor, name: str, title: str, editable: bool, why: str = "",
                 parent: QWidget | None = None, starter: str = ""):
        super().__init__(parent)
        self.floor, self.name = floor, name
        self.setWindowTitle(title)
        self.resize(820, 620)
        self.path, self.mtime, offline = None, None, False
        try:
            root = floor.write_root
        except SharedUnavailable as exc:
            root, offline, why = floor.data_root, True, str(exc)
        self.path = root / name
        try:
            text = self.path.read_text(encoding="utf-8")
            self.mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            text = starter
        self.original = text
        self.editor = QPlainTextEdit(text)
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.editable = editable and not offline
        self.editor.setReadOnly(not self.editable)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        if self.editable:
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
            buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"{self.path}" + ("" if self.editable else f"<br><b>View only:</b> {why}")))
        layout.addWidget(self.editor, 1)
        layout.addWidget(buttons)

    def changes(self, new: str) -> str:
        old_lines, new_lines = self.original.splitlines(), new.splitlines()
        added = len([line for line in new_lines if line not in old_lines])
        removed = len([line for line in old_lines if line not in new_lines])
        return f"+{added} / -{removed} line(s)"

    def save(self) -> None:
        text = self.editor.toPlainText()
        if not text.endswith("\n"):
            text += "\n"
        if self.name.endswith((".yaml", ".yml")):
            try:
                yaml.safe_load(text)
            except yaml.YAMLError as exc:
                QMessageBox.warning(self, "Video QC Bot", f"This isn't valid YAML, so it wasn't saved:\n\n{exc}")
                return
        if text == self.original:
            self.accept()
            return
        try:
            write_shared_text(self.floor, self.name, text, "edit", self.changes(text), expected_mtime=self.mtime)
        except (LockBusy, SharedUnavailable, OSError) as exc:
            QMessageBox.warning(self, "Video QC Bot", str(exc))
            return
        self.accept()
