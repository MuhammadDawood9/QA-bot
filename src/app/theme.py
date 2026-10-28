"""The app's look, taken from the mascot: menus and windows in the active skin's colors.

The accent is the skin's "working" glow color, the soft accent its "idle" glow, and the done and
error glows color good and bad news. Backgrounds and borders are greys tinted toward the accent's
hue, so a new skin recolors everything without a palette of its own. Light or dark follows the
OS and changes with it.

Everything is drawn with Qt's Fusion style plus one stylesheet, so Windows and macOS look the
same. (The tray menu on macOS is the system's own menu: Qt can't style it.)

Pieces for the windows:
  RoundMenu      a QMenu with rounded corners; its submenus are rounded too
  header(...)    the mascot's face, a title and a line under it, for the top of a dialog
  card(title)    a rounded box grouping related settings
  primary(btn)   the accent-filled button for a dialog's main action
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import (QApplication, QGroupBox, QHBoxLayout, QLabel, QMenu, QPushButton, QVBoxLayout,
                               QWidget, QWidgetAction)

from src.app.assets import Skin, load_skin

RADIUS = 10                   # corners of cards, inputs and buttons, logical pixels
MENU_RADIUS = 12
HEADER_ICON = 44              # the mascot's face at the top of a dialog
MENU_ICON = 36                # and at the top of the menu

# Greys tinted toward the accent hue: (saturation, lightness) out of 255, per theme.
_TINTS = {
    "dark": {"window": (34, 30), "surface": (34, 42), "raised": (34, 54), "border": (30, 74),
             "text": (24, 238), "muted": (20, 168)},
    "light": {"window": (60, 248), "surface": (0, 255), "raised": (70, 240), "border": (50, 222),
              "text": (40, 34), "muted": (24, 110)},
}


@dataclass
class Theme:
    skin: Skin
    dark: bool
    accent: QColor
    soft: QColor
    good: QColor
    bad: QColor
    window: QColor
    surface: QColor
    raised: QColor
    border: QColor
    text: QColor
    muted: QColor

    @property
    def on_accent(self) -> QColor:
        """Text on an accent-filled button: white unless the accent is very light."""
        return QColor("#FFFFFF") if self.accent.lightnessF() < 0.72 else QColor("#1A1726")


_current: Theme | None = None


def os_is_dark() -> bool:
    hints = QGuiApplication.styleHints()
    scheme = getattr(hints, "colorScheme", None)            # Qt 6.5+
    if scheme is not None:
        return scheme() == Qt.ColorScheme.Dark
    return QGuiApplication.palette().color(QPalette.ColorRole.Window).lightnessF() < 0.5


def make_theme(skin: Skin, dark: bool) -> Theme:
    glow = skin.glow
    accent = QColor(glow["working"]["color"])
    hue = max(0, accent.hslHue())
    tints = {name: QColor.fromHsl(hue, s, l) for name, (s, l) in _TINTS["dark" if dark else "light"].items()}
    return Theme(skin=skin, dark=dark, accent=accent, soft=QColor(glow["idle"]["color"]),
                 good=QColor(glow["done"]["color"]), bad=QColor(glow["error"]["color"]), **tints)


def current() -> Theme:
    """The theme in use (the default skin's, before the app has applied one)."""
    global _current
    if _current is None:
        _current = make_theme(load_skin(), os_is_dark())
    return _current


def _rgba(color: QColor, alpha: float) -> str:
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {alpha:.2f})"


def palette(t: Theme) -> QPalette:
    p = QPalette()
    roles = QPalette.ColorRole
    for role, color in ((roles.Window, t.window), (roles.WindowText, t.text), (roles.Base, t.surface),
                        (roles.AlternateBase, t.raised), (roles.Text, t.text), (roles.Button, t.raised),
                        (roles.ButtonText, t.text), (roles.ToolTipBase, t.raised), (roles.ToolTipText, t.text),
                        (roles.PlaceholderText, t.muted), (roles.Highlight, t.accent),
                        (roles.HighlightedText, t.on_accent), (roles.Link, t.accent if t.dark else t.accent.darker(115)),
                        (roles.LinkVisited, t.accent), (roles.BrightText, t.bad)):
        p.setColor(role, color)
    for role in (roles.WindowText, roles.Text, roles.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, t.muted)
    return p


