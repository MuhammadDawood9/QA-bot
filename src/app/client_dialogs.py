"""Add / edit a client from the mascot's menu: the client dialog, the review of everything found in
imported files, and the caption area picker.

    ClientDialog(store, config, code=None)     # None = add a new client; a code = edit that one
        -> files dropped or picked (.xlsx template or QC workbook, .csv, .pdf) are read locally
        -> ReviewDialog shows every finding, editable, with a tick box; nothing is saved before "Save client"
        -> ClientStore.save: validation, duplicate check, backup, changelog
    CaptionAreaDialog(video)                   # drag a rectangle on a frame -> ocr.region (fractions)

The logic (what's found, what's valid, how it's saved) is in src/clients/, without Qt, and tested there.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMessageBox,
                               QPlainTextEdit, QPushButton, QSlider, QSpinBox, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from src.app import theme
from src.clients.importers import IMPORT_EXTENSIONS, claude_allowed, import_file, pdf_text, read_with_claude
from src.clients.model import (GUIDE_SENSITIVITIES, KINDS, LANGUAGES, MUSIC_PLACES, SFX_PLACES, ClientError,
                               Finding, area_text, build_client, clean_code, finding_text, findings_from_client,
                               merge_findings, parse_area, parse_finding_text, SINGLE_KINDS)
from src.clients.store import ClientStore

log = logging.getLogger("qcbot.app")


# --- the client dialog -----------------------------------------------------------------------------

class DropList(QListWidget):
    """The files to import: drop them here or use "Add files...". Only .xlsx, .csv and .pdf."""
    changed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setMinimumHeight(70)
        self.setToolTip("Drop the client template, the QC review workbook, a .csv or a style guide .pdf here")

    def paths(self) -> list[Path]:
        return [Path(self.item(i).data(Qt.ItemDataRole.UserRole)) for i in range(self.count())]

    def add_paths(self, paths) -> list[str]:
        """Adds the importable ones; returns the names of the others."""
        skipped = []
        have = set(self.paths())
        for p in map(Path, paths):
            if p.suffix.lower() not in IMPORT_EXTENSIONS or not p.is_file():
                skipped.append(p.name)
            elif p not in have:
                self.addItem(p.name)
                self.item(self.count() - 1).setData(Qt.ItemDataRole.UserRole, str(p))
                have.add(p)
        self.changed.emit()
        return skipped

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, event) -> None:
        skipped = self.add_paths(u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile())
        event.acceptProposedAction()
        if skipped:
            QMessageBox.information(self, "Video QC Bot", "Only .xlsx, .csv and .pdf files can be imported. Skipped: "
                                    + ", ".join(skipped))


class ClientDialog(QDialog):
    def __init__(self, store: ClientStore, config: dict, code: str | None = None, parent: QWidget | None = None,
                 last_folder: str | None = None):
        super().__init__(parent)
        self.store, self.config = store, config
        self.editing = code is not None
        self.saved_code: str | None = None
        self.last_folder = last_folder or str(Path.home())
        current = store.load(code) if self.editing else {}
        self.current = current
        self.current_findings = findings_from_client(current, store.color_block(code)) if self.editing else []
        single = {f.kind: f.value for f in self.current_findings if f.kind in SINGLE_KINDS}
        self.setWindowTitle(f"Edit client {code}" if self.editing else "Add new client")
        self.setMinimumWidth(900)

        self.code = QLineEdit(single.get("code", code or ""))
        self.code.setPlaceholderText("e.g. E8D or 8D")
        self.code.setReadOnly(self.editing)
        self.name = QLineEdit(single.get("name", ""))
        self.name.setPlaceholderText("e.g. Danja promo videos")
        self.language = QComboBox()
        for key, label in LANGUAGES.items():
            self.language.addItem(f"{label} ({key})", key)
        self.language.setCurrentIndex(max(0, self.language.findData(single.get("language", "en"))))
        self.min_words = QSpinBox()
        self.min_words.setRange(1, 10)
        default_min = int((config.get("checks") or {}).get("too_short", {}).get("min_words", 2))
        self.min_words.setValue(int(single.get("min_words", default_min)))
        self.area = QLineEdit(area_text(single.get("caption_area")))
        self.area.setToolTip("full, bottom, middle, top, or left, top, width, height as fractions of the frame")
        pick_area = QPushButton("Pick on a video frame...")
        pick_area.clicked.connect(self.pick_area)
        area_row = QHBoxLayout()
        area_row.addWidget(self.area, 1)
        area_row.addWidget(pick_area)
        self.notes = QPlainTextEdit(str(current.get("notes") or ""))
        self.notes.setFixedHeight(80)
        # What the user changed: typed values beat values found in files; untouched defaults don't.
        self.touched: set[str] = set(single) if self.editing else set()
        self.name.textEdited.connect(lambda _t: self.touched.add("name"))
        self.min_words.valueChanged.connect(lambda _v: self.touched.add("min_words"))
        self.language.activated.connect(lambda _i: self.touched.add("language"))
        self.area.textEdited.connect(lambda _t: self.touched.add("caption_area"))

        self.files = DropList()
        add_files = QPushButton("Add files...")
        add_files.clicked.connect(self.choose_files)
        remove = QPushButton("Remove")
        remove.clicked.connect(lambda: [self.files.takeItem(self.files.row(i)) for i in self.files.selectedItems()])
        file_buttons = QHBoxLayout()
        file_buttons.addWidget(add_files)
        file_buttons.addWidget(remove)
        file_buttons.addStretch(1)

        details = theme.card("Client")
        form = QFormLayout(details)
        form.setContentsMargins(12, 4, 12, 8)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(6)
        form.addRow("Client code:", self.code)
        form.addRow("Display name:", self.name)
        form.addRow("Language:", self.language)
        form.addRow("Minimum words per caption:", self.min_words)
        form.addRow("Caption area:", area_row)
        form.addRow("Notes:", self.notes)
        self.guides = self._guidelines_box(current, config)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        review = theme.primary(buttons.addButton("Review and save...", QDialogButtonBox.ButtonRole.AcceptRole))
        review.setDefault(True)
        buttons.accepted.connect(self.review_and_save)
        buttons.rejected.connect(self.reject)

        files = theme.card("Optional files")
        files_layout = QVBoxLayout(files)
        files_layout.setContentsMargins(12, 4, 12, 8)
        files_layout.addWidget(theme.hint("Client template .xlsx, QC review workbook .xlsx, .csv, style guide .pdf. "
                                          "Drop them here. They're read on this PC, and you see everything found "
                                          "before anything is saved."))
        self.files.setMaximumHeight(110)
        files_layout.addWidget(self.files)
        files_layout.addLayout(file_buttons)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 10, 18, 12)
        layout.setSpacing(6)
        layout.addWidget(theme.header(f"Edit client {code}" if self.editing else "Add a new client"))
        columns = QHBoxLayout()          # side by side: laptop screens are wide but short
        columns.setSpacing(12)
        columns.addWidget(details, 1)
        right = QVBoxLayout()
        right.setSpacing(6)
        right.addWidget(self.guides)
        right.addWidget(files)
        right.addStretch(1)
        columns.addLayout(right, 1)
        layout.addLayout(columns)
        layout.addWidget(buttons)

    # --- actions -------------------------------------------------------------------------------

    def choose_files(self) -> None:
        names, _ = QFileDialog.getOpenFileNames(self, "Files about this client", self.last_folder,
                                                "Client files (*.xlsx *.csv *.pdf);;All files (*)")
        if names:
            self.last_folder = str(Path(names[0]).parent)
            skipped = self.files.add_paths(names)
            if skipped:
                QMessageBox.information(self, "Video QC Bot", "Skipped (not .xlsx, .csv or .pdf): " + ", ".join(skipped))

    def pick_area(self) -> None:
        video, _ = QFileDialog.getOpenFileName(self, "A sample video of this client", self.last_folder,
                                               "Videos (*.mp4 *.mov *.m4v *.mkv *.avi *.mxf *.webm);;All files (*)")
        if not video:
            return
        self.last_folder = str(Path(video).parent)
        try:
            region = parse_area(self.area.text())
        except ClientError:
            region = None
        dialog = CaptionAreaDialog(Path(video), region, self)
        if dialog.frames_loaded and dialog.exec() == QDialog.DialogCode.Accepted and dialog.region is not None:
            self.area.setText(area_text(dialog.region))
            self.touched.add("caption_area")

    def _guidelines_box(self, current: dict, config: dict) -> QGroupBox:
        """Podcast audio guidelines: where music and effects are placed on purpose, the client's
        reference audio, and how wide the ignored windows are. Saved only when ticked; folded away
        while it isn't, so the dialog fits a laptop screen."""
        g = {**(config.get("audio_guidelines") or {}), **(current.get("audio_guidelines") or {})}
        box = QGroupBox("Podcast audio guidelines (music and sound effects placed on purpose)")
        box.setCheckable(True)
        box.setChecked(bool(current.get("audio_guidelines") or current.get("audio")))
        self.music = {p: QCheckBox(p.replace("_", " ")) for p in MUSIC_PLACES}
        self.sfx = {p: QCheckBox(p) for p in SFX_PLACES}
        for places, boxes in ((g.get("music_allowed") or [], self.music), (g.get("sfx_allowed") or [], self.sfx)):
            for p, cb in boxes.items():
                cb.setChecked(p in places)
        self.reference = QLineEdit(str(g.get("reference_folder") or ""))
        self.reference.setPlaceholderText("folder with the client's stingers, jingles, beds and effects")
        browse = QPushButton("Choose...")
        browse.clicked.connect(lambda: self._choose_reference())
        self.guide_sensitivity = QComboBox()
        for key in GUIDE_SENSITIVITIES:
            self.guide_sensitivity.addItem(key, key)
        self.guide_sensitivity.setCurrentIndex(max(0, self.guide_sensitivity.findData(g.get("sensitivity", "normal"))))
        content = QWidget()
        form = QFormLayout(content)
        form.setContentsMargins(0, 0, 0, 0)
        form.setVerticalSpacing(6)
        for label, boxes in (("Music allowed:", self.music), ("Effects allowed:", self.sfx)):
            grid = QGridLayout()
            grid.setHorizontalSpacing(14)
            for i, cb in enumerate(boxes.values()):
                grid.addWidget(cb, i // 3, i % 3)
            grid.setColumnStretch(3, 1)
            form.addRow(label, grid)
        ref_row = QHBoxLayout()
        ref_row.addWidget(self.reference, 1)
        ref_row.addWidget(browse)
        form.addRow("Reference audio:", ref_row)
        form.addRow("Ignore around them:", self.guide_sensitivity)
        outer = QVBoxLayout(box)
        outer.setContentsMargins(12, 4, 12, 8)
        outer.addWidget(content)
        theme.fold(box, content)
        return box

    def _choose_reference(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "The client's reference audio", self.reference.text()
                                                  or self.last_folder)
        if folder:
            self.reference.setText(str(Path(folder)))

    def guideline_findings(self) -> list[Finding]:
        if not self.guides.isChecked():
            return []
        src = "typed in the dialog"
        out = [Finding("check", {"setting": "audio_guidelines.music_allowed",
                                 "value": [p for p, cb in self.music.items() if cb.isChecked()]}, src),
               Finding("check", {"setting": "audio_guidelines.sfx_allowed",
                                 "value": [p for p, cb in self.sfx.items() if cb.isChecked()]}, src),
               Finding("check", {"setting": "audio_guidelines.sensitivity",
                                 "value": self.guide_sensitivity.currentData()}, src)]
        if self.reference.text().strip():
            out.append(Finding("check", {"setting": "audio_guidelines.reference_folder",
                                         "value": self.reference.text().strip()}, src))
        return out

    def typed_findings(self) -> list[Finding]:
        src = "typed in the dialog"
        out = [Finding("code", self.code.text().strip(), src)]
        if self.name.text().strip():
            out.append(Finding("name", self.name.text().strip(), src))
        out.append(Finding("language", self.language.currentData(), src))
        out.append(Finding("min_words", self.min_words.value(), src))
        area = parse_area(self.area.text())
        if area is not None:
            out.append(Finding("caption_area", area, src))
        return out

    def review_and_save(self) -> None:
        try:
            if not self.editing:
                code = clean_code(self.code.text())
                if self.store.exists(code):
                    raise ClientError(f"There is already a client {code}. Close this and use Edit client... instead.")
            typed = self.typed_findings()
        except ClientError as exc:
            QMessageBox.warning(self, "Video QC Bot", str(exc))
            return
        imported: list[Finding] = []
        problems: list[str] = []
        code_hint = self.code.text().strip() or None
        for path in self.files.paths():
            try:
                imported += import_file(path, code_hint)
            except ClientError as exc:
                problems.append(str(exc))
        if problems:
            QMessageBox.warning(self, "Video QC Bot", "Some files couldn't be read and were left out:\n\n"
                                + "\n".join(problems))
        first_typed = [f for f in typed if f.kind in self.touched or f.kind == "code" or self.editing]
        first_typed += self.guideline_findings()
        later_typed = [f for f in typed if f not in first_typed]
        rest = [f for f in self.current_findings if f.kind not in SINGLE_KINDS]
        findings = merge_findings(first_typed, rest, imported, later_typed)
        pdfs = [p for p in self.files.paths() if p.suffix.lower() == ".pdf"]
        review = ReviewDialog(findings, self.save_findings, self,
                              claude=(lambda: self.claude_findings(pdfs)) if pdfs and claude_allowed(self.config)
                              else None)
        if review.exec() == QDialog.DialogCode.Accepted:
            self.accept()

    def claude_findings(self, pdfs: list[Path]) -> list[Finding]:
        settings = (self.config.get("checks") or {}).get("punctuation", {})
        out: list[Finding] = []
        for pdf in pdfs:
            out += read_with_claude(pdf_text(pdf), pdf.name, settings)
        return out

    def save_findings(self, findings: list[Finding]) -> str | None:
        """Called by the review dialog's Save. None when saved; else the problem, for the user."""
        try:
            built = build_client(findings, base=self.current, notes_text=self.notes.toPlainText())
            changes = self.store.save(built, "edit" if self.editing else "add", sources=self.files.paths())
        except ClientError as exc:
            return str(exc)
        except OSError as exc:
            log.exception("saving client failed")
            return f"The client couldn't be saved: {exc}"
        self.saved_code = built.code
        log.info("client %s %s: %s", built.code, "edited" if self.editing else "added", changes or "no changes")
        return None


# --- review ----------------------------------------------------------------------------------------

class ReviewDialog(QDialog):
    """Every finding, grouped by kind: tick to keep, double-click to edit, where it came from."""

    def __init__(self, findings: list[Finding], on_save: Callable[[list[Finding]], str | None],
                 parent: QWidget | None = None, claude: Callable[[], list[Finding]] | None = None):
        super().__init__(parent)
        self.on_save = on_save
        self.claude = claude
        self.setWindowTitle("Review the client's rules before saving")
        self.resize(900, 620)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Found (double-click to edit, untick to leave out)", "Where it came from"])
        self.tree.setColumnWidth(0, 560)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                  | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.groups: dict[str, QTreeWidgetItem] = {}
        self.add_findings(findings)

        tick, untick = QPushButton("Tick all"), QPushButton("Untick all")
        tick.clicked.connect(lambda: self.set_all(True))
        untick.clicked.connect(lambda: self.set_all(False))
        row = QHBoxLayout()
        row.addWidget(tick)
        row.addWidget(untick)
        if claude is not None:
            ask = QPushButton("Read the PDF with Claude too")
            ask.setToolTip("Sends the PDF's text to the Anthropic API (switched on by an admin). The results are added "
                           "here, unticked, for you to check.")
            ask.clicked.connect(self.ask_claude)
            row.addWidget(ask)
        row.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        save = theme.primary(buttons.addButton("Save client", QDialogButtonBox.ButtonRole.AcceptRole))
        save.setDefault(True)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Nothing is saved until you press <b>Save client</b>. Notes are kept in the client's "
                                "notes.md. The previous version of the client file is backed up."))
        layout.addWidget(self.tree, 1)
        layout.addLayout(row)
        layout.addWidget(buttons)

    def add_findings(self, findings: list[Finding]) -> None:
        for f in findings:
            group = self.groups.get(f.kind)
            if group is None:
                group = QTreeWidgetItem(self.tree, [KINDS[f.kind], ""])
                group.setFlags(Qt.ItemFlag.ItemIsEnabled)
                font = group.font(0)
                font.setBold(True)
                group.setFont(0, font)
                group.setExpanded(True)
                self.groups[f.kind] = group
            item = QTreeWidgetItem(group, [finding_text(f), f.source])
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEditable
                          | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked if f.keep else Qt.CheckState.Unchecked)
            item.setData(0, Qt.ItemDataRole.UserRole, f)
        for group in self.groups.values():
            group.setText(0, f"{KINDS[group.child(0).data(0, Qt.ItemDataRole.UserRole).kind]} ({group.childCount()})")

    def set_all(self, on: bool) -> None:
        for group in self.groups.values():
            for i in range(group.childCount()):
                group.child(i).setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)

    def findings(self) -> list[Finding]:
        """The findings as reviewed: edited text read back, ticks as keep. ClientError on bad text."""
        out = []
        for group in self.groups.values():
            for i in range(group.childCount()):
                item = group.child(i)
                f: Finding = item.data(0, Qt.ItemDataRole.UserRole)
                keep = item.checkState(0) == Qt.CheckState.Checked
                text = item.text(0)
                value = f.value if text == finding_text(f) else parse_finding_text(f.kind, text)
                out.append(Finding(f.kind, value, f.source, keep))
        return out

    def ask_claude(self) -> None:
        try:
            found = self.claude()
        except Exception as exc:                  # network, key, refusal: say it, keep the dialog
            QMessageBox.warning(self, "Video QC Bot", f"Claude couldn't read it: {exc}")
            return
        self.add_findings([Finding(f.kind, f.value, f.source, keep=False) for f in found])

    def save(self) -> None:
        try:
            findings = self.findings()
        except ClientError as exc:
            QMessageBox.warning(self, "Video QC Bot", str(exc))
            return
        problem = self.on_save(findings)
        if problem:
            QMessageBox.warning(self, "Video QC Bot", problem)
            return
        self.accept()


