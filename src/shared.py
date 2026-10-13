"""Many PCs on the QC floor: one SHARED data folder, a local cached copy of it, safe shared writes,
and editor roles.

Three places, kept apart:
  (a) the installed app: read-only. Ships the default config.yaml, color.yaml, rules.yaml... (app_dir)
  (b) SHARED data: clients, glossaries, rules, ignore lists, color settings, shared_settings.yaml,
      latest.json, backups and changelog.csv. A network drive (\\\\server\\share, /Volumes/QC) or a
      synced SharePoint / OneDrive folder.
  (c) PER-USER local data: settings, reports, caches, logs, downloaded models, and shared_cache/,
      the copy of (b) every check reads from. If the shared folder can't be reached, checks run
      from that copy and the app says "offline, using cached rules".

Where the shared folder is (first match wins):
  1. videoqcbot-bootstrap.yaml with "force: true", from IT (src/platform bootstrap_dirs)
  2. the user's own choice from the first-run setup screen (floor.yaml in the user config folder)
  3. videoqcbot-bootstrap.yaml from IT
  4. none: the app works alone on this PC, as before

    floor = current_floor()          # what's configured; floor.sync() refreshes the cached copy
    with floor.lock():                # the shared data lock: one writer at a time, every PC
        write_shared_text(floor, "ignore.yaml", text, "edit", "3 rules added")
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger("qcbot.shared")

APP_NAME = "VideoQCBot"
BOOTSTRAP_NAME = "videoqcbot-bootstrap.yaml"
SHARED_SETTINGS = "shared_settings.yaml"
LATEST_JSON = "latest.json"
# What the cached copy holds: everything a check reads. Not the backups, changelog or imported sources.
SHARED_FILES = ("config.yaml", "color.yaml", "rules.yaml", "glossary.txt", "extra_words.txt", "ignore.yaml",
                SHARED_SETTINGS, LATEST_JSON, "data/examples.csv")
SHARED_FOLDERS = ("clients",)                       # their *.yaml files
REACH_TIMEOUT_SEC = 4.0                             # a network drive that's gone can hang for a minute

SHARED_SETTINGS_TEXT = """\
# Floor-wide settings for Video QC Bot, in the shared data folder. Only editors can change them.
#
# Editors may add and edit clients, the glossary, the ignore list and these settings. Everyone can
# run checks. This is a convenience to stop accidental edits, NOT security: anyone who can write to
# this folder can change these files directly. Use folder permissions for real control (read for
# everyone, write for editors: docs/IT_DEPLOYMENT.md).
editors: []                  # OS login names that are editors automatically, e.g. ["dawood", "sara"]
editor_pin: null             # set from the app (Settings > Set editor PIN...): stored hashed, never the PIN

client_import:
  allow_claude: false        # "Read the PDF with Claude" in Add client (also needs ANTHROPIC_API_KEY on that PC)

reports:
  copy_to_shared: false      # also copy every Word report to the shared reports folder:
                             # <reports>/<client>/<date>_<user>_<video>.docx
"""


# --- where things are ------------------------------------------------------------------------------

def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """The installed app's own (read-only) files: the bundle when packaged, the project when not."""
    from src.config import PROJECT_ROOT

    return Path(getattr(sys, "_MEIPASS", PROJECT_ROOT)) if is_frozen() else PROJECT_ROOT


def user_local_dir() -> Path:
    """Per-user data: Windows %LOCALAPPDATA%\\VideoQCBot\\data, macOS ~/Library/Application Support/VideoQCBot/data."""
    from src.config import user_data_root

    return user_data_root()


def user_config_dir() -> Path:
    import platformdirs

    return platformdirs.user_config_path(APP_NAME, appauthor=False, roaming=False)


def _path(value) -> Path | None:
    """A path from a settings file: ~ and environment variables (%OneDrive%, $HOME) expanded."""
    if value in (None, ""):
        return None
    return Path(os.path.expandvars(os.path.expanduser(str(value))))


