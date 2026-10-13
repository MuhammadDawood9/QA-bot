"""Writing files other people (and other PCs) read: never half a file, a backup of the old one,
and a line in the changelog saying who changed what.

    atomic_write_text(path, text)               temp file in the same folder, then one rename
    backup(path, backups_dir, user)             a dated copy of the current file, before changing it
    append_changelog(changelog, entry)          one CSV row: when, who, which PC, what changed

Everything is UTF-8 with "\\n" line ends, so the same file reads the same on Windows and macOS.
"""

from __future__ import annotations

import csv
import datetime as dt
import getpass
import os
import platform
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

CHANGELOG_COLUMNS = ["time", "user", "computer", "file", "client", "action", "changes"]


def current_user() -> str:
    """The OS login name ("dawood"); "unknown" if the OS won't say."""
    try:
        return getpass.getuser() or "unknown"
    except Exception:                       # no login name in the environment (some services)
        return "unknown"


def computer_name() -> str:
    return platform.node() or "unknown"


def _safe_part(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "x"


def atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` so a reader sees the old file or the new one, never half of it:
    a temp file in the same folder (same drive, so the rename is atomic), flushed to disk, then
    os.replace. Works on local disks, SMB network drives and synced (OneDrive/SharePoint) folders."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        _replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _replace(src: str, dst: Path, attempts: int = 10) -> None:
    """os.replace, retried briefly: on Windows a reader (antivirus, a sync client, another PC
    holding the file open) can make the rename fail for a moment."""
    import time

    for n in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if n == attempts - 1:
                raise
            time.sleep(0.1 * (n + 1))


def backup(path: Path, backups_dir: Path, user: str | None = None, keep: int = 50) -> Path | None:
    """Copy `path` to backups_dir/<name>_<date-time>_<user><suffix> before it's changed. Keeps the
    newest `keep` backups of that file. None if there's nothing to back up yet."""
    path = Path(path)
    if not path.is_file():
        return None
    backups_dir = Path(backups_dir)
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    target = backups_dir / f"{path.stem}_{stamp}_{_safe_part(user or current_user())}{path.suffix}"
    shutil.copy2(path, target)
    old = sorted(backups_dir.glob(f"{path.stem}_*{path.suffix}"), key=lambda p: p.name)
    for extra in old[:-keep] if keep > 0 else []:
        try:
            extra.unlink()
        except OSError:
            pass
    return target


@dataclass
class ChangeEntry:
    file: str
    action: str                      # "add", "edit", "restore"...
    changes: str                     # one line: what changed
    client: str = ""
    user: str = field(default_factory=current_user)
    computer: str = field(default_factory=computer_name)
    time: str = field(default_factory=lambda: dt.datetime.now().isoformat(timespec="seconds"))

    def row(self) -> list[str]:
        return [self.time, self.user, self.computer, self.file, self.client, self.action, self.changes]


def append_changelog(changelog: Path, entry: ChangeEntry) -> None:
    """One row at the end of the changelog CSV (created with a header the first time). Appending
    is a single small write, which is as safe as a shared file gets without a server; the caller
    holds the data lock while it runs (src/shared.py)."""
    changelog = Path(changelog)
    changelog.parent.mkdir(parents=True, exist_ok=True)
    new = not changelog.is_file() or changelog.stat().st_size == 0
    with changelog.open("a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        if new:
            writer.writerow(CHANGELOG_COLUMNS)
        writer.writerow(entry.row())


def read_changelog(changelog: Path) -> list[dict]:
    if not Path(changelog).is_file():
        return []
    with Path(changelog).open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))
