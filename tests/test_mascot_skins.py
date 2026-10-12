"""Mascot skins, the glow, blinking and the hit region, headless. What only a person can judge
(how it looks over real windows, clicks passing through around the sprite, Retina) is in the
README's test steps."""

import json
import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, Qt, QUrl  # noqa: E402
from PySide6.QtGui import QDropEvent, QEnterEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from src.app import assets  # noqa: E402
from src.app import mascot as mascot_module  # noqa: E402
from src.app.main import CompanionApp, make_application  # noqa: E402
from src.app.mascot import PAD, SPRITE, WINDOW, MascotWindow, badge_rect  # noqa: E402
from src.app.settings import Settings  # noqa: E402

CENTER = QPoint(WINDOW // 2, WINDOW // 2)


@pytest.fixture(scope="module")
def app():
    return make_application(["test"])


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_path / "settings.json")


def pump(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.005)


def enter(widget):
    QApplication.sendEvent(widget, QEnterEvent(QPointF(CENTER), QPointF(CENTER), QPointF(widget.mapToGlobal(CENTER))))


def leave(widget):
    QApplication.sendEvent(widget, QEvent(QEvent.Type.Leave))


# --- skins -----------------------------------------------------------------------------------

def test_the_three_placeholder_skins(app):
    skins = assets.available_skins()
    assert skins[0] == "inspector" and {"inspector", "cloud", "robot"} <= set(skins)
    inspector, cloud = assets.load_skin("inspector"), assets.load_skin("cloud")
    assert inspector.eyes == "frames" and set(inspector.blink_icons) == set(assets.STATES)
    assert cloud.eyes == "drawn" and cloud.blink_icons == {} and cloud.eye_shape["left"] == [46, 64]
    assert all(cloud.can_blink(s) for s in assets.STATES)


def test_skin_json_values_are_checked(tmp_path):
    folder = tmp_path / "odd"
    folder.mkdir()
    (folder / "skin.json").write_text(json.dumps({
        "name": "Odd", "eyes": "painted", "eye_shape": {"left": [1, 2, 3], "radius": -4, "color": "#FF0000"},
        "state_eyes": {"idle": "winking", "done": "cross", "sleeping": "open"}}), encoding="utf-8")
    data = assets.read_skin_json(folder / "skin.json")
    assert data["eyes"] == "frames"                                      # unknown value: the default
    assert data["eye_shape"]["left"] == assets.DEFAULT_EYE_SHAPE["left"]
    assert data["eye_shape"]["radius"] == assets.DEFAULT_EYE_SHAPE["radius"] and data["eye_shape"]["color"] == "#FF0000"
    assert data["state_eyes"]["idle"] == "open" and data["state_eyes"]["done"] == "cross"


def test_missing_or_broken_skin_falls_back(app, tmp_path):
    assert assets.load_skin("no-such-skin").key == "inspector"
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "skin.json").write_text("{nope", encoding="utf-8")
    skin = assets.load_skin("broken", base=tmp_path)
    assert skin.key == "broken" and skin.eyes == "frames" and all(i.isNull() for i in skin.icons.values())


def test_a_frames_skin_without_a_blink_frame_doesnt_blink_in_that_state(app, tmp_path):
    folder = tmp_path / "partial"
    folder.mkdir()
    src = assets.mascot_dir() / "inspector"
    for name in ("idle.png", "working.png", "done.png", "error.png", "idle_blink.png", "skin.json"):
        (folder / name).write_bytes((src / name).read_bytes())
    skin = assets.load_skin("partial", base=tmp_path)
    assert skin.can_blink("idle") and not skin.can_blink("working")


def test_switching_skins_is_remembered(app, settings):
    m = MascotWindow(settings)
    assert m.skin.key == "inspector"
    m.set_skin("cloud")
    assert m.skin.key == "cloud" and Settings(settings.path).get("skin") == "cloud"
    assert MascotWindow(Settings(settings.path)).skin.key == "cloud"


