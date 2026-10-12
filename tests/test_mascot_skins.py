"""Mascot skins, the glow, blinking and the hit region, headless. What only a person can judge
(how it looks over real windows, clicks passing through around the sprite, Retina) is in the
README's test steps."""

import json
import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, QSize, Qt, QUrl  # noqa: E402
from PySide6.QtGui import QDropEvent, QEnterEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from src.app import assets  # noqa: E402
from src.app import mascot as mascot_module  # noqa: E402
from src.app.main import CompanionApp, make_application  # noqa: E402
from src.app.mascot import EDGE, PAD, SPRITE, WINDOW, MascotWindow, badge_rect, glow_layers  # noqa: E402
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

def test_the_skins(app):
    skins = assets.available_skins()
    assert skins[0] == "avatar" and {"inspector", "cloud", "robot"} <= set(skins)
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
    assert assets.load_skin("no-such-skin").key == "avatar"
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
    assert m.skin.key == "avatar"
    m.set_skin("cloud")
    assert m.skin.key == "cloud" and Settings(settings.path).get("skin") == "cloud"
    assert MascotWindow(Settings(settings.path)).skin.key == "cloud"


def test_skin_menu_lists_the_skins(app, settings, monkeypatch):
    c = CompanionApp(app, settings)
    menu = c.build_menu()
    skin_menu = next(a.menu() for a in menu.actions() if a.text() == "Skin")
    labels = [a.text() for a in skin_menu.actions() if a.text()]
    assert labels[:4] == ["Avatar", "Cloud", "Inspector", "Robot"] and "Blink while idle" in labels
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
    avatar = m.hit_region()
    m.set_skin("cloud")
    assert m.hit_region() != avatar
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

def test_avatar_skin_is_a_circle_with_its_glow_in_skin_json(app):
    skin = assets.load_skin("avatar")
    meta = json.loads((assets.mascot_dir() / "avatar" / "skin.json").read_text(encoding="utf-8"))
    assert skin.shape == "circle" and skin.eyes == "frames" and set(skin.blink_icons) == set(assets.STATES)
    assert meta["glow"]["working"]["period_ms"] == 1400 and meta["glow"]["done"]["return_ms"] == 3000
    for state in assets.STATES:
        assert skin.glow[state]["color"] == meta["glow"][state]["color"]