@dataclass
class FloorSetup:
    shared_data: Path | None = None
    shared_models: Path | None = None
    shared_reports: Path | None = None
    source: str = "this PC only"
    alone: bool = False                    # the user chose "work alone" in the setup screen
    bootstrap: Path | None = None          # the bootstrap file found, if any

    @property
    def needs_setup(self) -> bool:
        """Show the first-run setup screen: nothing configured and nothing chosen yet."""
        return self.shared_data is None and not self.alone and self.bootstrap is None


def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("couldn't read %s: %s", path, exc)
        return {}


def find_bootstrap(executable: Path | None = None, frozen: bool | None = None) -> Path | None:
    from src.platform import current

    frozen = is_frozen() if frozen is None else frozen
    for folder in current.bootstrap_dirs(Path(executable or sys.executable), frozen):
        candidate = folder / BOOTSTRAP_NAME
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def read_setup(bootstrap: Path | None = None, user_file: Path | None = None) -> FloorSetup:
    bootstrap = bootstrap if bootstrap is not None else find_bootstrap()
    user_file = user_file or user_config_dir() / "floor.yaml"
    boot = _read_yaml(bootstrap) if bootstrap else {}
    mine = _read_yaml(user_file) if user_file.is_file() else {}

    def from_boot(source: str) -> FloorSetup:
        shared = _path(boot.get("shared_data"))
        return FloorSetup(shared_data=shared,
                          shared_models=_path(boot.get("shared_models")) or (shared / "models" if shared else None),
                          shared_reports=_path(boot.get("shared_reports")) or (shared / "reports" if shared else None),
                          source=source, bootstrap=bootstrap)

    if boot and boot.get("force"):
        return from_boot(f"set by IT ({bootstrap})")
    if mine.get("alone"):
        return FloorSetup(source="this PC only (chosen in setup)", alone=True, bootstrap=bootstrap)
    if mine.get("shared_data"):
        shared = _path(mine["shared_data"])
        return FloorSetup(shared_data=shared, shared_models=_path(mine.get("shared_models")) or shared / "models",
                          shared_reports=_path(mine.get("shared_reports")) or shared / "reports",
                          source=f"chosen on this PC ({user_file})", bootstrap=bootstrap)
    if boot:
        return from_boot(f"set by IT ({bootstrap})")
    return FloorSetup()


def save_user_setup(shared_data: Path | None, alone: bool = False, user_file: Path | None = None) -> None:
    """What the setup screen (or Settings) chose, for this user on this PC."""
    from src.utils.safe_io import atomic_write_text

    user_file = user_file or user_config_dir() / "floor.yaml"
    data = {"shared_data": str(shared_data) if shared_data else None, "alone": bool(alone)}
    atomic_write_text(user_file, "# Video QC Bot: where the shared data folder is (set in the app).\n"
                      + yaml.safe_dump(data, allow_unicode=True, sort_keys=False))


# --- the floor ------------------------------------------------------------------------------------

class LockBusy(RuntimeError):
    """Someone else is saving to the shared folder right now. The message is for the user."""


class SharedUnavailable(RuntimeError):
    """The shared folder can't be reached, so nothing can be saved to it. The message is for the user."""


def _reachable(path: Path, timeout: float) -> bool:
    """path.is_dir(), but giving up after `timeout` s: a disconnected network drive can block for a minute."""
    result = {"ok": False}

    def probe():
        try:
            result["ok"] = path.is_dir()
        except OSError:
            result["ok"] = False

    t = threading.Thread(target=probe, daemon=True)
    t.start()
    t.join(timeout)
    return result["ok"]