def test_skin_menu_lists_the_skins(app, settings, monkeypatch):
    c = CompanionApp(app, settings)
    menu = c.build_menu()
    skin_menu = next(a.menu() for a in menu.actions() if a.text() == "Skin")
    labels = [a.text() for a in skin_menu.actions() if a.text()]
    assert labels[:3] == ["Inspector", "Cloud", "Robot"] and "Blink while idle" in labels
    next(a for a in skin_menu.actions() if a.text() == "Cloud").trigger()
    assert c.mascot.skin.key == "cloud"
    c.mascot.close()
    if c.tray is not None:
        c.tray.hide()


# --- window, padding and hit region -----------------------------------------------------------

def test_sprite_sits_in_a_padded_window_so_the_glow_is_never_clipped(app, settings):
    m = MascotWindow(settings)
    assert WINDOW == SPRITE + 2 * PAD and PAD >= 20
    assert m.size().width() == WINDOW == m.glow.size().width()
    assert m.glow.windowFlags() & Qt.WindowType.WindowTransparentForInput   # the glow never takes the mouse
    assert m.glow.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)


def test_hit_region_is_the_sprite_shape(app, settings):
    m = MascotWindow(settings)
    region = m.hit_region()
    assert m.hit(CENTER)                                                    # the face
    for corner in (QPoint(2, 2), QPoint(WINDOW - 3, 2), QPoint(2, WINDOW - 3), QPoint(WINDOW - 3, WINDOW - 3)):
        assert not m.hit(corner)                                            # the glow padding
    assert not m.hit(QPoint(PAD + 3, PAD + SPRITE - 3))                     # a transparent corner of the sprite
    assert m.mask() == region and not region.isEmpty()                       # the window itself is shaped


def test_hit_region_follows_the_skin_and_the_badge(app, settings):
    m = MascotWindow(settings)
    inspector = m.hit_region()
    m.set_skin("cloud")
    assert m.hit_region() != inspector
    rect = badge_rect(12)
    corner = QPoint(rect.right() - 2, rect.top() + 2)
    assert not m.hit(corner)
    m.set_state("done", 12)
    assert m.hit(corner)                                                    # the badge can be clicked too
    m.set_state("idle")
    assert not m.hit(corner)


def drop_at(widget, point, paths):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    widget.dropEvent(QDropEvent(QPointF(point), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
                                Qt.KeyboardModifier.NoModifier))


