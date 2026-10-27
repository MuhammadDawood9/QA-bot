"""Phase 3: many PCs on the QC floor. The shared folder and its cached copy, safe shared writes,
roles, machine limits, versions, shared reports and diagnostics. The "network drive" here is a
temporary folder; a real SMB share and a synced OneDrive folder are in the manual test steps."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
import yaml  # noqa: E402

from src import shared  # noqa: E402
from src.clients.model import ClientError, Finding, build_client  # noqa: E402
from src.clients.store import ClientStore  # noqa: E402
from src.config import load_config, model_dirs, resolve_path, save_local_settings  # noqa: E402
from src.machine import JobLock, apply_machine_defaults, choose_whisper_model, resource_warnings  # noqa: E402
from src.shared import (DataLock, Floor, FloorSetup, LockBusy, SharedUnavailable, copy_report_to_shared,  # noqa: E402
                        hash_pin, mirror, read_setup, role_for, seed_shared, set_editor_pin, verify_pin,
                        write_shared_text)
from src.utils.safe_io import read_changelog  # noqa: E402
from src.version import __version__, check_for_update, parse_version  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def floor(tmp_path) -> Floor:
    """A shared folder set up from the app's files, and a user's local folder."""
    share = tmp_path / "share"
    seed_shared(share)
    f = Floor(FloorSetup(shared_data=share, shared_models=share / "models", shared_reports=share / "reports",
                         source="test"), local_dir=tmp_path / "local")
    shared.set_floor(f)
    assert f.sync()
    return f


# --- where the shared folder is ---------------------------------------------------------------------

def test_setup_precedence(tmp_path, monkeypatch):
    boot = tmp_path / "videoqcbot-bootstrap.yaml"
    mine = tmp_path / "floor.yaml"
    assert read_setup(bootstrap=None, user_file=mine).needs_setup
    boot.write_text("shared_data: '%QCSHARE%/VideoQCBot'\n", encoding="utf-8")
    monkeypatch.setenv("QCSHARE", str(tmp_path / "server"))
    s = read_setup(bootstrap=boot, user_file=mine)
    assert s.shared_data == tmp_path / "server" / "VideoQCBot" and s.shared_models == s.shared_data / "models"
    assert not s.needs_setup and "IT" in s.source
    shared.save_user_setup(tmp_path / "mine", user_file=mine)
    assert read_setup(bootstrap=boot, user_file=mine).shared_data == tmp_path / "mine"      # the user's choice wins
    boot.write_text(f"shared_data: {tmp_path / 'forced'}\nforce: true\n", encoding="utf-8")
    assert read_setup(bootstrap=boot, user_file=mine).shared_data == tmp_path / "forced"   # ...unless IT forces
    shared.save_user_setup(None, alone=True, user_file=mine)
    boot.unlink()
    alone = read_setup(bootstrap=None, user_file=mine)
    assert alone.alone and alone.shared_data is None and not alone.needs_setup


def test_seeding_fills_an_empty_folder_and_never_overwrites(tmp_path):
    share = tmp_path / "share"
    copied = seed_shared(share)
    assert {"config.yaml", "color.yaml", "clients", "shared_settings.yaml"} <= set(copied)
    (share / "glossary.txt").write_text("=Mine\n", encoding="utf-8")
    assert "glossary.txt" not in seed_shared(share)
    assert (share / "glossary.txt").read_text(encoding="utf-8") == "=Mine\n"
    assert not shared.looks_empty(share) and shared.looks_empty(tmp_path / "other")


# --- the cached copy ------------------------------------------------------------------------------------

def test_config_reads_the_cached_copy_and_keeps_caches_local(floor):
    config = load_config()
    assert Path(config["_root"]) == floor.cache_dir
    assert resolve_path(config, "clients_dir") == floor.cache_dir / "clients"
    assert resolve_path(config, "transcript_cache_dir") == floor.local_dir / "cache" / "transcripts"
    assert model_dirs(config) == [floor.shared_dir / "models", floor.local_dir / "models"]
    assert "E8D" in [p.stem for p in resolve_path(config, "clients_dir").glob("*.yaml")]


def test_edits_on_another_pc_arrive_without_a_restart(floor):
    share = floor.shared_dir
    (share / "config.yaml").write_text("checks:\n  too_short:\n    min_words: 3\n", encoding="utf-8")
    (share / "clients" / "E8C.yaml").unlink()
    (share / "clients" / "E9NEW.yaml").write_text("client: E9NEW\nname: New\n", encoding="utf-8")
    time.sleep(0.01)
    assert floor.sync()
    config = load_config()
    assert config["checks"]["too_short"]["min_words"] == 3                  # the shared value
    assert config["checks"]["spelling"]["enabled"] is True                  # the rest from the app's own config
    clients = {p.stem for p in resolve_path(config, "clients_dir").glob("*.yaml")}
    assert "E9NEW" in clients and "E8C" not in clients


def test_offline_uses_the_cached_copy(floor, tmp_path):
    floor.setup.shared_data = tmp_path / "gone"                             # the drive disconnects
    assert not floor.sync(timeout=1)
    assert floor.online is False and floor.status_text().startswith("Offline, using cached rules")
    assert "E8D" in [p.stem for p in (floor.cache_dir / "clients").glob("*.yaml")]
    assert load_config()["checks"]["too_short"]["min_words"] == 2
    with pytest.raises(SharedUnavailable):
        floor.write_root


def test_a_shared_folder_missing_files_falls_back_to_the_apps_own(floor):
    (floor.shared_dir / "rules.yaml").unlink()
    floor.sync()
    assert not (floor.cache_dir / "rules.yaml").exists()
    assert resolve_path(load_config(), "rules_file") == ROOT / "rules.yaml"


def test_local_yaml_overrides_this_pc_only(floor):
    save_local_settings(floor.local_dir, {"machine": {"whisper_model_size": "tiny"}})
    assert load_config()["machine"]["whisper_model_size"] == "tiny"
    save_local_settings(floor.local_dir, {"machine": {"whisper_model_size": None}})
    assert load_config()["machine"]["whisper_model_size"] in ("base", "small")


def test_the_shared_config_cannot_set_machine_settings(floor):
    (floor.shared_dir / "config.yaml").write_text(
        "machine:\n  ffmpeg_path: '\\\\\\\\server\\\\share\\\\not-ffmpeg.exe'\n  whisper_model_size: tiny\n"
        "checks:\n  too_short:\n    min_words: 3\n", encoding="utf-8")
    time.sleep(0.01)
    assert floor.sync()
    config = load_config()
    assert config["machine"]["ffmpeg_path"] is None                         # the app's own value
    assert config["machine"]["whisper_model_size"] in ("base", "small")    # "auto" from the app's own config
    assert config["checks"]["too_short"]["min_words"] == 3                  # the rest of the shared file still applies


def test_local_yaml_still_sets_ffmpeg_on_the_floor(floor, tmp_path):
    ffmpeg = tmp_path / "ffmpeg.exe"
    (floor.shared_dir / "config.yaml").write_text("machine:\n  ffmpeg_path: elsewhere.exe\n", encoding="utf-8")
    time.sleep(0.01)
    assert floor.sync()
    save_local_settings(floor.local_dir, {"machine": {"ffmpeg_path": str(ffmpeg), "ffmpeg_timeout_sec": 60}})
    machine = load_config()["machine"]
    assert machine["ffmpeg_path"] == str(ffmpeg) and machine["ffmpeg_timeout_sec"] == 60


def test_mirror_keeps_the_copy_when_the_clients_folder_vanishes_for_a_moment(tmp_path):
    src, dst = tmp_path / "s", tmp_path / "d"
    (src / "clients").mkdir(parents=True)
    (src / "clients" / "E1A.yaml").write_text("client: E1A\n", encoding="utf-8")
    mirror(src, dst)
    shutil.rmtree(src / "clients")
    mirror(src, dst)
    assert (dst / "clients" / "E1A.yaml").is_file()


# --- safe shared writes ------------------------------------------------------------------------------------

def test_lock_blocks_a_second_writer_and_names_the_holder(tmp_path):
    path = tmp_path / ".locks" / "data.lock"
    with DataLock(path):
        with pytest.raises(LockBusy, match="is saving changes"):
            with DataLock(path, timeout=0.3):
                pass
    with DataLock(path, timeout=0.3):                                       # released: free again
        pass


def test_a_stale_lock_is_taken_over(tmp_path):
    path = tmp_path / "data.lock"
    path.write_text(json.dumps({"user": "crashed", "computer": "PC9"}), encoding="utf-8")
    old = time.time() - 600
    os.utime(path, (old, old))
    with DataLock(path, timeout=0.5, stale_after=120):
        assert json.loads(path.read_text(encoding="utf-8"))["user"] != "crashed"


def test_shared_text_save_backs_up_logs_and_refuses_a_conflicting_edit(floor):
    path = floor.shared_dir / "ignore.yaml"
    opened = path.stat().st_mtime
    write_shared_text(floor, "ignore.yaml", "ignore:\n  - {type: spelling, text: Danja}\n", "edit", "+1 line",
                      expected_mtime=opened)
    assert list((floor.shared_dir / "backups" / "ignore").glob("ignore_*.yaml"))
    assert read_changelog(floor.shared_dir / "changelog.csv")[-1]["changes"] == "+1 line"
    assert "Danja" in (floor.cache_dir / "ignore.yaml").read_text(encoding="utf-8")       # cache refreshed
    with pytest.raises(LockBusy, match="changed by someone else"):
        write_shared_text(floor, "ignore.yaml", "ignore: []\n", "edit", "x", expected_mtime=opened)


def test_client_saves_go_to_the_shared_folder_under_the_lock(floor):
    store = ClientStore(load_config(), lock=floor.lock, root=floor.write_root)
    store.save(build_client([Finding("code", "E9ZZ"), Finding("name", "Zed")]), "add", user="ed")
    assert (floor.shared_dir / "clients" / "E9ZZ.yaml").is_file()
    assert not (floor.cache_dir / "clients" / "E9ZZ.yaml").exists()          # until the next sync
    floor.sync()
    assert (floor.cache_dir / "clients" / "E9ZZ.yaml").is_file()
    # Another PC added E7AA a moment ago; this PC's cache doesn't have it yet, the duplicate is still seen.
    (floor.shared_dir / "clients" / "E7AA.yaml").write_text("client: E7AA\n", encoding="utf-8")
    with pytest.raises(ClientError, match="already a client E7AA"):
        store.save(build_client([Finding("code", "7aa"), Finding("name", "x")]), "add")


# --- roles ---------------------------------------------------------------------------------------------------

def test_pin_is_stored_hashed_and_checked(floor):
    stored = hash_pin("4821")
    assert "4821" not in stored and verify_pin("4821", stored) and not verify_pin("4822", stored)
    assert not verify_pin("4821", None) and not verify_pin("4821", "garbage")
    set_editor_pin(floor, "4821")
    text = (floor.shared_dir / "shared_settings.yaml").read_text(encoding="utf-8")
    assert "4821" not in text and text.startswith("# Floor-wide settings")
    assert verify_pin("4821", yaml.safe_load(text)["editor_pin"])
    with pytest.raises(ValueError):
        set_editor_pin(floor, "12")


def test_roles(floor, tmp_path):
    assert role_for(Floor(FloorSetup(), local_dir=tmp_path), "anyone").editor          # alone on a PC
    assert role_for(floor, "anyone").editor                                             # nothing set up yet
    write_shared_text(floor, "shared_settings.yaml", "editors: [Dawood]\neditor_pin: null\n", "edit", "editors")
    assert role_for(floor, "dawood").editor and not role_for(floor, "sara").editor
    assert role_for(floor, "sara", unlocked=True).editor
    assert "view only" in role_for(floor, "sara").why


# --- the machine --------------------------------------------------------------------------------------------------

def test_whisper_model_and_threads_from_the_machine(monkeypatch):
    assert choose_whisper_model(2, 16) == "base" and choose_whisper_model(8, 6) == "base"
    assert choose_whisper_model(8, 16) == "small" and choose_whisper_model(4, None) == "small"
    monkeypatch.setattr("src.machine.cpu_count", lambda: 12)
    monkeypatch.setattr("src.machine.ram_gb", lambda: 32.0)
    config = apply_machine_defaults({"machine": {"whisper_model_size": "auto", "cpu_threads": "auto"},
                                     "ocr": {"cpu_threads": "auto"}})
    assert config["machine"] == {"whisper_model_size": "small", "cpu_threads": 6} and config["ocr"]["cpu_threads"] == 6
    fixed = apply_machine_defaults({"machine": {"whisper_model_size": "tiny", "cpu_threads": 3}})
    assert fixed["machine"] == {"whisper_model_size": "tiny", "cpu_threads": 3}


def test_low_disk_and_memory_warnings(tmp_path, monkeypatch):
    assert resource_warnings([tmp_path], min_disk_gb=0, min_ram_gb=0) == []
    warnings = resource_warnings([tmp_path, tmp_path / "x" / "y"], min_disk_gb=10 ** 9, min_ram_gb=10 ** 9)
    assert len([w for w in warnings if "GB free on" in w]) == 1 and any("memory" in w for w in warnings)


def test_one_check_at_a_time_per_pc(tmp_path):
    first, second = JobLock(tmp_path), JobLock(tmp_path)
    assert first.try_acquire()
    assert not second.try_acquire()                               # this process is alive: wait
    assert second.acquire(should_stop=lambda: True, poll_sec=0.01) is False
    first.release()
    assert second.try_acquire()
    second.release()
    (tmp_path / "check.lock").write_text(json.dumps({"pid": 2 ** 22 + 12345}), encoding="utf-8")   # ended long ago
    assert first.try_acquire()
    first.release()


def test_whisper_model_ready_in_the_shared_models_folder(tmp_path):
    from src.audio.transcribe import ready_model_dir

    shared_models, mine = tmp_path / "shared", tmp_path / "mine"
    assert ready_model_dir("small", [shared_models, mine]) is None
    (shared_models / "whisper" / "small").mkdir(parents=True)
    (shared_models / "whisper" / "small" / "model.bin").write_bytes(b"x")
    assert ready_model_dir("small", [shared_models, mine]) == shared_models / "whisper" / "small"
    assert ready_model_dir("base", [shared_models, mine]) is None


def test_packaged_app_prefers_the_bundled_ffmpeg(monkeypatch):
    import sys

    from src.utils import system

    monkeypatch.setattr(system, "bundled_ffmpeg", lambda: "/bundled/ffmpeg")
    monkeypatch.setattr(system.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    assert system.find_ffmpeg() == "/usr/bin/ffmpeg"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert system.find_ffmpeg() == "/bundled/ffmpeg"


# --- versions, reports, diagnostics ---------------------------------------------------------------------------------

def test_update_check(tmp_path):
    latest = tmp_path / "latest.json"
    assert check_for_update(latest) is None
    latest.write_text(json.dumps({"version": "99.0", "windows": r"\\srv\VideoQCBot-99.zip", "macos": "/Volumes/x.zip",
                                  "notes": "big"}), encoding="utf-8")
    info = check_for_update(latest)
    assert info.version == "99.0" and info.installer in (r"\\srv\VideoQCBot-99.zip", "/Volumes/x.zip")
    latest.write_text(json.dumps({"version": __version__}), encoding="utf-8")
    assert check_for_update(latest) is None
    assert parse_version("v1.10.2") > parse_version("1.9") and parse_version("junk") == (0,)


def test_report_copied_to_the_shared_folder_by_client_date_user(tmp_path):
    report = tmp_path / "clip_QC.docx"
    report.write_bytes(b"docx")
    clips = tmp_path / "ep 12_20261013-101500_clips"
    clips.mkdir()
    (clips / "a.mp3").write_bytes(b"x")
    first = copy_report_to_shared(report, tmp_path / "reports", "E2CL", "sara", Path("ep 12: final?.mp3"), clips)
    second = copy_report_to_shared(report, tmp_path / "reports", "E2CL", "sara", Path("ep 12: final?.mp3"))
    assert first.parent == tmp_path / "reports" / "E2CL" and first.name.endswith("_sara_ep 12_ final.docx")
    assert second.name.endswith("-2.docx") and (first.parent / clips.name / "a.mp3").is_file()   # the name links use


def test_reports_carry_app_and_client_rules_versions(tmp_path):
    from src.engine import QCOptions, client_rules_version, run_qc, write_report

    config = load_config()
    config = {**config, "report": {**config["report"], "output_dir": str(tmp_path)}}
    result = run_qc(None, "E8D", options=QCOptions(srt_path=ROOT / "tests" / "fixtures" / "sample_mistakes.srt"),
                    config=config)
    text = next(p for p in write_report(result, formats=["md"]) if p.suffix == ".md").read_text(encoding="utf-8")
    version = client_rules_version(config, "E8D")
    assert f"- **App version:** Video QC Bot {__version__}" in text
    assert f"- **Client rules version:** {version}" in text and len(version) == 12
    assert client_rules_version(config, None) is None


def test_diagnostics_never_contain_the_api_key(monkeypatch, tmp_path):
    from src.diagnostics import collect

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-123")
    log = tmp_path / "videoqcbot.log"
    log.write_text("line 1\nline 2\n", encoding="utf-8")
    text = collect(load_config(), {"last_client": "E8D"}, log, "True (test)")
    assert f"Video QC Bot {__version__}" in text and "line 2" in text and "ANTHROPIC_API_KEY set: yes" in text
    assert "sk-ant-secret-123" not in text and "Floor setup" in text


# --- dialogs (headless) ---------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    from src.app import main as app_main

    return app_main.make_application(["test"])


def test_text_file_dialog_saves_safely_and_is_view_only_for_others(qapp, floor):
    from src.app.floor_dialogs import TextFileDialog

    dialog = TextFileDialog(floor, "glossary.txt", "Glossary", editable=True)
    dialog.editor.setPlainText(dialog.editor.toPlainText() + "=Danja\n")
    dialog.save()
    assert "=Danja" in (floor.shared_dir / "glossary.txt").read_text(encoding="utf-8")
    assert read_changelog(floor.shared_dir / "changelog.csv")[-1]["file"] == "glossary.txt"
    viewer = TextFileDialog(floor, "glossary.txt", "Glossary", editable=False, why="view only")
    assert viewer.editor.isReadOnly()


def test_setup_dialog_work_alone(qapp, tmp_path):
    from src.app.floor_dialogs import SetupDialog

    user_file = tmp_path / "floor.yaml"
    dialog = SetupDialog(None, user_file=user_file)
    dialog.alone.setChecked(True)
    dialog.accept()
    assert read_setup(bootstrap=None, user_file=user_file).alone


def test_setup_dialog_sets_up_a_shared_folder(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from src.app.floor_dialogs import SetupDialog

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    user_file = tmp_path / "floor.yaml"
    (tmp_path / "share").mkdir()
    dialog = SetupDialog(None, user_file=user_file)
    dialog.folder.setText(str(tmp_path / "share"))
    dialog.accept()
    assert dialog.chosen == tmp_path / "share" and (tmp_path / "share" / "shared_settings.yaml").is_file()
    assert read_setup(bootstrap=None, user_file=user_file).shared_data == tmp_path / "share"


def test_menu_on_the_floor(qapp, floor, tmp_path, monkeypatch):
    from src.app import main as app_main
    from src.app.settings import Settings

    write_shared_text(floor, "shared_settings.yaml", "editors: [someone_else]\neditor_pin: null\n", "edit", "x")
    monkeypatch.setattr(app_main, "current_user", lambda: "sara")
    c = app_main.CompanionApp(qapp, Settings(tmp_path / "settings.json"))
    try:
        menu = c.build_menu()                       # kept: a menu without a parent is deleted when dropped
        labels = [a.text() for a in menu.actions()]
        assert labels[0] == f"Video QC Bot {__version__}"
        assert {"Check for update", "Copy diagnostics", "Editor mode...", "View ignore list"} <= set(labels)
        clients = next(a.menu() for a in menu.actions() if a.text() == "Client")
        add = next(a for a in clients.actions() if a.text() == "Add new client...")
        assert not add.isEnabled() and "Editors only" in add.toolTip()
        c.editor_unlocked = True
        menu = c.build_menu()
        labels = [a.text() for a in menu.actions()]
        assert "Leave editor mode" in labels and "Edit ignore list" in labels
        floor.setup.shared_data = tmp_path / "gone"
        floor.sync(timeout=0.5)
        floor.last_sync = time.time()               # no background sync from the menu during the test
        menu = c.build_menu()
        assert any(a.text().startswith("Offline, using cached rules") for a in menu.actions())
    finally:
        for timer in (c.sync_timer, c.watch_timer):
            timer.stop()
        c.mascot.hide()
        if c.tray is not None:
            c.tray.hide()
