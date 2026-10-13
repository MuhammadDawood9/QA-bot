""""Copy diagnostics": everything support needs in one block of text, ready to paste in a message.

Version, OS, the machine, the floor setup (shared folder, online or not, editor or not), ffmpeg,
the models found, the app settings, and the end of the log. Never the API key: only whether one
is set.
"""

from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path


def _tail(path: Path, lines: int) -> list[str]:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [f"(log not readable: {exc})"]
    return text[-lines:]


def collect(config: dict, settings: dict | None = None, log_file: Path | None = None, role: str | None = None,
            log_lines: int = 150) -> str:
    from src.config import model_dirs, resolve_path
    from src.machine import cpu_count, ram_gb
    from src.platform import current
    from src.shared import current_floor
    from src.utils.safe_io import computer_name, current_user
    from src.utils.system import MissingToolError, ffmpeg_version, find_ffmpeg
    from src.version import __version__

    floor = current_floor()
    out = [f"Video QC Bot {__version__}", "=" * 40]
    out.append(f"OS: {platform.platform()} ({platform.machine()})")
    out.append(f"Python: {sys.version.split()[0]}, packaged: {bool(getattr(sys, 'frozen', False))}")
    out.append(f"Program: {sys.executable}")
    out.append(f"User: {current_user()} on {computer_name()}")
    available = current.available_ram_bytes()
    out.append(f"CPU cores: {cpu_count()}, memory: {ram_gb()} GB"
               + (f" ({available / 1024 ** 3:.1f} GB free)" if available else ""))
    machine = config.get("machine", {})
    out.append(f"Whisper model: {machine.get('whisper_model_size')}, threads: {machine.get('cpu_threads')}, "
               f"auto choices: {config.get('_machine')}")
    try:
        ff = find_ffmpeg(machine.get("ffmpeg_path"))
        out.append(f"ffmpeg: {ff} ({ffmpeg_version(machine.get('ffmpeg_path'))})")
    except (MissingToolError, OSError, Exception) as exc:      # diagnostics never fail
        out.append(f"ffmpeg: NOT FOUND ({str(exc).splitlines()[0]})")

    out += ["", "Floor setup", "-" * 40]
    out.append(f"Source: {floor.setup.source}")
    out.append(f"Shared data folder: {floor.shared_dir or '(none: this PC only)'}")
    if floor.shared:
        out.append(f"Status: {floor.status_text()} (last error: {floor.last_error or 'none'})")
        out.append(f"Cached copy: {floor.cache_dir} (refreshed {floor.cache_time() or 'never'})")
        out.append(f"Shared models: {floor.setup.shared_models}, shared reports: {floor.setup.shared_reports}")
    out.append(f"Data folder in use: {config.get('_root')}, local folder: {config.get('_local_root')}")
    out.append(f"Editor: {role or 'unknown'}")
    out.append(f"Clients folder: {resolve_path(config, 'clients_dir')}")
    for folder in model_dirs(config):
        try:
            found = sorted(p.name for p in Path(folder).iterdir())[:20] if Path(folder).is_dir() else ["(missing)"]
        except OSError as exc:
            found = [f"(unreadable: {exc})"]
        out.append(f"Models in {folder}: {', '.join(found) or '(empty)'}")
    out.append(f"ANTHROPIC_API_KEY set: {'yes' if os.environ.get('ANTHROPIC_API_KEY') else 'no'}; punctuation check "
               f"enabled: {bool((config.get('checks') or {}).get('punctuation', {}).get('enabled'))}")

    if settings is not None:
        out += ["", "App settings", "-" * 40, json.dumps(settings, indent=2, ensure_ascii=False, default=str)]
    if log_file is not None:
        out += ["", f"Last {log_lines} log lines ({log_file})", "-" * 40] + _tail(log_file, log_lines)
    return "\n".join(out) + "\n"
