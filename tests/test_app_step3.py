"""Step 3 of the app, headless: choosing the client and checks, the background worker (with a
fake engine), dropping and clicking on the mascot, and what the app does when a check finishes,
fails or is cancelled. Real drag-and-drop from Explorer/Finder is in the README's manual steps."""

import os
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, Qt, QUrl  # noqa: E402
from PySide6.QtGui import QDropEvent, QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from src.app import main as app_main  # noqa: E402
from src.app import worker  # noqa: E402
from src.app.choices import CHECK_TOGGLES, choose_client, file_dialog_filter, is_video, qc_options, videos_in  # noqa: E402
from src.app.mascot import MascotWindow  # noqa: E402
from src.app.settings import Settings  # noqa: E402
from src.engine import QCError  # noqa: E402
from src.utils.progress import Cancelled  # noqa: E402

KNOWN = ["E2CI", "E2OCA", "E3RK", "E8C", "E8D"]


@pytest.fixture(scope="module")
def app():
    return app_main.make_application(["test"])


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_path / "settings.json")


def wait_for(condition, seconds=10.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return False


# --- choices ---------------------------------------------------------------------------------

def test_which_files_are_videos(tmp_path):
    (tmp_path / "clip.MP4").write_bytes(b"x")
    (tmp_path / "notes.txt").write_bytes(b"x")
    (tmp_path / "folder.mov").mkdir()
    assert is_video(Path("a.mov")) and is_video(Path("b.MXF")) and not is_video(Path("c.srt"))
    assert videos_in([tmp_path / "clip.MP4", tmp_path / "notes.txt", tmp_path / "folder.mov"]) == [tmp_path / "clip.MP4"]
    assert "*.mp4" in file_dialog_filter() and "*.mov" in file_dialog_filter()


def test_client_from_the_file_name_then_the_last_one():
    assert choose_client(Path("E8D_reel.mp4"), "E2CI", True, KNOWN) == ("E8D", "E8D (from the file name)")
    assert choose_client(Path("E8D_reel.mp4"), "E2CI", False, KNOWN) == ("E2CI", "E2CI (last used)")
    assert choose_client(Path("del.mp4"), "E2CI", True, KNOWN) == ("E2CI", "E2CI (last used)")
    assert choose_client(Path("del.mp4"), None, True, KNOWN) == (None, "default rules")


def test_check_toggles_become_engine_options():
    every = qc_options([])
    assert every.use_llm and every.check_color and not every.disabled_checks
    some = qc_options(["punctuation", "color", "spelling"])
    assert not some.use_llm and not some.check_color and some.disabled_checks == frozenset({"punctuation", "spelling"})
    assert {k for k, _ in CHECK_TOGGLES} >= {"too_short", "spelling", "punctuation", "transcript", "screen_text", "color"}


def test_new_settings_defaults(settings):
    assert settings.get("disabled_checks") == ["punctuation"]          # the paid check is off until switched on
    assert settings.get("guess_client") is True and settings.get("open_report") is True


def test_wrong_typed_setting_falls_back_to_default(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"open_report": "yes please", "last_client": "E8D"}', encoding="utf-8")
    s = Settings(path)
    assert s.get("open_report") is True and s.get("last_client") == "E8D"


# --- the background worker -------------------------------------------------------------------

class FakeEngine:
    """Stands in for run_qc / build_docx_report: reports progress, honours cancel."""

    def __init__(self, tmp_path, issues=3, fail=None, steps=5, delay=0.0):
        self.tmp_path, self.issues, self.fail, self.steps, self.delay = tmp_path, issues, fail, steps, delay
        self.thread = None
        self.calls = []

    def run_qc(self, video, client, progress_cb, cancel_event, *, options=None, config=None):
        self.thread = threading.current_thread()
        self.calls.append((video, client, options))
        for n in range(self.steps):
            if cancel_event.is_set():
                raise Cancelled("cancelled")
            progress_cb("Reading captions", n / self.steps)
            time.sleep(self.delay)
        if self.fail:
            raise self.fail
        return ["issue"] * self.issues

    def build_docx_report(self, result, output_dir=None):
        path = self.tmp_path / "clip_QC_2026-10-12.docx"
        path.write_bytes(b"docx")
        return path


@pytest.fixture
def fake(tmp_path, monkeypatch):
    engine = FakeEngine(tmp_path)
    monkeypatch.setattr(worker, "run_qc", engine.run_qc)
    monkeypatch.setattr(worker, "build_docx_report", engine.build_docx_report)
    return engine


def run_and_collect(runner, video=Path("clip.mp4")):
    seen = {"progress": [], "finished": None, "failed": None, "cancelled": False}
    runner.progress.connect(lambda s, f: seen["progress"].append((s, f)))
    runner.finished.connect(lambda n, p: seen.__setitem__("finished", (n, p)))
    runner.failed.connect(lambda m: seen.__setitem__("failed", m))
    runner.cancelled.connect(lambda: seen.__setitem__("cancelled", True))
    runner.start(video, "E8D", qc_options([]))
    return seen


def test_worker_runs_off_the_ui_thread_and_reports_back(app, fake):
    runner = worker.CheckRunner()
    seen = run_and_collect(runner)
    assert runner.busy
    assert wait_for(lambda: seen["finished"] is not None)
    assert fake.thread is not threading.main_thread()
    assert seen["finished"][0] == 3 and seen["finished"][1].endswith(".docx")
    assert [s for s, _ in seen["progress"]][-1] == "Writing the report"
    assert wait_for(lambda: not runner.busy)


def test_worker_failure_gives_the_engine_message(app, fake):
    fake.fail = QCError("Video not found: clip.mp4")
    runner = worker.CheckRunner()
    seen = run_and_collect(runner)
    assert wait_for(lambda: seen["failed"] is not None)
    assert seen["failed"] == "Video not found: clip.mp4"


def test_worker_bug_is_caught_and_logged(app, fake, caplog):
    fake.fail = ZeroDivisionError("oops")
    runner = worker.CheckRunner()
    seen = run_and_collect(runner)
    assert wait_for(lambda: seen["failed"] is not None)
    assert "Unexpected error (ZeroDivisionError: oops)" in seen["failed"]
    assert "Traceback" in caplog.text or "oops" in caplog.text


def test_worker_can_be_cancelled(app, fake):
    fake.steps, fake.delay = 200, 0.01
    runner = worker.CheckRunner()
    seen = run_and_collect(runner)
    assert wait_for(lambda: len(seen["progress"]) > 2)
    runner.cancel()
    assert wait_for(lambda: seen["cancelled"])
    assert seen["finished"] is None


def test_only_one_check_at_a_time(app, fake):
    fake.steps, fake.delay = 50, 0.01
    runner = worker.CheckRunner()
    run_and_collect(runner)
    with pytest.raises(RuntimeError):
        runner.start(Path("other.mp4"), None, qc_options([]))
    runner.cancel()
    assert wait_for(lambda: not runner.busy)


# --- the mascot: drop and click --------------------------------------------------------------

def drop(widget, paths):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    event = QDropEvent(QPointF(60, 60), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier)
    widget.dropEvent(event)            # Qt only routes a drop after a real drag has entered the window


def test_dropping_videos_emits_only_the_videos(app, settings, tmp_path):
    video = tmp_path / "Vidéo été 01.mp4"                        # spaces and accents
    video.write_bytes(b"x")
    other = tmp_path / "notes.txt"
    other.write_bytes(b"x")
    mascot = MascotWindow(settings)
    got = []
    mascot.files_dropped.connect(got.append)
    drop(mascot, [other, video])
    drop(mascot, [other])
    assert got == [[video]]


def test_click_without_drag_emits_clicked(app, settings):
    mascot = MascotWindow(settings)
    mascot.show()
    clicks = []
    mascot.clicked.connect(lambda: clicks.append(1))
    where = QPointF(mascot.mapToGlobal(QPoint(60, 60)))
    for kind, buttons in ((QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton),
                          (QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)):
        QApplication.sendEvent(mascot, QMouseEvent(kind, QPointF(60, 60), where, Qt.MouseButton.LeftButton, buttons,
                                                   Qt.KeyboardModifier.NoModifier))
    assert clicks == [1]
    mascot.hide()


def test_progress_ring_and_tooltip(app, settings):
    mascot = MascotWindow(settings)
    mascot.set_state("working")
    mascot.set_progress("Transcribing audio", 0.42)
    assert mascot.progress == 0.42 and "Transcribing audio... 42%" in mascot.toolTip()
    assert not mascot.grab().toImage().isNull()
    mascot.set_state("done", 3)
    assert mascot.progress is None


# --- the whole app: finished, failed, cancelled ----------------------------------------------

@pytest.fixture
def companion(app, settings, fake, monkeypatch):
    opened, errors = [], []
    monkeypatch.setattr(app_main, "open_path", lambda p: opened.append(Path(p)) or True)
    monkeypatch.setattr(app_main, "show_error", lambda parent, msg, log: errors.append(msg))
    c = app_main.CompanionApp(app, settings)
    c.opened, c.errors = opened, errors
    yield c
    c.runner.cancel()
    c.runner.wait(5000)
    c.mascot.hide()
    if c.tray is not None:
        c.tray.hide()


def test_finished_check_shows_the_count_and_opens_the_report(companion, settings, fake):
    companion.start_check(Path("E8D_reel 01.mp4"))
    assert companion.mascot.state == "working"
    assert wait_for(lambda: companion.mascot.state == "done")
    assert companion.mascot.count == 3
    assert fake.calls[0][1] == "E8D"                                # client from the file name
    assert companion.opened == [Path(settings.get("last_report"))]
    assert companion.status.startswith("Done: 3 issues")


def test_report_is_not_opened_when_switched_off(companion, settings):
    settings.set("open_report", False)
    companion.start_check(Path("clip.mp4"))
    assert wait_for(lambda: companion.mascot.state == "done")
    assert companion.opened == []


def test_failed_check_shows_the_error(companion, fake):
    fake.fail = QCError("The video has no audio")
    companion.start_check(Path("clip.mp4"))
    assert wait_for(lambda: companion.mascot.state == "error")
    assert companion.errors == ["The video has no audio"]


def test_cancelled_check_goes_back_to_idle(companion, fake):
    fake.steps, fake.delay = 300, 0.01
    companion.start_check(Path("clip.mp4"))
    assert wait_for(lambda: companion.runner.busy and companion.mascot.progress is not None)
    companion.cancel_check()
    assert wait_for(lambda: companion.mascot.state == "idle")
    assert "cancelled" in companion.status.lower()


def test_menu_toggles_and_client_choice_are_saved(companion, settings):
    menu = companion.build_menu()
    labels = [a.text() for a in menu.actions()]
    assert "Check a video..." in labels and "Cancel check" in labels and "Quit Video QC Bot" in labels
    checks = next(a.menu() for a in menu.actions() if a.text() == "Checks")
    color = next(a for a in checks.actions() if a.text() == "Color grading")
    color.toggle()                                                   # off
    assert "color" in settings.get("disabled_checks")
    clients = next(a.menu() for a in menu.actions() if a.text() == "Client")
    next(a for a in clients.actions() if a.text() == "E8C").trigger()
    assert Settings(settings.path).get("last_client") == "E8C"


def test_second_drop_while_busy_is_refused(companion, fake):
    fake.steps, fake.delay = 300, 0.01
    companion.start_check(Path("clip.mp4"))
    companion.videos_dropped([Path("other.mp4")])
    assert len(fake.calls) <= 1 and companion.runner.video == Path("clip.mp4")
