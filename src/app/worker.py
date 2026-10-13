"""Running a check in the background, so the window never freezes.

    runner = CheckRunner()
    runner.progress.connect(...)      # (stage name, overall fraction 0..1)
    runner.finished.connect(...)      # (issue count, report path)
    runner.warning.connect(...)       # (message): low disk or memory, a report not copied to the shared folder
    runner.failed.connect(...)        # (message for the user)
    runner.cancelled.connect(...)
    runner.idle.connect(...)          # the thread is gone: the next check can start
    runner.start(video, client, options, reports_dir)
    runner.cancel()                   # stops at the next frame / batch

Before the engine starts, the job refreshes the cached shared rules (so edits made on other PCs
apply without a restart), loads the config, warns about low disk space or memory, and waits for
the PC's one-check-at-a-time lock (another user of this PC may be running a check). After the
report, it is copied to the shared reports folder when that's switched on.

The engine runs on a QThread. It reports progress through run_qc's callback, which emits a Qt
signal; Qt delivers signals across threads to the window safely. Progress is passed on at most
once per 0.5% so the window isn't flooded.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot

from src.config import load_config, resolve_path
from src.engine import QCError, QCOptions, build_docx_report, run_qc
from src.machine import JobLock, resource_warnings
from src.utils.progress import Cancelled

log = logging.getLogger("qcbot.app")


class _Job(QObject):
    progress = Signal(str, float)
    finished = Signal(int, str)
    failed = Signal(str)
    cancelled = Signal()
    warning = Signal(str)

    def __init__(self, video: Path, client: str | None, options: QCOptions, reports_dir: Path | None,
                 cancel_event: threading.Event, shared_reports: Path | None = None):
        super().__init__()
        self.video, self.client, self.options, self.reports_dir = video, client, options, reports_dir
        self.cancel_event = cancel_event
        self.shared_reports = shared_reports
        self._last = (None, -1.0)

    def _prepare(self) -> dict:
        """Fresh rules, a word about low resources, and this PC's turn to run a check."""
        from src.shared import current_floor

        floor = current_floor()
        if floor.shared:
            self.progress.emit("Refreshing the shared rules", 0.0)
            floor.sync()
        config = load_config(self.options.config_path)
        folders = [p for p in (self.reports_dir, resolve_path(config, "transcript_cache_dir")) if p is not None]
        for message in resource_warnings(folders):
            log.warning(message)
            self.warning.emit(message)
        return config

    def _wait_turn(self, lock: JobLock) -> None:
        def waiting(owner):
            who = f" ({owner.get('user')})" if owner and owner.get("user") else ""
            self.progress.emit(f"Waiting: another check is running on this PC{who}", 0.0)

        if not lock.acquire(wait=waiting, should_stop=self.cancel_event.is_set, poll_sec=1.0):
            raise Cancelled("cancelled while waiting for another check on this PC")

    def _share(self, report: Path) -> None:
        if self.shared_reports is None:
            return
        from src.shared import copy_report_to_shared
        from src.utils.safe_io import current_user

        try:
            clips = report.parent / "clips"
            copied = copy_report_to_shared(report, self.shared_reports, self.client, current_user(), self.video,
                                           extra_folder=clips if clips.is_dir() else None)
            log.info("report copied to the shared folder: %s", copied)
        except OSError as exc:
            log.warning("report not copied to the shared folder: %s", exc)
            self.warning.emit(f"The report is saved on this PC, but couldn't be copied to the shared folder: {exc}")

    def _report_progress(self, stage: str, fraction: float) -> None:
        if stage != self._last[0] or fraction - self._last[1] >= 0.005 or fraction >= 1.0:
            self._last = (stage, fraction)
            self.progress.emit(stage, fraction)

    @Slot()
    def run(self) -> None:
        lock = JobLock()
        try:
            log.info("checking %s (client %s)", self.video, self.client or "default")
            config = self._prepare()
            self._wait_turn(lock)
            result = run_qc(self.video, self.client, self._report_progress, self.cancel_event, options=self.options,
                            config=config)
            self.progress.emit("Writing the report", 1.0)
            if self.cancel_event.is_set():
                raise Cancelled("cancelled before the report")
            report = build_docx_report(result, output_dir=self.reports_dir)
            log.info("done: %d issues, report %s", len(result), report)
            self._share(Path(report))
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
        finally:
            lock.release()


class CheckRunner(QObject):
    """One check at a time, on its own thread."""
    progress = Signal(str, float)
    finished = Signal(int, str)
    failed = Signal(str)
    cancelled = Signal()
    warning = Signal(str)
    idle = Signal()                       # after finished/failed/cancelled, once start() works again

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._thread: QThread | None = None
        self._job: _Job | None = None
        self._cancel = threading.Event()
        self.video: Path | None = None

    @property
    def busy(self) -> bool:
        return self._thread is not None

    def start(self, video: Path, client: str | None, options: QCOptions, reports_dir: Path | None = None,
              shared_reports: Path | None = None) -> None:
        if self.busy:
            raise RuntimeError("a check is already running")
        self.video = video
        self._cancel = threading.Event()
        self._thread = QThread()
        self._job = _Job(video, client, options, reports_dir, self._cancel, shared_reports)
        self._job.moveToThread(self._thread)
        self._thread.started.connect(self._job.run)
        self._job.progress.connect(self.progress)
        self._job.warning.connect(self.warning)
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
        self.idle.emit()
