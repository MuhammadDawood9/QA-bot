"""The floating mascot: a sprite you can drag anywhere, with a glow drawn behind it.

Two windows, both frameless, transparent and always on top:
  MascotWindow  the sprite. Its window is masked to the sprite's own shape (a circle for round
                skins, plus the badge), so that shape is the one hit region for hover, click,
                drag and drop. Outside it the window doesn't exist: clicks go to whatever is
                underneath, the same on Windows (where only fully transparent pixels would pass
                clicks through) and macOS (where no pixel would).
  GlowWindow    the halo, the thin ring on the circle's border, the progress ring and the drop
                highlight, a little larger than the sprite and behind it. It ignores the mouse
                entirely, so the glow can never block a click, and it is never clipped by the
                sprite's mask.

Glow: a halo outside the circle's edge (color at the edge fading to nothing ~14 px out, over a
faint dark shadow), pre-rendered once per color; frames only change its opacity and scale.
Colors, strengths and timings come from the skin (skin.json "glow", defaults in
assets.DEFAULT_GLOW). Idle: soft pink-violet, static. Working: blue-violet, breathing every
1.4 s. Done: a brief bright green pulse, then back to idle over 3 s. Error: red, steady. Only
the glow window repaints, and only while working or during the done pulse.

Blinks: one QTimer. On hover the eyes close for 120 ms, then again every 3 s while the cursor
stays. Optionally (setting "idle_blink") a random blink every 5-10 s. Skins with "eyes": "frames"
swap to <state>_blink.png; skins with "eyes": "drawn" paint the eyes closed.

Position: the window's top-left is saved after a drag and restored at the next launch, pulled
back onto a screen if that screen is gone.
"""

from __future__ import annotations

import logging
import math
import random
from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import (QBitmap, QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPen, QPixmap,
                           QRadialGradient, QRegion)
from PySide6.QtWidgets import QWidget

from src.app.assets import STATES, Skin, load_skin
from src.app.choices import videos_in
from src.app.settings import Settings
from src.platform import current as platform

log = logging.getLogger("qcbot.app")

SPRITE = 88                   # the sprite on screen, logical pixels; Qt scales for high-DPI screens
ART = 120                     # the size skins are drawn at (eye positions in skin.json use it)
PAD = 32                      # transparent margin around it, so the halo is never clipped
WINDOW = SPRITE + 2 * PAD     # 152
CENTER = WINDOW / 2
EDGE = SPRITE / 2             # the badge circle's radius; the halo starts here
EDGE_MARGIN = 24              # gap from the screen edge for the default position
DRAG_THRESHOLD = 4            # pixels the mouse must move before a press counts as a drag

BLINK_MS = 120                # eyes closed
HOVER_BLINK_EVERY_MS = 3000   # again while the cursor stays
IDLE_BLINK_MS = (5000, 10000)  # random gap for the optional idle blink

SHADOW_OFFSET = 1.5           # the shadow sits a little lower than the halo
RING_RADIUS = EDGE + 6        # the progress ring
BADGE_HEIGHT = 24
BADGE_FONT = 13


def badge_text(count: int) -> str:
    return "99+" if count > 99 else str(count)


def badge_rect(count: int) -> QRect:
    """Where the issue-count badge goes: over the sprite's top-right corner, inside the window."""
    font = QFont()
    font.setBold(True)
    font.setPixelSize(BADGE_FONT)
    width = max(BADGE_HEIGHT, QFontMetrics(font).horizontalAdvance(badge_text(count)) + 12)
    return QRect(PAD + SPRITE - width + 6, PAD - 5, width, BADGE_HEIGHT)


# --- the glow --------------------------------------------------------------------------------