def stylesheet(t: Theme) -> str:
    a = t.accent.name()
    soft = t.soft.name() if t.dark else t.accent.darker(115).name()     # headings: the pale glow fades on white
    hover = _rgba(t.accent, 0.22 if t.dark else 0.14)
    return f"""
QToolTip {{ background: {t.raised.name()}; color: {t.text.name()}; border: 1px solid {t.border.name()};
           border-radius: 6px; padding: 4px 8px; }}

QMenu {{ background: {t.surface.name()}; color: {t.text.name()}; border: 1px solid {t.border.name()};
        border-radius: {MENU_RADIUS}px; padding: 5px; }}
QMenu::item {{ padding: 4px 28px 4px 14px; border-radius: 7px; }}
QMenu::item:selected {{ background: {hover}; }}
QMenu::item:disabled {{ color: {t.muted.name()}; }}
QMenu::separator {{ height: 1px; background: {t.border.name()}; margin: 4px 8px; }}
QMenu::indicator {{ width: 14px; height: 14px; left: 6px; }}
QMenu::right-arrow {{ width: 8px; height: 8px; right: 10px; }}
QWidget#MenuHeader {{ background: {_rgba(t.accent, 0.16 if t.dark else 0.10)}; border-radius: 9px; }}
QLabel#MenuTitle {{ font-weight: 600; }}
QLabel#MenuStatus, QLabel#MenuVersion {{ color: {t.muted.name()}; }}
QLabel#MenuSection {{ color: {soft}; font-size: 11px; font-weight: 600; padding: 6px 14px 1px 14px; }}

QDialog, QMessageBox {{ background: {t.window.name()}; }}
QLabel#HeaderTitle {{ font-size: 17px; font-weight: 600; }}
QLabel#HeaderSubtitle, QLabel#Hint {{ color: {t.muted.name()}; }}

QGroupBox {{ background: {t.surface.name()}; border: 1px solid {t.border.name()}; border-radius: {RADIUS + 2}px;
            margin-top: 17px; padding: 6px 10px 8px 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 6px; padding: 0 4px;
                   color: {soft}; font-weight: 600; }}
QGroupBox::indicator {{ width: 15px; height: 15px; }}
QGroupBox:flat {{ background: transparent; border-color: transparent; }}

QLineEdit, QComboBox, QPlainTextEdit, QListWidget, QTextEdit {{
    background: {t.window.name() if t.dark else t.raised.name()}; color: {t.text.name()};
    border: 1px solid {t.border.name()}; border-radius: {RADIUS - 2}px; padding: 4px 8px;
    selection-background-color: {a}; selection-color: {t.on_accent.name()}; }}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QListWidget:focus, QTextEdit:focus {{
    border: 1px solid {a}; }}
QLineEdit:disabled, QComboBox:disabled {{ color: {t.muted.name()}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {t.surface.name()}; border: 1px solid {t.border.name()};
                              border-radius: 8px; padding: 4px; selection-background-color: {hover};
                              selection-color: {t.text.name()}; }}

QPushButton {{ background: {t.raised.name()}; color: {t.text.name()}; border: 1px solid {t.border.name()};
              border-radius: {RADIUS - 2}px; padding: 5px 16px; }}
QPushButton:hover {{ border-color: {a}; }}
QPushButton:pressed {{ background: {hover}; }}
QPushButton:disabled {{ color: {t.muted.name()}; }}
QPushButton[primary="true"] {{ background: {a}; color: {t.on_accent.name()}; border: 1px solid {a};
                              font-weight: 600; }}
QPushButton[primary="true"]:hover {{ background: {t.accent.lighter(112).name()}; }}
QPushButton[primary="true"]:pressed {{ background: {t.accent.darker(110).name()}; }}

QCheckBox, QRadioButton {{ spacing: 8px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t.border.name()}; border-radius: 4px; min-height: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
"""


def apply(app: QApplication, skin: Skin | None = None, dark: bool | None = None) -> Theme:
    """Style the whole app from this skin (default: the one in use) and the OS's light/dark mode."""
    global _current
    skin = skin if skin is not None else current().skin
    _current = make_theme(skin, os_is_dark() if dark is None else dark)
    app.setStyle("Fusion")
    app.setPalette(palette(_current))
    app.setStyleSheet(stylesheet(_current))
    return _current