# --- caption area --------------------------------------------------------------------------------------

def load_frames(video: Path, count: int = 30, every_sec: float = 2.0) -> list[tuple[float, QImage]]:
    """Up to `count` frames, one every `every_sec` seconds from the start, as QImages."""
    from src.inputs.video_ocr import sample_frames

    frames = []
    for t, _duration, rgb in sample_frames(video, 1.0 / every_sec):
        h, w = rgb.shape[:2]
        image = QImage(rgb.tobytes(), w, h, 3 * w, QImage.Format.Format_RGB888).copy()
        frames.append((t, image))
        if len(frames) >= count:
            break
    return frames


class FrameView(QWidget):
    """A frame, scaled to fit, with a rectangle you drag out with the mouse."""
    region_changed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.image: QImage | None = None
        self.region: dict | None = None
        self._start: QPoint | None = None
        self.setMinimumSize(QSize(480, 360))
        self.setCursor(Qt.CursorShape.CrossCursor)

    def set_image(self, image: QImage) -> None:
        self.image = image
        self.update()

    def image_rect(self) -> QRect:
        if self.image is None:
            return QRect()
        size = self.image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        return QRect((self.width() - size.width()) // 2, (self.height() - size.height()) // 2,
                     size.width(), size.height())

    def to_fraction(self, p: QPoint) -> tuple[float, float]:
        r = self.image_rect()
        x = min(max((p.x() - r.left()) / max(1, r.width()), 0.0), 1.0)
        y = min(max((p.y() - r.top()) / max(1, r.height()), 0.0), 1.0)
        return x, y

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._start = event.position().toPoint()

    def mouseMoveEvent(self, event) -> None:
        if self._start is not None:
            self._set_from(self._start, event.position().toPoint())

    def mouseReleaseEvent(self, event) -> None:
        if self._start is not None:
            self._set_from(self._start, event.position().toPoint())
            self._start = None

    def _set_from(self, a: QPoint, b: QPoint) -> None:
        (x1, y1), (x2, y2) = self.to_fraction(a), self.to_fraction(b)
        left, top, right, bottom = min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)
        if right - left > 0.01 and bottom - top > 0.01:
            self.region = {"left": round(left, 3), "top": round(top, 3), "width": round(right - left, 3),
                           "height": round(bottom - top, 3)}
            self.region_changed.emit()
            self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#202020"))
        if self.image is None:
            return
        r = self.image_rect()
        painter.drawPixmap(r, QPixmap.fromImage(self.image))
        if self.region:
            box = QRect(int(r.left() + self.region["left"] * r.width()), int(r.top() + self.region["top"] * r.height()),
                        int(self.region["width"] * r.width()), int(self.region["height"] * r.height()))
            painter.fillRect(box, QColor(255, 210, 0, 60))
            painter.setPen(QPen(QColor(255, 210, 0), 2))
            painter.drawRect(box)


