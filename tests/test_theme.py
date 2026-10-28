"""The app's look taken from the mascot skin, headless: colors, the badge, the menu's header and
sections, and the folding podcast card. How it looks on a real screen is checked by eye."""

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtGui import QColor, QPalette  # noqa: E402
from PySide6.QtWidgets import QGroupBox, QPushButton, QWidget, QWidgetAction  # noqa: E402

from src.app import assets, theme  # noqa: E402
from src.app.main import CompanionApp, make_application  # noqa: E402
from src.app.settings import Settings  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return make_application(["test"])


def write_skin(base: Path, key: str, data: dict) -> None:
    folder = base / key
    folder.mkdir(parents=True)
    (folder / "skin.json").write_text(json.dumps(data), encoding="utf-8")


def test_theme_colors_come_from_the_skin_glow(app, tmp_path):
    write_skin(tmp_path, "mint", {"name": "Mint", "glow": {"working": {"color": "#20B090"},
                                                          "idle": {"color": "#A0E0D0"}}})
    skin = assets.load_skin("mint", tmp_path)
    for dark in (True, False):
        t = theme.make_theme(skin, dark)
        assert t.accent == QColor("#20B090") and t.soft == QColor("#A0E0D0")
        assert t.good == QColor(assets.DEFAULT_GLOW["done"]["color"])        # not set: the default
        assert t.window.hslHue() == t.accent.hslHue()                         # greys tinted toward the accent
        assert (t.window.lightnessF() < 0.5) == dark
        assert (t.text.lightnessF() > 0.5) == dark


def test_apply_styles_the_app_and_marks_primary_buttons(app):
    t = theme.apply(app, assets.load_skin(), dark=True)
    assert app.palette().color(QPalette.ColorRole.Highlight) == t.accent
    assert t.accent.name() in app.styleSheet()
    button = theme.primary(QPushButton("Save"))
    assert button.property("primary") is True
    theme.apply(app, dark=False)                                              # same skin, light
    assert theme.current().skin.key == t.skin.key and not theme.current().dark


def test_badge_is_the_working_color_unless_the_skin_sets_one(tmp_path):
    write_skin(tmp_path, "plain", {"name": "Plain", "glow": {"working": {"color": "#112233"}}})
    write_skin(tmp_path, "red", {"name": "Red", "badge": "#E53935"})
    write_skin(tmp_path, "bad", {"name": "Bad", "badge": "red"})
    assert assets.load_skin("plain", tmp_path).badge == "#112233"
    assert assets.load_skin("red", tmp_path).badge == "#E53935"
    assert assets.load_skin("bad", tmp_path).badge == assets.DEFAULT_GLOW["working"]["color"]   # invalid: ignored


def test_menu_has_a_header_and_sections_and_is_rounded(app, tmp_path):
    companion = CompanionApp(app, Settings(tmp_path / "settings.json"))
    try:
        menu = companion.build_menu()
        assert isinstance(menu, theme.RoundMenu)
        actions = menu.actions()
        assert isinstance(actions[0], QWidgetAction)                          # the mascot, name and status
        assert actions[0].text().startswith("Video QC Bot ")
        labels = [a.text() for a in actions]
        assert ["Check", "Setup", "Reports and lists", "App"] == [a.text() for a in actions
                                                                  if isinstance(a, QWidgetAction)][1:]
        assert labels.index("Check") < labels.index("Check a video...") < labels.index("Setup")
        clients = next(a.menu() for a in actions if a.text() == "Client")
        assert isinstance(clients, theme.RoundMenu)                           # submenus are rounded too
    finally:
        if companion.tray:
            companion.tray.hide()
        companion.mascot.glow.close()
        companion.mascot.close()


def test_changing_the_skin_recolors_the_app(app, tmp_path, monkeypatch):
    companion = CompanionApp(app, Settings(tmp_path / "settings.json"))
    try:
        seen = []
        monkeypatch.setattr(theme, "apply", lambda a, skin=None, dark=None: seen.append(skin.key))
        key = assets.available_skins()[-1]
        companion.set_skin(key)
        assert companion.mascot.skin.key == key and seen == [key]
    finally:
        if companion.tray:
            companion.tray.hide()
        companion.mascot.glow.close()
        companion.mascot.close()


def test_a_folded_card_shows_only_its_title(app):
    box = QGroupBox("Podcast audio guidelines")
    box.setCheckable(True)
    box.setChecked(False)
    content = QWidget(box)
    theme.fold(box, content)
    assert box.isFlat() and content.isHidden()
    box.setChecked(True)
    assert not box.isFlat() and not content.isHidden()
