"""The floating mascot: a small frameless, transparent, always-on-top window you can drag anywhere.

States: idle, working, done (with a badge showing the issue count), error. Its position is
saved when you let go after dragging and restored at the next launch, pulled back onto a screen
if that screen is gone (a laptop undocked from its monitor).
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QWidget

from src.app.assets import STATES, mascot_icons
from src.app.settings import Settings
from src.platform import current as platform

log = logging.getLogger("qcbot.app")

SIZE = 120                    # logical pixels; Qt scales for high-DPI screens
EDGE_MARGIN = 24              # gap from the screen edge for the default position
DRAG_THRESHOLD = 4            # pixels the mouse must move before a press counts as a drag


def badge_text(count: int) -> str:
    return "99+" if count > 99 else str(count)


class MascotWindow(QWidget):
    state_changed = Signal(str)
    moved_by_user = Signal(QPoint)

    def __init__(self, settings: Settings, icons: dict | None = None):
        super().__init__(None, platform.floating_window_flags())
        self.settings = settings
        self.icons = icons if icons is not None else mascot_icons()
        self.state = "idle"
        self.count: int | None = None
        self._press_offset: QPoint | None = None
        self._press_global: QPoint | None = None
        self._dragging = False

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedSize(SIZE, SIZE)
        self.setWindowTitle("Video QC Bot")
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.move(self.start_position())

    # --- state ---------------------------------------------------------------------------

    def set_state(self, state: str, count: int | None = None) -> None:
        """idle | working | done (count = issues found, shown as a badge) | error."""
        if state not in STATES:
            raise ValueError(f"unknown mascot state {state!r}; expected one of {STATES}")
        self.state, self.count = state, (count if state == "done" else None)
        self.setToolTip({"idle": "Video QC Bot", "working": "Checking...",
                         "done": f"Done: {count} issue{'s' if count != 1 else ''}" if count is not None else "Done",
                         "error": "Something went wrong"}[state])
        self.update()
        self.state_changed.emit(state)

    # --- position ------------------------------------------------------------------------

    def start_position(self) -> QPoint:
        """The saved position if it's still on a screen, otherwise bottom-right of the main screen."""
        saved = self.settings.window_position
        if saved is not None:
            point = QPoint(*saved)
            if self.on_a_screen(point):
                return point
            log.info("saved mascot position %s is off every screen; using the default", saved)
        area = QGuiApplication.primaryScreen().availableGeometry()
        return QPoint(area.right() - SIZE - EDGE_MARGIN, area.bottom() - SIZE - EDGE_MARGIN)

    @staticmethod
    def on_a_screen(top_left: QPoint) -> bool:
        """At least half of the window would be visible on some screen."""
        window = QRect(top_left, QSize(SIZE, SIZE))
        for screen in QGuiApplication.screens():
            visible = window.intersected(screen.availableGeometry())
            if visible.width() * visible.height() >= SIZE * SIZE / 2:
                return True
        return False

    def remember_position(self) -> None:
        self.settings.window_position = (self.x(), self.y())
        self.settings.save()

    # --- Qt events -----------------------------------------------------------------------

    def showEvent(self, event) -> None:
        super().showEvent(event)
        platform.prepare_floating_window(self)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        icon = self.icons.get(self.state)
        if icon is not None and not icon.isNull():
            # The 2x image is used on high-DPI screens: ask for the window's device pixel ratio.
            pixmap = icon.pixmap(QSize(SIZE, SIZE), self.devicePixelRatioF())
            painter.drawPixmap(QRect(0, 0, SIZE, SIZE), pixmap)
        else:
            self._paint_fallback(painter)
        if self.state == "done" and self.count is not None:
            self._paint_badge(painter, badge_text(self.count))
        painter.end()

    def _paint_fallback(self, painter: QPainter) -> None:
        """A plain circle with the state's name, when an image file is missing."""
        colors = {"idle": "#40A0AA", "working": "#4682C8", "done": "#46AA6E", "error": "#D25050"}
        painter.setBrush(QColor(colors[self.state]))
        painter.setPen(QPen(QColor("#1E2832"), 3))
        painter.drawEllipse(QRect(8, 8, SIZE - 16, SIZE - 16))
        painter.setPen(QColor("white"))
        painter.drawText(QRect(0, 0, SIZE, SIZE), Qt.AlignmentFlag.AlignCenter, self.state)

    def _paint_badge(self, painter: QPainter, text: str) -> None:
        font = QFont(painter.font())
        font.setBold(True)
        font.setPixelSize(15)
        painter.setFont(font)
        width = max(30, painter.fontMetrics().horizontalAdvance(text) + 14)
        rect = QRect(SIZE - width - 2, 2, width, 30)
        painter.setBrush(QColor("#E53935"))
        painter.setPen(QPen(QColor("white"), 2.5))
        painter.drawRoundedRect(rect, 15, 15)
        painter.setPen(QColor("white"))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_global = event.globalPosition().toPoint()
            self._press_offset = self._press_global - self.frameGeometry().topLeft()
            self._dragging = False
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._press_offset is None or not (event.buttons() & Qt.MouseButton.LeftButton):
            return super().mouseMoveEvent(event)
        where = event.globalPosition().toPoint()
        if not self._dragging and (where - self._press_global).manhattanLength() >= DRAG_THRESHOLD:
            self._dragging = True
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        if self._dragging:
            self.move(where - self._press_offset)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._press_offset is not None:
            if self._dragging:
                self.remember_position()
                self.moved_by_user.emit(self.pos())
            self._press_offset = self._press_global = None
            self._dragging = False
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
        else:
            super().mouseReleaseEvent(event)