@dataclass
class Floor:
    setup: FloorSetup = field(default_factory=FloorSetup)
    local_dir: Path | None = None          # per-user data; default user_local_dir() (the project, when alone from source)
    online: bool | None = None             # None until the first sync
    last_sync: float | None = None         # time.time() of the last successful sync
    last_error: str = ""

    def __post_init__(self) -> None:
        if self.local_dir is None:
            self.local_dir = user_local_dir() if (self.shared or is_frozen()) else app_dir()
        self.local_dir = Path(self.local_dir)

    @property
    def shared(self) -> bool:
        return self.setup.shared_data is not None

    @property
    def shared_dir(self) -> Path | None:
        return self.setup.shared_data

    @property
    def cache_dir(self) -> Path:
        return self.local_dir / "shared_cache"

    @property
    def data_root(self) -> Path:
        """Where checks read clients, glossary, rules... from."""
        if self.shared:
            return self.cache_dir
        return self.local_dir

    # --- the cached copy -------------------------------------------------------------------------

    def reachable(self, timeout: float = REACH_TIMEOUT_SEC) -> bool:
        return self.shared and _reachable(self.shared_dir, timeout)

    def sync(self, timeout: float = REACH_TIMEOUT_SEC) -> bool:
        """Refresh the cached copy from the shared folder (only files that changed). Returns
        whether the shared folder was reached. Offline, the copy is left as it is."""
        if not self.shared:
            return True
        if not self.reachable(timeout):
            if self.online is not False:
                log.warning("shared folder %s can't be reached: using the cached rules", self.shared_dir)
            self.online, self.last_error = False, f"{self.shared_dir} can't be reached"
            return False
        try:
            copied = mirror(self.shared_dir, self.cache_dir)
        except OSError as exc:
            log.warning("couldn't refresh the cached rules from %s: %s", self.shared_dir, exc)
            self.online, self.last_error = False, str(exc)
            return False
        if copied:
            log.info("cached rules refreshed from the shared folder: %s", ", ".join(copied[:10]))
        self.online, self.last_sync, self.last_error = True, time.time(), ""
        try:
            (self.cache_dir / ".synced.json").write_text(json.dumps(
                {"time": dt.datetime.now().isoformat(timespec="seconds"), "from": str(self.shared_dir)}),
                encoding="utf-8")
        except OSError:
            pass
        return True

    def cache_time(self) -> str | None:
        """When the cached copy was last refreshed ("2026-10-13 14:02"), from this or an earlier session."""
        try:
            data = json.loads((self.cache_dir / ".synced.json").read_text(encoding="utf-8"))
            return str(data.get("time", "")).replace("T", " ")[:16] or None
        except (OSError, ValueError):
            return None

    def status_text(self) -> str:
        if not self.shared:
            return "Rules: on this PC only"
        if self.online is False:
            when = self.cache_time()
            return f"Offline, using cached rules{f' from {when}' if when else ''}"
        return f"Rules: shared folder {self.shared_dir}"

    # --- writing ---------------------------------------------------------------------------------

    @property
    def write_root(self) -> Path:
        """Where saves go: the shared folder (must be reachable), or the local data when alone."""
        if not self.shared:
            return self.data_root
        if not self.reachable():
            raise SharedUnavailable(f"The shared folder ({self.shared_dir}) can't be reached, so nothing can be "
                                    f"saved now. Checks still run with the cached rules.")
        return self.shared_dir

    def lock(self, timeout: float = 20.0):
        root = self.write_root
        return DataLock(root / ".locks" / "data.lock", timeout=timeout)


def mirror(src_root: Path, dst_root: Path) -> list[str]:
    """Copy each shared file that's new or changed (size or modified time) into the cached copy,
    and drop cached client files deleted from the shared folder. Returns the names copied."""
    copied = []
    names = list(SHARED_FILES)
    for folder in SHARED_FOLDERS:
        if not (src_root / folder).is_dir():         # missing for a moment (a sync client): keep the copy
            continue
        names += [f"{folder}/{p.name}" for p in sorted((src_root / folder).glob("*.yaml"))]
        cached = dst_root / folder
        if cached.is_dir():
            for p in cached.glob("*.yaml"):
                if not (src_root / folder / p.name).is_file():
                    p.unlink()
                    copied.append(f"-{folder}/{p.name}")
    for name in names:
        src, dst = src_root / name, dst_root / name
        if not src.is_file():
            if dst.is_file() and name in SHARED_FILES:
                dst.unlink()                                 # removed from the shared folder
                copied.append(f"-{name}")
            continue
        s = src.stat()
        if dst.is_file():
            d = dst.stat()
            if d.st_size == s.st_size and int(d.st_mtime) == int(s.st_mtime):
                continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f".{dst.name}.sync")
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        copied.append(name)
    return copied