class CaptionAreaDialog(QDialog):
    def __init__(self, video: Path, region: dict | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Caption area: {video.name}")
        self.resize(900, 700)
        self.frames: list[tuple[float, QImage]] = []
        try:
            self.frames = load_frames(video)
        except Exception as exc:                 # unreadable video: say so instead of opening an empty picker
            QMessageBox.warning(parent, "Video QC Bot", f"Couldn't read frames from {video.name}: {exc}")
        self.frames_loaded = bool(self.frames)
        self.view = FrameView()
        self.view.region = region
        self.label = QLabel()
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, max(0, len(self.frames) - 1))
        self.slider.valueChanged.connect(self.show_frame)
        self.view.region_changed.connect(self._update_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Move the slider to a frame with a caption, then drag a rectangle around where the "
                                "captions appear (leave a little room for two-line captions)."))
        layout.addWidget(self.view, 1)
        layout.addWidget(self.slider)
        layout.addWidget(self.label)
        layout.addWidget(buttons)
        if self.frames:
            self.slider.setValue(min(2, len(self.frames) - 1))
            self.show_frame(self.slider.value())
        self._update_label()

    @property
    def region(self) -> dict | None:
        return self.view.region

    def show_frame(self, index: int) -> None:
        if 0 <= index < len(self.frames):
            t, image = self.frames[index]
            self.view.set_image(image)
            self._time = t
            self._update_label()

    def _update_label(self) -> None:
        r = self.view.region
        where = f"Frame at {getattr(self, '_time', 0):.0f} s. " if self.frames else ""
        self.label.setText(where + (f"Area: left {r['left']:.3f}, top {r['top']:.3f}, width {r['width']:.3f}, "
                                    f"height {r['height']:.3f} (fractions of the frame)" if r else "No area drawn yet."))
