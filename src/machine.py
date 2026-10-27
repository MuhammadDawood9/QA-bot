"""Being gentle on shared work PCs (they also edit and QC video):

- threads: "auto" = about half the cores, for Whisper, the OCR and OpenCV
- the Whisper model: "auto" = picked from the CPU and memory (manual override in config or Settings)
- one check at a time per PC, across every user and every copy of the app (JobLock)
- low memory or disk space: a warning before a check starts
- below-normal CPU priority: src/platform (lower_priority), called by the app

    apply_machine_defaults(config)        # turns "auto" into numbers; load_config calls it
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Callable, Iterable

log = logging.getLogger("qcbot.machine")

GB = 1024 ** 3
MIN_FREE_DISK_GB = 2.0          # a check writes audio, frames and a report: warn below this
MIN_FREE_RAM_GB = 1.5           # Whisper "small" alone takes about 1 GB


def cpu_count() -> int:
    return os.cpu_count() or 2


def half_the_cores(cores: int | None = None) -> int:
    return max(1, (cores or cpu_count()) // 2)


def ram_gb() -> float | None:
    from src.platform import current

    total = current.total_ram_bytes()
    return round(total / GB, 1) if total else None


def choose_whisper_model(cores: int, ram: float | None) -> str:
    """The model a PC can run without slowing everything else down. "small" catches clearly more
    wrong words than "base" but is ~3x slower; on CPU "medium" is too slow for a QC floor.
        under 4 cores or under 8 GB   -> base
        otherwise                     -> small"""
    if cores < 4 or (ram is not None and ram < 8):
        return "base"
    return "small"


def apply_machine_defaults(config: dict) -> dict:
    """Replace "auto" (or a missing value) in machine.cpu_threads, ocr.cpu_threads and
    machine.whisper_model_size by what suits this PC. What was chosen is kept in config["_machine"]
    for the diagnostics."""
    machine = config.setdefault("machine", {})
    cores, ram = cpu_count(), None
    chosen = {"cores": cores}
    if str(machine.get("cpu_threads", "auto")).lower() in ("auto", "none", ""):
        machine["cpu_threads"] = half_the_cores(cores)
        chosen["cpu_threads"] = "auto"
    ocr = config.get("ocr")
    if isinstance(ocr, dict) and str(ocr.get("cpu_threads", "")).lower() == "auto":
        ocr["cpu_threads"] = half_the_cores(cores)
    if str(machine.get("whisper_model_size", "auto")).lower() in ("auto", "none", ""):
        ram = ram_gb()
        machine["whisper_model_size"] = choose_whisper_model(cores, ram)
        chosen["whisper_model_size"] = "auto"
    chosen["ram_gb"] = ram if ram is not None else ram_gb()
    config["_machine"] = chosen
    from src.utils.system import set_ffmpeg_timeout

    set_ffmpeg_timeout(machine.get("ffmpeg_timeout_sec"))
    return config


def resource_warnings(folders: Iterable[Path], min_disk_gb: float = MIN_FREE_DISK_GB,
                      min_ram_gb: float = MIN_FREE_RAM_GB) -> list[str]:
    """Plain-language warnings about low free disk space (on each folder's drive) and memory."""
    from src.platform import current

    out, seen = [], set()
    for folder in folders:
        folder = Path(folder)
        probe = next((p for p in [folder, *folder.parents] if p.exists()), None)
        if probe is None:
            continue
        try:
            usage = shutil.disk_usage(probe)
        except OSError:
            continue
        key = (usage.total, probe.anchor)
        if key in seen:
            continue
        seen.add(key)
        if usage.free < min_disk_gb * GB:
            out.append(f"Only {usage.free / GB:.1f} GB free on {probe.anchor or probe}: the check may fail. "
                       f"Free some space (at least {min_disk_gb:g} GB).")
    available = current.available_ram_bytes()
    if available is not None and available < min_ram_gb * GB:
        out.append(f"Only {available / GB:.1f} GB of memory free: the check will be slow and may slow down "
                   f"other programs. Close what you don't need.")
    return out


# --- one check at a time per PC ----------------------------------------------------------------------

class JobLock:
    """A lock file in a folder every user of the PC can write (src/platform machine_dir). It holds
    the owner's process id; a lock left by a process that has ended is taken over.

        lock = JobLock()
        if lock.acquire(wait=..., should_stop=cancel.is_set):   # waits while another check runs
            try: ...
            finally: lock.release()
    """

    def __init__(self, folder: Path | None = None, name: str = "check.lock"):
        if folder is None:
            from src.platform import current

            folder = os.environ.get("VIDEOQCBOT_MACHINE_DIR") or current.machine_dir()
        self.path = Path(folder) / name
        self.held = False

    def owner(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _stale(self, info: dict | None) -> bool:
        from src.platform import current

        if info is None:                           # half-written or unreadable: stale after a minute
            try:
                return time.time() - self.path.stat().st_mtime > 60
            except OSError:
                return True
        return not current.pid_alive(int(info.get("pid", 0)))

    def try_acquire(self) -> bool:
        from src.utils.safe_io import computer_name, current_user

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("no machine-wide lock folder (%s); checks aren't coordinated between users", exc)
            self.held = True
            return True
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
            except FileExistsError:
                if self._stale(self.owner()):
                    try:
                        self.path.unlink()
                        log.info("took over a check lock left by a process that has ended")
                    except OSError:
                        return False
                    continue
                return False
            except OSError as exc:                 # no permission on the shared machine folder
                log.warning("can't create %s (%s); checks aren't coordinated between users", self.path, exc)
                self.held = True
                return True
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "user": current_user(), "computer": computer_name(),
                           "since": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
            self.held = True
            return True
        return False

    def acquire(self, wait: Callable[[dict | None], None] | None = None,
                should_stop: Callable[[], bool] | None = None, poll_sec: float = 2.0) -> bool:
        """Block until the lock is ours (True), or should_stop() says so (False). wait(owner) is
        called each time round while someone else holds it."""
        while not self.try_acquire():
            if should_stop is not None and should_stop():
                return False
            if wait is not None:
                wait(self.owner())
            time.sleep(poll_sec)
        return True

    def release(self) -> None:
        if not self.held:
            return
        self.held = False
        info = self.owner()
        if info is None or info.get("pid") == os.getpid():
            try:
                self.path.unlink()
            except OSError:
                pass