def follow_os(app: QApplication) -> None:
    """Re-apply the theme when the OS switches between light and dark."""
    hints = QGuiApplication.styleHints()
    if hasattr(hints, "colorSchemeChanged"):
        hints.colorSchemeChanged.connect(lambda _scheme: apply(app))


# --- pieces --------------------------------------------------------------------------------


class RoundMenu(QMenu):
    """A menu with rounded corners (the window behind the corners is transparent)."""

    def __init__(self, *args):
        super().__init__(*args)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setToolTipsVisible(True)

    def addMenu(self, *args):
        if len(args) == 1 and isinstance(args[0], str):
            sub = RoundMenu(args[0], self)
            super().addMenu(sub)
            return sub
        return super().addMenu(*args)


def face(state: str = "idle", size: int = HEADER_ICON, skin: Skin | None = None) -> QLabel:
    """The mascot's face as a label."""
    skin = skin or current().skin
    label = QLabel()
    icon = skin.icons.get(state)
    if icon is not None and not icon.isNull():
        label.setPixmap(icon.pixmap(QSize(size, size)))
    label.setFixedSize(size, size)
    return label


def menu_header(menu: QMenu, title: str, status: str, state: str = "idle") -> QWidgetAction:
    """The menu's first row: the mascot in its current state, the app's name and what it's doing.
    The action's text is the title, so code that reads the menu's labels still finds it."""
    box = QWidget()
    box.setObjectName("MenuHeader")
    row = QHBoxLayout(box)
    row.setContentsMargins(8, 8, 14, 8)
    row.setSpacing(10)
    row.addWidget(face(state, MENU_ICON))
    text = QVBoxLayout()
    text.setSpacing(1)
    name, _, version = title.rpartition(" ")
    top = QHBoxLayout()
    top.setSpacing(6)
    title_label = QLabel(name or title)
    title_label.setObjectName("MenuTitle")
    version_label = QLabel(version if name else "")
    version_label.setObjectName("MenuVersion")
    top.addWidget(title_label)
    top.addWidget(version_label)
    top.addStretch(1)
    status_label = QLabel(status)
    status_label.setObjectName("MenuStatus")
    text.addLayout(top)
    text.addWidget(status_label)
    row.addLayout(text, 1)
    action = QWidgetAction(menu)
    action.setDefaultWidget(box)
    action.setText(title)
    menu.addAction(action)
    return action


def menu_section(menu: QMenu, title: str) -> QWidgetAction:
    """A small colored heading over a group of menu items."""
    label = QLabel(title.upper())
    label.setObjectName("MenuSection")
    action = QWidgetAction(menu)
    action.setDefaultWidget(label)
    action.setText(title)
    action.setEnabled(False)
    menu.addAction(action)
    return action


def header(title: str, subtitle: str = "", state: str = "idle") -> QWidget:
    """The top of a dialog: the mascot's face, a title and a short line under it."""
    box = QWidget()
    row = QHBoxLayout(box)
    row.setContentsMargins(0, 0, 0, 2)
    row.setSpacing(12)
    row.addWidget(face(state))
    text = QVBoxLayout()
    text.setSpacing(2)
    title_label = QLabel(title)
    title_label.setObjectName("HeaderTitle")
    text.addWidget(title_label)
    if subtitle:
        sub = QLabel(subtitle)
        sub.setObjectName("HeaderSubtitle")
        sub.setWordWrap(True)
        text.addWidget(sub)
    row.addLayout(text, 1)
    return box


def card(title: str = "") -> QGroupBox:
    return QGroupBox(title)


def fold(box: QGroupBox, content: QWidget) -> None:
    """A tickable card that shows only its title while unticked."""
    def show(on: bool) -> None:
        content.setVisible(on)
        box.setFlat(not on)
        if box.window() is not None:
            box.window().adjustSize()
    show(box.isChecked())
    box.toggled.connect(show)


def hint(text: str) -> QLabel:
    """A muted, wrapping line of explanation."""
    label = QLabel(text)
    label.setObjectName("Hint")
    label.setWordWrap(True)
    return label


def primary(button: QPushButton) -> QPushButton:
    button.setProperty("primary", True)
    button.style().unpolish(button)
    button.style().polish(button)
    return button