def seed_shared(shared_dir: Path) -> list[str]:
    """Fill an empty shared folder with the app's starting files (never overwrites)."""
    from src.config import install_user_files

    shared_dir.mkdir(parents=True, exist_ok=True)
    copied = install_user_files(app_dir(), shared_dir)
    settings = shared_dir / SHARED_SETTINGS
    if not settings.exists():
        from src.utils.safe_io import atomic_write_text

        atomic_write_text(settings, SHARED_SETTINGS_TEXT)
        copied.append(SHARED_SETTINGS)
    for folder in ("clients", "models", "reports", "backups", "installers"):
        (shared_dir / folder).mkdir(exist_ok=True)
    return copied


def looks_empty(folder: Path) -> bool:
    return not any((folder / name).exists() for name in ("config.yaml", "clients", SHARED_SETTINGS))


# --- the shared data lock -------------------------------------------------------------------------------

class DataLock:
    """One writer at a time, across every PC: a lock file created with O_EXCL (atomic on local
    disks and SMB shares). It records who holds it; one older than `stale_after` seconds (a PC that
    crashed while saving) is taken over. On a synced OneDrive/SharePoint folder the file only
    reaches other PCs after a sync, so two editors saving at the same moment can still both win:
    the backups and the changelog are the safety net there (docs/IT_DEPLOYMENT.md)."""

    def __init__(self, path: Path, timeout: float = 20.0, stale_after: float = 120.0):
        self.path, self.timeout, self.stale_after = Path(path), timeout, stale_after
        self.held = False

    def holder(self) -> str:
        try:
            info = json.loads(self.path.read_text(encoding="utf-8"))
            return f"{info.get('user', '?')} on {info.get('computer', '?')}"
        except (OSError, ValueError):
            return "someone"

    def __enter__(self) -> "DataLock":
        from src.utils.safe_io import computer_name, current_user

        self.path.parent.mkdir(parents=True, exist_ok=True)
        end = time.monotonic() + self.timeout
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({"user": current_user(), "computer": computer_name(), "pid": os.getpid(),
                               "time": dt.datetime.now().isoformat(timespec="seconds")}, f)
                self.held = True
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    continue                                   # just released: try again
                if age > self.stale_after:
                    log.warning("taking over a shared lock held by %s for %.0f s", self.holder(), age)
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                if time.monotonic() > end:
                    raise LockBusy(f"{self.holder()} is saving changes to the shared rules. Try again in a "
                                   f"moment.") from None
                time.sleep(0.25)

    def __exit__(self, *exc) -> None:
        if self.held:
            self.held = False
            try:
                self.path.unlink()
            except OSError:
                pass


def write_shared_text(floor: Floor, name: str, text: str, action: str, changes: str,
                      expected_mtime: float | None = None) -> None:
    """Save one shared file: lock, refuse if someone changed it since it was opened
    (expected_mtime), back up the old one, write atomically, log, refresh the cached copy."""
    from src.utils.safe_io import ChangeEntry, append_changelog, atomic_write_text, backup

    root = floor.write_root
    with floor.lock():
        path = root / name
        if expected_mtime is not None and path.is_file() and abs(path.stat().st_mtime - expected_mtime) > 0.001:
            raise LockBusy(f"{name} was changed by someone else since you opened it. Close this, open it again "
                           f"and redo your change.")
        backup(path, root / "backups" / Path(name).stem)
        atomic_write_text(path, text)
        append_changelog(root / "changelog.csv", ChangeEntry(file=name, action=action, changes=changes))
    floor.sync()


# --- shared settings and roles ---------------------------------------------------------------------------

def shared_settings(data_root: Path) -> dict:
    path = Path(data_root) / SHARED_SETTINGS
    data = _read_yaml(path) if path.is_file() else {}
    data.setdefault("editors", [])
    data.setdefault("editor_pin", None)
    return data


