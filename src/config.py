"""Load config.yaml and per-client overrides from clients/<CLIENT>.yaml."""

from __future__ import annotations

import copy
import re
import sys
import warnings
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config.yaml"

# Files the user may edit. A packaged app (PyInstaller) can't write inside itself, so on its first
# run they're copied from the bundle into the OS data folder and read from there; the cache,
# models and reports paths in config.yaml then also land in that folder.
EDITABLE_FILES = ("config.yaml", "rules.yaml", "glossary.txt", "extra_words.txt", "ignore.yaml", "color.yaml",
                  "clients", "data/examples.csv")


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def user_data_root() -> Path:
    """Where a packaged app keeps its config, cache and models:
    Windows %LOCALAPPDATA%\\VideoQCBot\\data, macOS ~/Library/Application Support/VideoQCBot/data."""
    import platformdirs

    return platformdirs.user_data_path("VideoQCBot", appauthor=False) / "data"


def install_user_files(bundle: Path, target: Path) -> list[str]:
    """Copy each editable file or folder from the bundle that isn't in `target` yet. Never
    overwrites, so your edits survive updates. Returns what was copied."""
    import shutil

    copied = []
    for name in EDITABLE_FILES:
        src, dst = bundle / name, target / name
        if dst.exists() or not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        copied.append(name)
    return copied


def default_config_path() -> Path:
    """config.yaml next to src/ when run from source; in the OS data folder in a packaged app."""
    if not is_frozen():
        return DEFAULT_CONFIG
    target = user_data_root()
    install_user_files(Path(getattr(sys, "_MEIPASS", PROJECT_ROOT)), target)
    return target / "config.yaml"

_BARE_CLIENT_CODE = re.compile(r"\d+[A-Z]+")


def normalize_client_code(code: str | None) -> str | None:
    """Make client codes consistent: "2CS" and "e2cs" both become "E2CS".

    Task names write the same client with or without the leading "E". Codes that don't
    follow the E<number><letters> pattern ("S_UP", "disc_Echo") are only upper-cased.
    """
    if code is None:
        return None
    cleaned = code.strip().lstrip("#").upper()
    if not cleaned:
        return None
    if _BARE_CLIENT_CODE.fullmatch(cleaned):
        cleaned = "E" + cleaned
    return cleaned


def load_yaml(path: Path) -> dict:
    with Path(path).open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}


def load_config(path: Path | None = None) -> dict:
    """Load config.yaml. Relative paths inside it are resolved against the file's folder.

    In a packaged app the user's copy (in the OS data folder) is laid over the config.yaml the app
    ships with: their values win, and settings added since their copy was made come from the new
    one. Without this an installed app would never get a new setting (the copy is never replaced,
    so edits survive updates)."""
    path = Path(path) if path else default_config_path()
    config = load_yaml(path)
    shipped = Path(getattr(sys, "_MEIPASS", "")) / "config.yaml" if is_frozen() else None
    if shipped is not None and shipped.is_file() and shipped.resolve() != path.resolve():
        config = deep_merge(load_yaml(shipped), config)
    config["_root"] = path.resolve().parent
    apply_color_file(config)
    return config


def apply_color_file(config: dict) -> None:
    """Read color.yaml (paths.color_file) into config["color"] (its "defaults:") and
    config["_color_clients"] (its "clients:", by normalized code). The file's values win over an
    older "color:" block in config.yaml, which only fills keys the file doesn't have. A packaged
    app lays the user's copy over the color.yaml it ships with, as for config.yaml."""
    name = (config.get("paths") or {}).get("color_file")
    if not name:
        return
    path = Path(config["_root"]) / name
    data = load_yaml(path) if path.is_file() else {}
    shipped = Path(getattr(sys, "_MEIPASS", "")) / "color.yaml" if is_frozen() else None
    if shipped is not None and shipped.is_file() and shipped.resolve() != path.resolve():
        data = deep_merge(load_yaml(shipped), data)
    config["color"] = deep_merge(config.get("color") or {}, data.get("defaults") or {})
    config["_color_clients"] = {normalize_client_code(str(code)): block or {}
                                for code, block in (data.get("clients") or {}).items()}


def resolve_path(config: dict, key: str) -> Path:
    """Absolute path for an entry under config["paths"]."""
    return Path(config["_root"]) / Path(config["paths"][key])


def load_client(config: dict, code: str | None) -> dict:
    """Load clients/<CODE>.yaml. A missing file is a warning, not an error: defaults apply."""
    code = normalize_client_code(code)
    if code is None:
        return {"client": None, "overrides": {}}
    path = resolve_path(config, "clients_dir") / f"{code}.yaml"
    if not path.exists():
        warnings.warn(f"No client file {path.name}; using default rules for {code}.", stacklevel=2)
        return {"client": code, "overrides": {}}
    client = load_yaml(path)
    client.setdefault("client", code)
    client["overrides"] = client.get("overrides") or {}
    return client


def deep_merge(base: dict, override: dict) -> dict:
    """Return base updated by override. Nested dicts merge; lists and scalars are replaced."""
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def check_settings(config: dict, client: dict, check_name: str) -> dict:
    """Settings for one check: config.yaml defaults with the client's overrides applied."""
    base = config.get("checks", {}).get(check_name, {})
    override = (client or {}).get("overrides", {}).get(check_name, {})
    return deep_merge(base, override)


def color_settings(config: dict, client: dict) -> dict:
    """The color defaults (color.yaml "defaults:"), then the client's block under color.yaml
    "clients:", then an older "color:" block in the client's own file."""
    code = normalize_client_code((client or {}).get("client"))
    merged = deep_merge(config.get("color", {}), (config.get("_color_clients") or {}).get(code) or {})
    return deep_merge(merged, (client or {}).get("color") or {})


def ocr_settings(config: dict, client: dict) -> dict:
    """Settings for reading burned-in captions: config.yaml "ocr:" with the client's "ocr:" block applied.
    Unlike check overrides, a client's region or ignore lists sit directly under "ocr:" in its file."""
    return deep_merge(config.get("ocr", {}), (client or {}).get("ocr") or {})