def _smooth(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def glow_layers(state: str, t: float, glow: dict) -> list[tuple[str, float, float, float]]:
    """What the glow shows for a state at animation time t (0-1): a list of
    (color of state, halo opacity, halo scale, ring weight). Colors and timings come from the
    skin's glow settings (assets.DEFAULT_GLOW).

    working  t runs 0-1 once per period; the halo breathes (1 - cos) between its low and high
             strength and scale, easing in and out.
    done     t runs 0-1 over return_ms: a bright pulse for flash_ms, a moment of steady green,
             then a crossfade to the idle glow, which stays when t reaches 1.
    """
    if state == "working":
        w = glow["working"]
        breath = (1 - math.cos(2 * math.pi * t)) / 2
        (lo, hi), (s0, s1) = w["strength"], w["scale"]
        return [("working", lo + (hi - lo) * breath, s0 + (s1 - s0) * breath, 1.0)]
    if state == "done":
        d, idle = glow["done"], glow["idle"]["strength"]
        flash = min(0.9, d["flash_ms"] / max(1, d["return_ms"]))
        if t < flash:
            p = math.sin(math.pi * t / flash)
            return [("done", d["strength"] + (1 - d["strength"]) * p, 1 + 0.06 * p, 1.0)]
        hold = flash + (1 - flash) * 0.35
        f = _smooth((t - hold) / (1 - hold))
        if f <= 0:
            return [("done", d["strength"], 1.0, 1.0)]
        if f >= 1:
            return [("idle", idle, 1.0, 1.0)]
        return [("done", d["strength"] * (1 - f), 1.0, 1 - f), ("idle", idle * f, 1.0, f)]
    return [(state, glow[state]["strength"], 1.0, 1.0)]


class GlowWindow(QWidget):
    """The halo, the thin ring, the progress ring and the drop highlight behind the sprite.
    Never takes the mouse.

    Each halo color is rendered once into a QPixmap (at the screen's pixel ratio); a frame only
    draws that pixmap with an opacity and a scale. One QVariantAnimation drives the working pulse
    and the done pulse; it is stopped in every other state, so idle and error never repaint."""

    def __init__(self, skin: Skin):
        flags = (platform.floating_window_flags() | Qt.WindowType.WindowTransparentForInput
                 | Qt.WindowType.WindowDoesNotAcceptFocus)
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFixedSize(WINDOW, WINDOW)
        self.mode = "idle"
        self.t = 0.0
        self.progress: float | None = None
        self.drop_hover = False
        self._halos: dict[tuple[str, float], QPixmap] = {}
        self.animation = QVariantAnimation(self)          # the one animation: working pulse, done pulse
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.valueChanged.connect(self._tick)
        self.set_skin(skin)

    def set_skin(self, skin: Skin) -> None:
        self.glow = skin.glow
        self.ring = skin.shape == "circle"
        self._halos.clear()
        self.update()

    def set_mode(self, state: str) -> None:
        """working: pulse, done: a pulse then back to idle, idle and error: steady."""
        self.animation.stop()
        self.mode, self.t = state, 0.0
        if state == "working":
            self.animation.setDuration(int(self.glow["working"]["period_ms"]) or 1400)
            self.animation.setLoopCount(-1)
            self.animation.start()
        elif state == "done":
            self.animation.setDuration(int(self.glow["done"]["return_ms"]) or 3000)
            self.animation.setLoopCount(1)
            self.animation.start()
        self.update()

    def _tick(self, value) -> None:
        self.t = float(value)
        self.update()

    @property
    def animating(self) -> bool:
        return self.animation.state() == QVariantAnimation.State.Running

    def layers(self) -> list[tuple[str, float, float, float]]:
        t = 1.0 if self.mode == "done" and not self.animating else self.t
        return glow_layers(self.mode, t, self.glow)

    def color(self, state: str) -> QColor:
        return QColor(self.glow[state]["color"])

    # --- drawing -------------------------------------------------------------------------

    def _limits(self) -> tuple[float, float]:
        """The halo's width and largest scale, kept inside the window whatever skin.json says."""
        room = CENTER - EDGE - SHADOW_OFFSET - 3
        width = min(float(self.glow["width"]), room)
        return width, min(float(self.glow["working"]["scale"][1]) + 0.06, (CENTER - 1) / (EDGE + width + 3))

    def halo(self, state: str, dpr: float) -> QPixmap:
        """The halo of one color at strength 1, rendered once per color and pixel ratio: a faint
        dark shadow, then the color from edge_opacity at the circle edge to nothing `width` beyond."""
        key = (state, dpr)
        if key not in self._halos:
            width, _ = self._limits()
            outer = EDGE + width
            pixmap = QPixmap(round(WINDOW * dpr), round(WINDOW * dpr))
            pixmap.setDevicePixelRatio(dpr)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            shadow = QRadialGradient(QPointF(CENTER, CENTER + SHADOW_OFFSET), outer + 3)
            dark = float(self.glow["shadow_opacity"])
            for stop, alpha in ((0.0, dark), (EDGE / (outer + 3), dark), (1.0, 0.0)):
                shadow.setColorAt(stop, QColor(0, 0, 0, round(255 * alpha)))
            painter.setBrush(shadow)
            painter.drawEllipse(QPointF(CENTER, CENTER + SHADOW_OFFSET), outer + 3, outer + 3)
            gradient = QRadialGradient(QPointF(CENTER, CENTER), outer)
            edge = float(self.glow["edge_opacity"])
            inner = EDGE / outer
            for stop, alpha in ((0.0, edge), (inner, edge), (inner + (1 - inner) * 0.35, edge * 0.5),
                                (inner + (1 - inner) * 0.7, edge * 0.15), (1.0, 0.0)):
                color = self.color(state)
                color.setAlphaF(alpha)
                gradient.setColorAt(stop, color)
            painter.setBrush(gradient)
            painter.drawEllipse(QPointF(CENTER, CENTER), outer, outer)
            painter.end()
            self._halos[key] = pixmap
        return self._halos[key]

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        dpr = self.devicePixelRatioF()
        _, max_scale = self._limits()
        layers = self.layers()
        for state, opacity, scale, _ in layers:
            if opacity <= 0.005:
                continue
            painter.save()
            painter.setOpacity(min(1.0, opacity))
            scale = min(scale, max_scale)
            painter.translate(CENTER, CENTER)
            painter.scale(scale, scale)
            painter.translate(-CENTER, -CENTER)
            painter.drawPixmap(0, 0, self.halo(state, dpr))
            painter.restore()
        if self.ring:                                   # the thin bright border, always at full strength
            ring_width = float(self.glow["ring_width"])
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for state, _, _, weight in layers:
                if weight <= 0.005:
                    continue
                painter.setOpacity(weight)
                painter.setPen(QPen(self.color(state).lighter(135), ring_width))
                r = EDGE + ring_width / 2 - 1               # tucked one pixel under the image's edge
                painter.drawEllipse(QPointF(CENTER, CENTER), r, r)
            painter.setOpacity(1.0)
        if self.progress is not None:
            rect = QRectF(CENTER - RING_RADIUS, CENTER - RING_RADIUS, 2 * RING_RADIUS, 2 * RING_RADIUS)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(255, 255, 255, 170), 5))
            painter.drawArc(rect, 0, 360 * 16)
            painter.setPen(QPen(QColor("#2E7DF6"), 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawArc(rect, 90 * 16, -int(360 * 16 * self.progress))    # clockwise from the top
        if self.drop_hover:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#2E7DF6"), 4, Qt.PenStyle.DashLine))
            painter.drawEllipse(QPointF(CENTER, CENTER), CENTER - 6, CENTER - 6)
        painter.end()


# --- the sprite ------------------------------------------------------------------------------

class MascotWindow(QWidget):
    state_changed = Signal(str)
    moved_by_user = Signal(QPoint)
    clicked = Signal()                    # a left click that wasn't a drag
    files_dropped = Signal(list)          # list[Path] of the video files dropped on it
    cue_sheets_dropped = Signal(list, list)   # (media, .csv files) dropped together: cue sheets for episodes

    def __init__(self, settings: Settings, skin: Skin | None = None):
        super().__init__(None, platform.floating_window_flags())
        self.settings = settings
        self.skin = skin if skin is not None else load_skin(settings.get("skin"))
        self.glow = GlowWindow(self.skin)
        self.state = "idle"
        self.count: int | None = None
        self.progress: float | None = None
        self.eyes_closed = False
        self.hovered = False
        self.idle_blink = bool(settings.get("idle_blink"))
        self._press_offset: QPoint | None = None
        self._press_global: QPoint | None = None
        self._dragging = False
        self._region = QRegion()

        self.blink_timer = QTimer(self)                 # the one timer for every blink
        self.blink_timer.setSingleShot(True)
        self.blink_timer.timeout.connect(self._blink_tick)

        self.setAcceptDrops(True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, False)          # enter/leave are enough; no hover repaints
        self.setFixedSize(WINDOW, WINDOW)
        self.setWindowTitle("Video QC Bot")
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.move(self.start_position())
        self._update_shape()
        self._schedule_blink()

    # --- state ---------------------------------------------------------------------------

    def set_state(self, state: str, count: int | None = None) -> None:
        """idle | working | done (count = issues found, shown as a badge) | error."""
        if state not in STATES:
            raise ValueError(f"unknown mascot state {state!r}; expected one of {STATES}")
        self.state, self.count = state, (count if state == "done" else None)
        if state != "working":
            self.progress = None
            self.glow.progress = None
        self.glow.set_mode(state)
        self.setToolTip({"idle": "Video QC Bot", "working": "Checking...",
                         "done": f"Done: {count} issue{'s' if count != 1 else ''}" if count is not None else "Done",
                         "error": "Something went wrong"}[state])
        self._update_shape()
        self.update()
        self.state_changed.emit(state)

    def set_progress(self, stage: str, fraction: float) -> None:
        """While working: the ring around the sprite, and the stage in the tooltip."""
        self.progress = min(1.0, max(0.0, fraction))
        self.glow.progress = self.progress
        self.setToolTip(f"{stage}... {self.progress:.0%}\nRight-click to cancel")
        self.glow.update()

    def set_skin(self, key: str) -> None:
        self.skin = load_skin(key)
        self.settings.set("skin", self.skin.key)
        self.glow.set_skin(self.skin)
        self.eyes_closed = False
        self._update_shape()
        self.update()

    def set_idle_blink(self, on: bool) -> None:
        self.idle_blink = on
        self.settings.set("idle_blink", on)
        if not self.eyes_closed:
            self._schedule_blink()

    # --- the hit region --------------------------------------------------------------------

    def sprite_region(self) -> QRegion:
        """The sprite's shape in window coordinates: the circle for round skins (one pixel wider,
        so its antialiased edge isn't cut), else the pixels at least half opaque."""
        if self.skin.shape == "circle":
            return QRegion(QRect(PAD - 1, PAD - 1, SPRITE + 2, SPRITE + 2), QRegion.RegionType.Ellipse)
        icon = self.skin.icons.get(self.state)
        if icon is None or icon.isNull():
            return QRegion(QRect(PAD + 8, PAD + 8, SPRITE - 16, SPRITE - 16), QRegion.RegionType.Ellipse)
        image = icon.pixmap(QSize(SPRITE, SPRITE), 1.0).toImage()
        mask = image.createAlphaMask(Qt.ImageConversionFlag.ThresholdAlphaDither)
        return QRegion(QBitmap.fromImage(mask)).translated(PAD, PAD)

    def hit_region(self) -> QRegion:
        """Hover, click, drag and drop all use this: the sprite, plus the badge when it shows."""
        return self._region

    def hit(self, point: QPoint) -> bool:
        if self.skin.shape == "circle":
            on_badge = self.state == "done" and self.count is not None and badge_rect(self.count).contains(point)
            return on_badge or math.hypot(point.x() - CENTER, point.y() - CENTER) <= EDGE
        return self._region.contains(point)

    def _update_shape(self) -> None:
        region = self.sprite_region()
        if self.state == "done" and self.count is not None:
            region = region.united(QRegion(badge_rect(self.count)))
        self._region = region
        self.setMask(region)                  # outside it the window doesn't exist (both OSes)

    # --- blinking --------------------------------------------------------------------------

    def _blink_tick(self) -> None:
        if self.eyes_closed:
            self.eyes_closed = False
            self.update()
            self._schedule_blink()
        else:
            self._close_eyes()

    def _close_eyes(self) -> None:
        if not self.skin.can_blink(self.state):
            self._schedule_blink()
            return
        self.eyes_closed = True
        self.update()
        self.blink_timer.start(BLINK_MS)

    def _schedule_blink(self) -> None:
        """The next blink: 3 s while hovered, a random 5-10 s with idle blinking on, else none."""
        if self.hovered:
            self.blink_timer.start(HOVER_BLINK_EVERY_MS)
        elif self.idle_blink:
            self.blink_timer.start(random.randint(*IDLE_BLINK_MS))
        else:
            self.blink_timer.stop()

    def enterEvent(self, event) -> None:
        self.hovered = True
        if not self.eyes_closed:
            self.blink_timer.stop()
            self._close_eyes()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.hovered = False
        if not self.eyes_closed:                      # a running blink reschedules itself when it ends
            self._schedule_blink()
        super().leaveEvent(event)

    # --- position --------------------------------------------------------------------------

    def start_position(self) -> QPoint:
        """The saved position if it's still on a screen, otherwise bottom-right of the main screen."""
        saved = self.settings.window_position
        if saved is not None:
            point = QPoint(*saved)
            if self.on_a_screen(point):
                return point
            log.info("saved mascot position %s is off every screen; using the default", saved)
        area = QGuiApplication.primaryScreen().availableGeometry()
        return QPoint(area.right() - WINDOW - EDGE_MARGIN + PAD, area.bottom() - WINDOW - EDGE_MARGIN + PAD)

    @staticmethod
    def on_a_screen(top_left: QPoint) -> bool:
        """At least half of the sprite would be visible on some screen."""
        sprite = QRect(top_left + QPoint(PAD, PAD), QSize(SPRITE, SPRITE))
        for screen in QGuiApplication.screens():
            visible = sprite.intersected(screen.availableGeometry())
            if visible.width() * visible.height() >= SPRITE * SPRITE / 2:
                return True
        return False

    def remember_position(self) -> None:
        self.settings.window_position = (self.x(), self.y())
        self.settings.save()

    # --- Qt events -------------------------------------------------------------------------

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.glow.move(self.pos())
        self.glow.show()
        platform.prepare_floating_window(self.glow)
        platform.prepare_floating_window(self)
        self.raise_()                                 # the sprite above its glow

    def hideEvent(self, event) -> None:
        self.glow.hide()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:
        self.glow.close()
        super().closeEvent(event)

    def moveEvent(self, event) -> None:
        self.glow.move(self.pos())
        super().moveEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        target = QRectF(PAD, PAD, SPRITE, SPRITE)
        icon = self.skin.icons.get(self.state)
        if self.eyes_closed and self.skin.eyes == "frames" and self.state in self.skin.blink_icons:
            icon = self.skin.blink_icons[self.state]
        if icon is not None and not icon.isNull():
            pixmap = icon.pixmap(QSize(SPRITE, SPRITE), self.devicePixelRatioF())   # the @2x image when needed
            painter.drawPixmap(target, pixmap, QRectF(pixmap.rect()))
            if self.skin.eyes == "drawn":
                self._paint_eyes(painter)
        else:
            self._paint_fallback(painter)
        if self.state == "done" and self.count is not None:
            self._paint_badge(painter, self.count)
        painter.end()

    def _paint_eyes(self, painter: QPainter) -> None:
        shape = self.skin.eye_shape
        k = SPRITE / ART                              # skin.json positions are in the 120 px art
        r = float(shape["radius"]) * k
        color = QColor(shape["color"])
        style = "closed" if self.eyes_closed else self.skin.state_eyes.get(self.state, "open")
        for x, y in (shape["left"], shape["right"]):
            c = QPointF(PAD + x * k, PAD + y * k)
            if style == "open":
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(color)
                painter.drawEllipse(c, r, r * 1.25)
                painter.setBrush(QColor(shape["highlight"]))
                painter.drawEllipse(c + QPointF(r * 0.3, -r * 0.45), r * 0.35, r * 0.35)
            elif style == "focused":
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(color)
                painter.drawEllipse(c + QPointF(0, r * 0.3), r, r * 0.85)
                painter.setPen(QPen(color, max(1.5, r * 0.45), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                painter.drawLine(c + QPointF(-r * 1.3, -r * 0.7), c + QPointF(r * 1.3, -r * 0.7))
            elif style == "happy":
                painter.setPen(QPen(color, max(1.5, r * 0.55), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawArc(QRectF(c.x() - r * 1.3, c.y() - r * 0.6, r * 2.6, r * 2.2), 30 * 16, 120 * 16)
            elif style == "cross":
                painter.setPen(QPen(color, max(1.5, r * 0.55), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                painter.drawLine(c + QPointF(-r, -r), c + QPointF(r, r))
                painter.drawLine(c + QPointF(-r, r), c + QPointF(r, -r))
            else:                                    # closed, for a blink
                painter.setPen(QPen(color, max(1.5, r * 0.55), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                painter.drawLine(c + QPointF(-r * 1.1, 0), c + QPointF(r * 1.1, 0))

    def _paint_fallback(self, painter: QPainter) -> None:
        """A plain circle with the state's name, when an image file is missing."""
        colors = {"idle": "#40A0AA", "working": "#4682C8", "done": "#46AA6E", "error": "#D25050"}
        painter.setBrush(QColor(colors[self.state]))
        painter.setPen(QPen(QColor("#1E2832"), 3))
        painter.drawEllipse(QRect(PAD + 8, PAD + 8, SPRITE - 16, SPRITE - 16))
        painter.setPen(QColor("white"))
        painter.drawText(QRect(PAD, PAD, SPRITE, SPRITE), Qt.AlignmentFlag.AlignCenter, self.state)

    def _paint_badge(self, painter: QPainter, count: int) -> None:
        font = QFont(painter.font())
        font.setBold(True)
        font.setPixelSize(BADGE_FONT)
        painter.setFont(font)
        rect = badge_rect(count)
        painter.setBrush(QColor("#E53935"))
        painter.setPen(QPen(QColor("white"), 2.5))
        painter.drawRoundedRect(QRectF(rect).adjusted(1.5, 1.5, -1.5, -1.5), BADGE_HEIGHT / 2 - 1.5, BADGE_HEIGHT / 2 - 1.5)
        painter.setPen(QColor("white"))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, badge_text(count))

    # --- mouse: drag and click, only on the sprite -----------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.hit(event.position().toPoint()):
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
            else:
                self.clicked.emit()
            self._press_offset = self._press_global = None
            self._dragging = False
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    # --- dropping files, only on the sprite ------------------------------------------------

    @staticmethod
    def _dropped_paths(event) -> list[Path]:
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        return [Path(url.toLocalFile()) for url in mime.urls() if url.isLocalFile()]

    def _drop_point(self, event) -> QPoint:
        return event.position().toPoint()

    def dragEnterEvent(self, event) -> None:
        if self.hit(self._drop_point(event)) and videos_in(self._dropped_paths(event)):
            event.acceptProposedAction()
            self.glow.drop_hover = True
            self.glow.update()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if self.hit(self._drop_point(event)):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self.glow.drop_hover = False
        self.glow.update()

    def dropEvent(self, event) -> None:
        self.glow.drop_hover = False
        self.glow.update()
        paths = self._dropped_paths(event)
        videos = videos_in(paths)
        if videos and self.hit(self._drop_point(event)):
            event.acceptProposedAction()
            csvs = [p for p in paths if p.suffix.lower() == ".csv" and p.is_file()]
            if csvs:
                self.cue_sheets_dropped.emit(videos, csvs)     # remembered before the checks start
            self.files_dropped.emit(videos)
        else:
            event.ignore()