PIN_ITERATIONS = 200_000


def hash_pin(pin: str, salt: bytes | None = None, iterations: int = PIN_ITERATIONS) -> str:
    """PBKDF2-SHA256, salted: "pbkdf2_sha256$<iterations>$<salt>$<hash>" (base64). The PIN itself is never stored."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, iterations)
    b64 = lambda b: base64.b64encode(b).decode("ascii")      # noqa: E731
    return f"pbkdf2_sha256${iterations}${b64(salt)}${b64(digest)}"


def verify_pin(pin: str, stored: str | None) -> bool:
    try:
        scheme, iterations, salt, digest = str(stored).split("$")
        if scheme != "pbkdf2_sha256":
            return False
        expected = base64.b64decode(digest)
        got = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(got, expected)
    except (ValueError, TypeError):
        return False


@dataclass
class Role:
    editor: bool
    why: str


def role_for(floor: Floor, user: str, unlocked: bool = False) -> Role:
    """Who may edit clients and shared files. Alone on a PC: always. On the floor: a user in
    shared_settings.yaml "editors", or anyone who entered the editor PIN this session; when no
    editor and no PIN is set up yet, everyone (so the first editor can set it up)."""
    if not floor.shared:
        return Role(True, "rules on this PC only")
    settings = shared_settings(floor.data_root)
    editors = {str(e).casefold() for e in settings.get("editors") or []}
    if user.casefold() in editors:
        return Role(True, f"{user} is in the editors list")
    if unlocked:
        return Role(True, "editor PIN entered")
    if not editors and not settings.get("editor_pin"):
        return Role(True, "no editors set up yet: everyone can edit")
    return Role(False, "view only: ask an editor, or enter the editor PIN")


def set_editor_pin(floor: Floor, pin: str) -> None:
    if len(pin) < 4:
        raise ValueError("The PIN needs at least 4 characters.")
    path = floor.write_root / SHARED_SETTINGS
    text = path.read_text(encoding="utf-8") if path.is_file() else SHARED_SETTINGS_TEXT
    data = yaml.safe_load(text) or {}
    data["editor_pin"] = hash_pin(pin)
    import re

    line = f"editor_pin: \"{data['editor_pin']}\"   # set from the app; stored hashed, never the PIN"
    new, n = re.subn(r"(?m)^editor_pin:.*$", lambda _m: line, text)
    if not n:
        new = text.rstrip("\n") + "\n" + line + "\n"
    write_shared_text(floor, SHARED_SETTINGS, new, "edit", "editor PIN changed")


# --- reports on the shared folder ----------------------------------------------------------------------------

def copy_report_to_shared(report: Path, reports_root: Path, client: str | None, user: str, video: Path | None,
                          extra_folder: Path | None = None) -> Path:
    """Copy a Word report to <reports_root>/<client>/<date>_<user>_<video>.docx (and a clips
    folder next to it, for audio reports). Never overwrites: a number is added instead."""
    import re

    def safe(text: str) -> str:
        return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text).strip(" ._") or "x"

    folder = Path(reports_root) / safe(client or "no-client")
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{dt.date.today():%Y-%m-%d}_{safe(user)}_{safe((video or report).stem)}"
    target, n = folder / f"{stem}.docx", 2
    while target.exists():
        target, n = folder / f"{stem}-{n}.docx", n + 1
    shutil.copy2(report, target)
    if extra_folder is not None and Path(extra_folder).is_dir():
        shutil.copytree(extra_folder, target.with_name(target.stem + "_" + Path(extra_folder).name))
    return target


# --- the floor this process uses ----------------------------------------------------------------------------------

_floor: Floor | None = None


def current_floor() -> Floor:
    global _floor
    if _floor is None:
        _floor = Floor(read_setup())
    return _floor


def set_floor(floor: Floor | None) -> None:
    """Use this floor from now on (after the setup screen, or in tests). None: read it again."""
    global _floor
    _floor = floor
