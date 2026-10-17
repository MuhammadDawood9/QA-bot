"""Reports must land where they can really be written. A reports folder that doesn't exist yet
is created; one that can't be written to (Windows "Controlled folder access" can block even
Documents, so the app fell over at the last step) falls back to the app's local data folder and
says so in the log."""

import datetime as dt
import os
import stat
from pathlib import Path

import pytest

from src.models import Caption, Issue
from src.report import builder
from src.report.builder import ReportInfo, build_report, ensure_writable

WHEN = dt.datetime(2026, 10, 12, 9, 30, 0)
CAPTIONS = [Caption(1, 0.0, 2.0, "Welcome back.")]
ISSUES = [Issue(1, 0.0, "spelling", "low", "Welcome back.", "Odd text", "", 0.5)]


def info() -> ReportInfo:
    return ReportInfo(srt=Path("clip.srt"), generated=WHEN)


def settings(output_dir, **kw) -> dict:
    return {"output_dir": output_dir, "formats": ["md"], "include_screenshots": True,
            "write_csv": False, **kw}


@pytest.fixture
def local_folder(tmp_path, monkeypatch) -> Path:
    """The app's local reports folder, somewhere harmless (the real one is in %LOCALAPPDATA%)."""
    folder = tmp_path / "appdata" / "reports"
    monkeypatch.setattr(builder, "local_reports_dir", lambda: folder)
    return folder


def test_missing_folder_is_created(tmp_path, local_folder):
    """A reports folder that doesn't exist yet is created, parents and all."""
    target = tmp_path / "never" / "existed"
    written = build_report(CAPTIONS, ISSUES, info(), settings(target))
    report = written[0]
    assert report == target / "clip_20261012-093000" / "report.md"
    assert report.is_file()
    assert not local_folder.exists()                   # no fallback was needed


def test_read_only_folder_falls_back(tmp_path, local_folder, monkeypatch, caplog):
    """A folder that exists but refuses writes: the report goes to the local folder, and the
    log says where it went and why. Windows doesn't make folders read-only for their owner, so
    the refused write is simulated exactly where it would happen: the probe file."""
    target = tmp_path / "protected"
    real_probe = builder._probe

    def probe(folder: Path) -> None:
        folder = Path(folder)
        if folder == target or target in folder.parents:
            raise PermissionError(5, "Access is denied (Controlled folder access)")
        real_probe(folder)

    monkeypatch.setattr(builder, "_probe", probe)
    written = build_report(CAPTIONS, ISSUES, info(), settings(target))
    report = written[0]
    assert report == local_folder / "clip_20261012-093000" / "report.md"
    assert report.is_file()
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any(str(target) in w and str(local_folder) in w and "written to" in w for w in warnings)


@pytest.mark.skipif(os.name == "nt", reason="Windows folders stay writable for their owner")
def test_truly_read_only_folder_falls_back(tmp_path, local_folder):
    """The real thing where the OS honours it: a chmod'd read-only folder."""
    target = tmp_path / "readonly"
    target.mkdir()
    os.chmod(target, stat.S_IRUSR | stat.S_IXUSR)
    try:
        written = build_report(CAPTIONS, ISSUES, info(), settings(target))
    finally:
        os.chmod(target, stat.S_IWUSR | stat.S_IXUSR)
    assert written[0] == local_folder / "clip_20261012-093000" / "report.md"
    assert written[0].is_file()


def test_folder_path_taken_by_a_file_falls_back(tmp_path, local_folder):
    """A real failure, no patching: something else sits where the folder should be, so the
    folder can never be created."""
    target = tmp_path / "in_the_way"
    target.write_text("", encoding="utf-8")
    written = build_report(CAPTIONS, ISSUES, info(), settings(target))
    assert written[0] == local_folder / "clip_20261012-093000" / "report.md"
    assert written[0].is_file()


def test_ensure_writable_creates_the_folder_and_cleans_up(tmp_path):
    folder = ensure_writable(tmp_path / "a" / "b")
    assert folder.is_dir()
    assert list(folder.iterdir()) == []                # the test file is gone again
