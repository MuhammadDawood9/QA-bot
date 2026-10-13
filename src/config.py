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


LOCAL_NAME = "local.yaml"      # this PC's own settings (machine: block), laid over everything else
# Paths that are per user and per PC (caches, downloaded models): under the local folder even when
# the rules come from the shared folder.
LOCAL_PATH_KEYS = ("llm_cache_file", "transcript_cache_dir", "ocr_cache_dir", "color_cache_dir", "audio_cache_dir",
                   "models_dir")
# Shared files that, when missing from the data folder, are read from the app's own defaults.
DATA_FILE_KEYS = ("rules_file", "glossary_file", "extra_words_file", "examples_file", "color_file")


def load_config(path: Path | None = None) -> dict:
    """Load config.yaml. Relative paths inside it are resolved against the file's folder.

    With no path: the floor setup decides (src/shared.py). With a shared folder, the app's own
    config.yaml is laid under the shared one (from the cached copy), and caches and models stay
    in the user's local folder. Alone, as before: config.yaml next to src/, or in a packaged app the
    user's copy (in the OS data folder) laid over the config.yaml the app ships with, so settings
    added since their copy was made still arrive. In both cases this PC's local.yaml comes last."""
    floor = None
    if path is None:
        from src.shared import current_floor

        floor = current_floor()
        if floor.shared:
            return _finish(_shared_config(floor), floor)
        path = default_config_path()
    path = Path(path)
    config = load_yaml(path)
    shipped = Path(getattr(sys, "_MEIPASS", "")) / "config.yaml" if is_frozen() else None
    if shipped is not None and shipped.is_file() and shipped.resolve() != path.resolve():
        config = deep_merge(load_yaml(shipped), config)
    config["_root"] = path.resolve().parent
    if is_frozen():
        config["_app_root"] = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT))
    return _finish(config, floor)


def _shared_config(floor) -> dict:
    from src.shared import app_dir

    shipped = app_dir() / "config.yaml"
    config = load_yaml(shipped) if shipped.is_file() else {}
    on_floor = floor.data_root / "config.yaml"
    if on_floor.is_file():
        config = deep_merge(config, load_yaml(on_floor))
    config["_root"] = floor.data_root
    config["_local_root"] = floor.local_dir
    config["_app_root"] = app_dir()
    config["_shared"] = True
    if floor.setup.shared_models is not None:
        config["_shared_models"] = floor.setup.shared_models
    return config


def _finish(config: dict, floor) -> dict:
    """What every load does last: this PC's local.yaml, the floor-wide settings, color.yaml, and
    "auto" machine settings turned into numbers."""
    from src.machine import apply_machine_defaults
    from src.shared import shared_settings

    if floor is not None:
        # Alone on a PC the data folder is this user's own, so local.yaml sits there; on the floor
        # it's in the local folder (_local_root, set by _shared_config).
        local = Path(config.get("_local_root") or config["_root"]) / LOCAL_NAME
        if local.is_file():
            config = deep_merge(config, load_yaml(local))
    shared = shared_settings(Path(config["_root"]))
    config["_shared_settings"] = shared
    if shared.get("client_import"):
        config["client_import"] = deep_merge(config.get("client_import") or {}, shared["client_import"])
    apply_color_file(config)
    apply_machine_defaults(config)
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
    shipped = Path(config["_app_root"]) / "color.yaml" if config.get("_app_root") else None
    if shipped is not None and shipped.is_file() and shipped.resolve() != path.resolve():
        data = deep_merge(load_yaml(shipped), data)
    config["color"] = deep_merge(config.get("color") or {}, data.get("defaults") or {})
    config["_color_clients"] = {normalize_client_code(str(code)): block or {}
                                for code, block in (data.get("clients") or {}).items()}


def resolve_path(config: dict, key: str) -> Path:
    """Absolute path for an entry under config["paths"]: caches and models in the local folder,
    everything else in the data folder (falling back to the app's own copy of a shared file that
    the data folder doesn't have)."""
    relative = Path(config["paths"][key])
    if key in LOCAL_PATH_KEYS and config.get("_local_root"):
        return Path(config["_local_root"]) / relative
    path = Path(config["_root"]) / relative
    if key in DATA_FILE_KEYS and config.get("_app_root") and not path.exists():
        fallback = Path(config["_app_root"]) / relative
        if fallback.exists():
            return fallback
    return path


def model_dirs(config: dict) -> list[Path]:
    """Where models are looked for: the shared read-only models folder first (when there is one),
    then this user's own models folder (where a missing model is downloaded)."""
    dirs = []
    shared = config.get("_shared_models")
    if shared:
        dirs.append(Path(shared))
    dirs.append(resolve_path(config, "models_dir"))
    return dirs


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


def save_local_settings(local_dir: Path, updates: dict) -> Path:
    """Merge `updates` into this PC's local.yaml (e.g. {"machine": {"whisper_model_size": "small"}});
    a value of None removes that setting. Returns the file."""
    from src.utils.safe_io import atomic_write_text

    path = Path(local_dir) / LOCAL_NAME
    data = load_yaml(path) if path.is_file() else {}

    def merge(target: dict, changes: dict) -> None:
        for key, value in changes.items():
            if isinstance(value, dict):
                merge(target.setdefault(key, {}), value)
                if not target[key]:
                    target.pop(key)
            elif value is None:
                target.pop(key, None)
            else:
                target[key] = value

    merge(data, updates)
    atomic_write_text(path, "# This PC's own Video QC Bot settings (set from the app's Settings). Laid over\n"
                            "# config.yaml, including a shared one.\n"
                      + yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
    return path