def test_drops_count_only_on_the_sprite(app, settings, tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    m = MascotWindow(settings)
    got = []
    m.files_dropped.connect(got.append)
    drop_at(m, QPoint(3, 3), [video])                     # on the padding: not the mascot
    drop_at(m, CENTER, [video])
    assert got == [[video]]


# --- glow ------------------------------------------------------------------------------------

def test_glow_per_state(app, settings, monkeypatch):
    monkeypatch.setattr(mascot_module, "DONE_FADE_MS", 200)
    m = MascotWindow(settings)
    assert m.glow.glowOpacity == 0.0 and not m.glow.animating             # idle: none
    m.set_state("working")
    assert m.glow.animating and m.glow.color.name() == mascot_module.GLOW_COLORS["working"].name()
    m.set_state("error")
    assert not m.glow.animating and m.glow.glowOpacity == pytest.approx(0.75)
    assert m.glow.color.name() == mascot_module.GLOW_COLORS["error"].name()
    m.set_state("done", 3)
    assert m.glow.animating and m.glow.color.name() == mascot_module.GLOW_COLORS["done"].name()
    pump(0.5)
    assert not m.glow.animating and m.glow.glowOpacity == pytest.approx(0.0, abs=0.01)   # faded out


def test_working_glow_moves_radius_and_opacity(app, settings):
    m = MascotWindow(settings)
    m.set_state("working")
    radii, opacities = set(), set()
    end = time.monotonic() + 1.0
    while time.monotonic() < end:
        QApplication.processEvents()
        radii.add(round(m.glow.glowRadius))
        opacities.add(round(m.glow.glowOpacity, 2))
        time.sleep(0.02)
    assert len(radii) > 3 and len(opacities) > 3
    assert max(radii) <= WINDOW / 2                                          # inside the padded window


def test_glow_window_follows_the_sprite(app, settings):
    m = MascotWindow(settings)
    m.show()
    m.move(300, 260)
    assert m.glow.pos() == QPoint(300, 260) and m.glow.isVisible()
    m.hide()
    assert not m.glow.isVisible()


# --- blinking --------------------------------------------------------------------------------

@pytest.fixture
def fast_blinks(monkeypatch):
    monkeypatch.setattr(mascot_module, "BLINK_MS", 40)
    monkeypatch.setattr(mascot_module, "HOVER_BLINK_EVERY_MS", 250)
    monkeypatch.setattr(mascot_module, "IDLE_BLINK_MS", (150, 200))


def test_hover_blinks_once_then_again_while_hovered(app, settings, fast_blinks):
    m = MascotWindow(settings)
    assert not m.blink_timer.isActive()                                       # idle and not hovered: no timer
    enter(m)
    assert m.eyes_closed                                                      # a blink right away
    pump(0.1)
    assert not m.eyes_closed and m.blink_timer.isActive()                     # reopened, next blink queued
    closes = 0
    end = time.monotonic() + 0.7
    was = False
    while time.monotonic() < end:
        QApplication.processEvents()
        if m.eyes_closed and not was:
            closes += 1
        was = m.eyes_closed
        time.sleep(0.005)
    assert closes >= 2                                                        # repeats every 250 ms here (3 s really)
    leave(m)
    pump(0.1)
    assert not m.eyes_closed and not m.blink_timer.isActive()                 # stops when the cursor leaves


def test_idle_blink_is_off_by_default_and_can_be_switched_on(app, settings, fast_blinks):
    m = MascotWindow(settings)
    assert settings.get("idle_blink") is False and not m.blink_timer.isActive()
    m.set_idle_blink(True)
    assert m.blink_timer.isActive() and 150 <= m.blink_timer.interval() <= 200
    closes = 0
    was = False
    end = time.monotonic() + 1.0
    while time.monotonic() < end:
        QApplication.processEvents()
        if m.eyes_closed and not was:
            closes += 1
        was = m.eyes_closed
        time.sleep(0.005)
    assert closes >= 3
    m.set_idle_blink(False)
    pump(0.1)
    assert not m.blink_timer.isActive() and Settings(settings.path).get("idle_blink") is False


def test_real_intervals(app):
    assert mascot_module.BLINK_MS == 120 and mascot_module.HOVER_BLINK_EVERY_MS == 3000
    assert mascot_module.IDLE_BLINK_MS == (5000, 10000)


def pixels(widget):
    return widget.grab().toImage()


def test_frames_skin_swaps_to_the_blink_image(app, settings):
    m = MascotWindow(settings)                                                # inspector: frames
    open_img = pixels(m)
    m.eyes_closed = True
    assert pixels(m) != open_img


def test_drawn_eyes_are_painted_and_close_for_a_blink(app, settings):
    m = MascotWindow(settings)
    m.set_skin("cloud")
    with_eyes = pixels(m)
    m.skin.eyes = "frames"                                                    # same body, no painted eyes
    without = pixels(m)
    m.skin.eyes = "drawn"
    assert with_eyes != without
    m.eyes_closed = True
    assert pixels(m) != with_eyes
    for state in ("working", "done", "error"):                                # every eye style paints
        m.eyes_closed = False
        m.set_state(state, 1 if state == "done" else None)
        assert not pixels(m).isNull()


def test_no_repaints_and_no_timers_when_idle(app, settings):
    m = MascotWindow(settings)
    m.show()
    count = {"n": 0}
    original = m.paintEvent

    def counting(event):
        count["n"] += 1
        original(event)
    m.paintEvent = counting
    pump(0.3)
    count["n"] = 0
    pump(0.5)
    assert count["n"] == 0 and not m.blink_timer.isActive() and not m.glow.animating
    m.hide()
