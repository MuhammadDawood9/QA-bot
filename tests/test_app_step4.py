"""Step 4: the working animation, start at login (Windows registry, macOS LaunchAgent), and how a
packaged app finds and installs its editable files. The packaged build itself is checked with
`VideoQCBot --selftest` (packaging/README.md)."""

import os
import plistlib
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from src import config as config_module  # noqa: E402
from src.app import main as app_main  # noqa: E402
from src.app.mascot import MascotWindow  # noqa: E402
from src.app.settings import Settings  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return app_main.make_application(["test"])


# --- animation -------------------------------------------------------------------------------

def test_glow_pulses_only_while_working(app, tmp_path):
    mascot = MascotWindow(Settings(tmp_path / "s.json"))
    assert not mascot.glow.animating
    mascot.set_state("working")
    assert mascot.glow.animating
    seen = set()
    end = time.monotonic() + 1.0
    while time.monotonic() < end:
        QApplication.processEvents()
        seen.add(round(mascot.glow.layers()[0][1], 2))
        time.sleep(0.02)
    low, high = mascot.skin.glow["working"]["strength"]
    assert len(seen) > 5 and low - 0.01 <= min(seen) and max(seen) <= high + 0.01   # it breathes, within bounds
    mascot.set_state("idle")
    assert not mascot.glow.animating                                       # no CPU when not working


# --- start at login --------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")
def test_windows_start_at_login_uses_the_run_key():
    import winreg

    from src.platform import windows

    test_key = r"Software\VideoQCBotTest\Run"                  # never the real Run key in a test
    try:
        assert not windows.launch_at_login_enabled(test_key)
        windows.set_launch_at_login(True, [r"C:\Program Files\Video QC Bot\VideoQCBot.exe"], test_key)
        assert windows.launch_at_login_enabled(test_key)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, test_key) as key:
            value, _ = winreg.QueryValueEx(key, windows.RUN_VALUE)
        assert value == '"C:\\Program Files\\Video QC Bot\\VideoQCBot.exe"'      # quoted: the path has spaces
        windows.set_launch_at_login(False, [], test_key)
        assert not windows.launch_at_login_enabled(test_key)
        windows.set_launch_at_login(False, [], test_key)                         # off twice is fine
    finally:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, test_key)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\VideoQCBotTest")


def test_macos_start_at_login_writes_a_launch_agent(tmp_path):
    from src.platform import macos

    agents = tmp_path / "LaunchAgents"
    command = ["/Applications/Video QC Bot.app/Contents/MacOS/VideoQCBot"]
    assert not macos.launch_at_login_enabled(agents)
    macos.set_launch_at_login(True, command, agents)
    plist = plistlib.loads((agents / "com.videoqcbot.companion.plist").read_bytes())
    assert plist["Label"] == "com.videoqcbot.companion" and plist["ProgramArguments"] == command
    assert plist["RunAtLoad"] is True and plist["LimitLoadToSessionType"] == "Aqua"
    assert macos.launch_at_login_enabled(agents)
    macos.set_launch_at_login(False, command, agents)
    assert not macos.launch_at_login_enabled(agents)


def test_every_platform_module_has_start_at_login():
    from src.platform import macos, other, windows

    for module in (windows, macos, other):
        assert callable(module.launch_at_login_enabled) and callable(module.set_launch_at_login)
    with pytest.raises(OSError):
        other.set_launch_at_login(True, ["x"])


def test_launch_command_from_source_runs_the_launcher():
    command = app_main.launch_command()
    assert command[-1].endswith("run_app.pyw") and Path(command[-1]).is_file()
    if sys.platform == "win32":
        assert command[0].lower().endswith("pythonw.exe")             # no console window at login


def test_launch_command_when_packaged(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/Applications/Video QC Bot.app/Contents/MacOS/VideoQCBot")
    assert app_main.launch_command() == ["/Applications/Video QC Bot.app/Contents/MacOS/VideoQCBot"]


# --- packaged app: editable files ------------------------------------------------------------

def test_editable_files_are_copied_once_and_never_overwritten(tmp_path):
    bundle, target = tmp_path / "bundle", tmp_path / "Données utilisateur"
    (bundle / "clients").mkdir(parents=True)
    (bundle / "config.yaml").write_text("paths: {}\n", encoding="utf-8")
    (bundle / "clients" / "E8D.yaml").write_text("client: E8D\n", encoding="utf-8")
    (bundle / "glossary.txt").write_text("=MAIER\n", encoding="utf-8")
    copied = config_module.install_user_files(bundle, target)
    assert set(copied) == {"config.yaml", "clients", "glossary.txt"}
    (target / "glossary.txt").write_text("=Maier\n=McGwire\n", encoding="utf-8")       # the user's edit
    assert config_module.install_user_files(bundle, target) == []
    assert (target / "glossary.txt").read_text(encoding="utf-8") == "=Maier\n=McGwire\n"


def test_packaged_app_reads_config_from_the_data_folder(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "config.yaml").write_bytes((Path(config_module.PROJECT_ROOT) / "config.yaml").read_bytes())
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(config_module, "user_data_root", lambda: tmp_path / "data")
    loaded = config_module.load_config()
    assert Path(loaded["_root"]) == tmp_path / "data"
    assert config_module.resolve_path(loaded, "ocr_cache_dir") == tmp_path / "data" / "cache" / "ocr"


def test_source_runs_keep_using_the_project_folder():
    assert not config_module.is_frozen()
    assert config_module.default_config_path() == config_module.DEFAULT_CONFIG