def test_avatar_image_is_masked_to_a_circle_when_loaded(app):
    skin = assets.load_skin("avatar")
    for size in (120, 240):
        image = skin.icons["idle"].pixmap(QSize(size, size), 1.0).toImage()
        assert image.width() == size
        for x, y in ((1, 1), (size - 2, 1), (1, size - 2), (size - 2, size - 2)):
            assert image.pixelColor(x, y).alpha() == 0                          # the square's corners are gone
        assert image.pixelColor(size // 2, size // 2).alpha() == 255
        assert image.pixelColor(size // 2, 2).alpha() > 200                    # the circle reaches the edge
    raw = assets.load_icon(assets.mascot_dir() / "avatar" / "idle.png").pixmap(QSize(120, 120), 1.0).toImage()
    assert raw.pixelColor(1, 1).alpha() > 0                                     # the file itself is untouched


def test_circle_hit_area_is_only_the_circle(app, settings):
    m = MascotWindow(settings)                                                  # avatar: a circle
    c = WINDOW / 2
    assert m.hit(QPoint(round(c + EDGE - 3), round(c))) and m.hit(QPoint(round(c), round(c - EDGE + 3)))
    assert not m.hit(QPoint(round(c + EDGE + 4), round(c)))                     # the halo: not the mascot
    assert not m.hit(QPoint(round(c + 0.72 * EDGE), round(c + 0.72 * EDGE)))   # outside, toward a corner
    rect = badge_rect(5)
    m.set_state("done", 5)
    assert m.hit(QPoint(rect.right() - 2, rect.top() + 2))                     # the badge still counts


def test_skin_json_glow_values_are_checked(tmp_path):
    path = tmp_path / "skin.json"
    path.write_text(json.dumps({"shape": "square", "glow": {
        "width": 10, "edge_opacity": 3, "idle": {"color": "pink", "strength": 0.2},
        "working": {"scale": [1.2, 1.0], "period_ms": 2000}, "sparkle": 1}}), encoding="utf-8")
    data = assets.read_skin_json(path)
    glow = data["glow"]
    assert data["shape"] == "free"                                               # unknown shape: the default
    assert glow["width"] == 10 and glow["edge_opacity"] == assets.DEFAULT_GLOW["edge_opacity"]
    assert glow["idle"] == {"color": assets.DEFAULT_GLOW["idle"]["color"], "strength": 0.2}
    assert glow["working"]["scale"] == assets.DEFAULT_GLOW["working"]["scale"] and glow["working"]["period_ms"] == 2000
    assert "sparkle" not in glow


def test_glow_timeline():
    glow = assets.DEFAULT_GLOW
    idle, = glow_layers("idle", 0.0, glow)
    assert idle == ("idle", glow["idle"]["strength"], 1.0, 1.0)
    low, high = glow["working"]["strength"]
    assert glow_layers("working", 0.0, glow)[0][1:3] == pytest.approx((low, 1.0))
    assert glow_layers("working", 0.5, glow)[0][1:3] == pytest.approx((high, glow["working"]["scale"][1]))
    assert glow_layers("working", 1.0, glow)[0][1] == pytest.approx(low)         # seamless loop
    flash = glow["done"]["flash_ms"] / glow["done"]["return_ms"]
    peak, = glow_layers("done", flash / 2, glow)
    assert peak[0] == "done" and peak[1] == pytest.approx(1.0)                   # the bright pulse
    middle = glow_layers("done", 0.8, glow)
    assert [layer[0] for layer in middle] == ["done", "idle"]                     # crossfading back
    assert glow_layers("done", 1.0, glow) == [idle]                              # and settled on idle
    assert glow_layers("error", 0.3, glow) == [("error", glow["error"]["strength"], 1.0, 1.0)]


def test_glow_per_state(app, settings):
    m = MascotWindow(settings)
    m.skin.glow["done"].update(flash_ms=60, return_ms=300)
    assert m.glow.layers()[0][0] == "idle" and not m.glow.animating             # idle: a soft glow, static
    m.set_state("working")
    assert m.glow.animating and m.glow.animation.duration() == 1400 and m.glow.animation.loopCount() == -1
    m.set_state("error")
    assert not m.glow.animating and m.glow.layers()[0][0] == "error"
    m.set_state("done", 3)
    assert m.glow.animating and m.glow.layers()[0][0] == "done"
    pump(0.5)
    assert not m.glow.animating and m.glow.layers() == glow_layers("idle", 0, m.skin.glow)   # back to idle
    assert m.count == 3                                                          # the badge stays


def test_working_glow_moves_opacity_and_scale(app, settings):
    m = MascotWindow(settings)
    m.set_state("working")
    scales, opacities = set(), set()
    end = time.monotonic() + 1.0
    while time.monotonic() < end:
        QApplication.processEvents()
        _, opacity, scale, _ = m.glow.layers()[0]
        scales.add(round(scale, 3))
        opacities.add(round(opacity, 2))
        time.sleep(0.02)
    assert len(scales) > 3 and len(opacities) > 3


def test_halo_is_rendered_once_per_color(app, settings):
    m = MascotWindow(settings)
    first = m.glow.halo("working", 1.0)
    assert m.glow.halo("working", 1.0) is first                                  # cached
    assert m.glow.halo("working", 2.0) is not first                              # Retina: its own copy
    assert m.glow.halo("working", 2.0).width() == WINDOW * 2
    m.set_skin("cloud")
    assert m.glow.halo("working", 1.0) is not first                              # a new skin, new colors


def test_halo_is_never_clipped(app, settings):
    m = MascotWindow(settings)
    m.set_state("working")
    m.glow.animation.stop()
    m.glow.t = 0.5                                                               # the widest moment
    image = m.glow.grab().toImage()
    edge = [image.pixelColor(i, j).alpha() for i in range(image.width()) for j in (0, image.height() - 1)]
    edge += [image.pixelColor(i, j).alpha() for j in range(image.height()) for i in (0, image.width() - 1)]
    assert max(edge) == 0                                                        # fades out before the window edge
    c = image.width() // 2
    assert image.pixelColor(c + round(EDGE) + 4, c).alpha() > 60                 # but is there outside the circle


def test_ring_on_the_border_at_full_strength(app, settings):
    m = MascotWindow(settings)
    image = m.glow.grab().toImage()
    c = image.width() // 2
    ring = image.pixelColor(c + round(EDGE), c)
    assert ring.alpha() > 200                                                    # even at the low idle strength
    m.set_skin("robot")
    assert m.glow.grab().toImage().pixelColor(c + round(EDGE), c).alpha() < ring.alpha()   # no ring: free shape


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


def test_no_glow_repaints_when_idle_error_or_done(app, settings):
    m = MascotWindow(settings)
    m.skin.glow["done"].update(flash_ms=40, return_ms=150)
    m.show()
    count = {"n": 0}
    original = m.glow.paintEvent

    def counting(event):
        count["n"] += 1
        original(event)
    m.glow.paintEvent = counting
    for state in ("idle", "error", "done"):
        m.set_state(state, 2 if state == "done" else None)
        pump(0.3)                                                                # the done pulse plays out
        count["n"] = 0
        pump(0.4)
        assert count["n"] == 0 and not m.glow.animating, state
    m.hide()
