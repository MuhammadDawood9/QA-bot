"""Running a check in the background, so the window never freezes.

    runner = CheckRunner()
    runner.progress.connect(...)      # (stage name, overall fraction 0..1)
    runner.finished.connect(...)      # (issue count, report path)
    runner.failed.connect(...)        # (message for the user)
    runner.cancelled.connect(...)
    runner.start(video, client, options, reports_dir)
    runner.cancel()                   # stops at the next frame / batch

The engine runs on a QThread. It reports progress through run_qc's callback, which emits a Qt
signal; Qt delivers signals across threads to the window safely. Progress is passed on at most
once per 0.5% so the window isn't flooded.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot

from src.engine import QCError, QCOptions, build_docx_report, run_qc
from src.utils.progress import Cancelled

log = logging.getLogger("qcbot.app")


class _Job(QObject):
    progress = Signal(str, float)
    finished = Signal(int, str)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, video: Path, client: str | None, options: QCOptions, reports_dir: Path | None,
                 cancel_event: threading.Event):
        super().__init__()
        self.video, self.client, self.options, self.reports_dir = video, client, options, reports_dir
        self.cancel_event = cancel_event
        self._last = (None, -1.0)

    def _report_progress(self, stage: str, fraction: float) -> None:
        if stage != self._last[0] or fraction - self._last[1] >= 0.005 or fraction >= 1.0:
            self._last = (stage, fraction)
            self.progress.emit(stage, fraction)

    @Slot()
    def run(self) -> None:
        try:
            log.info("checking %s (client %s)", self.video, self.client or "default")
            result = run_qc(self.video, self.client, self._report_progress, self.cancel_event, options=self.options)
            self.progress.emit("Writing the report", 1.0)
            if self.cancel_event.is_set():
                raise Cancelled("cancelled before the report")
            report = build_docx_report(result, output_dir=self.reports_dir)
            log.info("done: %d issues, report %s", len(result), report)
            self.finished.emit(len(result), str(report))
        except Cancelled:
            log.info("check of %s cancelled", self.video.name)
            self.cancelled.emit()
        except QCError as exc:
            log.error("check of %s failed: %s", self.video.name, exc)
            self.failed.emit(str(exc))
        except Exception as exc:                       # a bug: keep the app alive, keep the details in the log
            log.exception("unexpected error while checking %s", self.video)
            self.failed.emit(f"Unexpected error ({type(exc).__name__}: {exc}). The details are in the log file.")


class CheckRunner(QObject):
    """One check at a time, on its own thread."""
    progress = Signal(str, float)
    finished = Signal(int, str)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._thread: QThread | None = None
        self._job: _Job | None = None
        self._cancel = threading.Event()
        self.video: Path | None = None

    @property
    def busy(self) -> bool:
        return self._thread is not None

    def start(self, video: Path, client: str | None, options: QCOptions, reports_dir: Path | None = None) -> None:
        if self.busy:
            raise RuntimeError("a check is already running")
        self.video = video
        self._cancel = threading.Event()
        self._thread = QThread()
        self._job = _Job(video, client, options, reports_dir, self._cancel)
        self._job.moveToThread(self._thread)
        self._thread.started.connect(self._job.run)
        self._job.progress.connect(self.progress)
        for signal, relay in ((self._job.finished, self.finished), (self._job.failed, self.failed),
                              (self._job.cancelled, self.cancelled)):
            # Direct: quit() is thread-safe, and a queued call would wait for the main thread's event
            # loop, so wait() (on Quit) would block forever.
            signal.connect(self._thread.quit, Qt.ConnectionType.DirectConnection)
            signal.connect(relay)
        self._thread.finished.connect(self._cleanup)
        self._thread.start()

    def cancel(self) -> None:
        if self.busy:
            self._cancel.set()

    def wait(self, msecs: int = 30_000) -> bool:
        """Block until the current check ends (used when quitting and in tests)."""
        thread = self._thread
        return thread.wait(msecs) if thread is not None else True

    def _cleanup(self) -> None:
        thread, job = self._thread, self._job
        self._job = self._thread = None
        if thread is not None:
            # QThread.finished is emitted just before the thread really ends. Python frees the
            # QThread when the last reference goes, and freeing a running QThread aborts the
            # process, so let it end first (a few milliseconds at most).
            thread.wait()
        del job, thread
