"""The desktop app's Step 2 pieces, run headless (Qt's offscreen platform): settings, images,
mascot states, dragging and remembering the position, and the per-OS platform modules.
Things only a person can check (real tray, dragging with a mouse, Retina sharpness, the Dock)
are in the README's test steps."""

import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from src.app import assets  # noqa: E402
from src.app.main import make_application  # noqa: E402
from src.app.mascot import SIZE, MascotWindow, badge_text  # noqa: E402
from src.app.settings import Settings  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return make_application(["test"])


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_path / "Réglages QC" / "settings.json")      # spaces and accents in the path


def mouse(widget, kind, global_pos: QPoint, buttons):
    local = QPointF(widget.mapFromGlobal(global_pos))
    button = Qt.MouseButton.LeftButton if kind != QEvent.Type.MouseMove else Qt.MouseButton.NoButton
    event = QMouseEvent(kind, local, QPointF(global_pos), button, buttons, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget, event)


# --- settings --------------------------------------------------------------------------------

def test_settings_round_trip_in_a_folder_with_spaces_and_accents(settings):
    assert settings.window_position is None
    settings.window_position = (321, 654)
    settings.save()
    assert json.loads(settings.path.read_text(encoding="utf-8"))["window"] == {"x": 321, "y": 654}
    assert Settings(settings.path).window_position == (321, 654)


def test_damaged_settings_file_means_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    assert Settings(path).window_position is None


def test_settings_live_in_the_os_config_folder():
    from src.app.settings import config_dir

    folder = str(config_dir())
    assert "VideoQCBot" in folder and "Video-qc-bot" not in folder        # not in the project


# --- images ----------------------------------------------------------------------------------

def test_every_state_has_a_1x_and_2x_image(app):
    folder = assets.assets_dir() / "mascot"
    for state in assets.STATES:
        assert (folder / f"{state}.png").is_file() and (folder / f"{state}@2x.png").is_file()
        sizes = {(s.width(), s.height()) for s in assets.load_icon(folder / f"{state}.png").availableSizes()}
        assert {(120, 120), (240, 240)} <= sizes


def test_missing_image_falls_back_to_a_drawn_circle(app, settings, tmp_path):
    icons = assets.mascot_icons(tmp_path / "no images here")
    assert all(icon.isNull() for icon in icons.values())
    window = MascotWindow(settings, icons)
    assert not window.grab().toImage().isNull()                         # paints without crashing


# --- the mascot ------------------------------------------------------------------------------

def test_window_is_small_frameless_transparent_and_on_top(app, settings):
    window = MascotWindow(settings)
    flags = window.windowFlags()
    assert window.size().width() == SIZE == window.size().height()
    assert flags & Qt.WindowType.FramelessWindowHint and flags & Qt.WindowType.WindowStaysOnTopHint
    assert flags & Qt.WindowType.Tool                                   # no taskbar button / window list entry
    assert window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)


def test_states_and_badge(app, settings):
    window = MascotWindow(settings)
    seen = []
    window.state_changed.connect(seen.append)
    window.set_state("working")
    window.set_state("done", 12)
    assert (window.state, window.count) == ("done", 12) and window.toolTip() == "Done: 12 issues"
    window.set_state("error")
    assert window.count is None
    window.set_state("done", 1)
    assert window.toolTip() == "Done: 1 issue"
    assert seen == ["working", "done", "error", "done"]
    with pytest.raises(ValueError):
        window.set_state("sleeping")
    assert badge_text(7) == "7" and badge_text(99) == "99" and badge_text(150) == "99+"


def test_drag_moves_the_window_and_saves_the_position(app, settings):
    window = MascotWindow(settings)
    window.move(200, 200)
    window.show()
    start = window.mapToGlobal(QPoint(60, 60))
    mouse(window, QEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton)
    mouse(window, QEvent.Type.MouseMove, start + QPoint(40, 25), Qt.MouseButton.LeftButton)
    mouse(window, QEvent.Type.MouseButtonRelease, start + QPoint(40, 25), Qt.MouseButton.NoButton)
    assert window.pos() == QPoint(240, 225)
    assert Settings(settings.path).window_position == (240, 225)
    window.hide()


def test_a_click_without_moving_is_not_a_drag(app, settings):
    window = MascotWindow(settings)
    window.move(200, 200)
    window.show()
    start = window.mapToGlobal(QPoint(60, 60))
    mouse(window, QEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton)
    mouse(window, QEvent.Type.MouseMove, start + QPoint(1, 1), Qt.MouseButton.LeftButton)
    mouse(window, QEvent.Type.MouseButtonRelease, start + QPoint(1, 1), Qt.MouseButton.NoButton)
    assert window.pos() == QPoint(200, 200) and not settings.path.exists()
    window.hide()


def test_saved_position_is_used_but_never_off_screen(app, settings):
    settings.window_position = (150, 160)
    assert MascotWindow(settings).pos() == QPoint(150, 160)
    settings.window_position = (50_000, 50_000)                         # the monitor it was on is gone
    window = MascotWindow(settings)
    assert window.on_a_screen(window.pos()) and window.pos() != QPoint(50_000, 50_000)


# --- app and platform ------------------------------------------------------------------------

def test_app_keeps_running_without_windows(app):
    assert app.quitOnLastWindowClosed() is False


def test_platform_module_matches_the_os():
    from src.platform import current

    expected = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "other")
    assert current.name == expected
    for name in ("floating_window_flags", "prepare_floating_window", "after_app_created", "tray_icon"):
        assert callable(getattr(current, name))


def test_every_platform_module_has_the_same_interface():
    from src.platform import macos, other, windows

    for module in (windows, macos, other):
        for name in ("floating_window_flags", "prepare_floating_window", "after_app_created", "tray_icon"):
            assert callable(getattr(module, name)), (module.__name__, name)


def test_tray_icons_exist_for_both_systems(app):
    from src.platform import macos, windows

    win = windows.tray_icon(assets.assets_dir())
    mac = macos.tray_icon(assets.assets_dir())
    assert not win.isNull() and not mac.isNull()
    assert mac.isMask() and not win.isMask()                           # macOS: a template image
