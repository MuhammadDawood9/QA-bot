# CODING_STANDARDS.md

This document captures the coding conventions this repo already follows (derived from existing code). Use it as the baseline for reviews.

## Naming
- **Modules/functions/variables**: `snake_case`. Classes: `PascalCase`. Constants: `UPPER_SNAKE_CASE`.
- **Issue/check types**: string constants defined in `src/models.py` (e.g. `TOO_SHORT`, `PHRASE_BREAK`, `SPELLING`).
- **Config keys**: snake_case in YAML/JSON.

## Imports
- **Style**: absolute imports from `src.*` (e.g. `from src.checks.too_short import check`, `from src.models import Caption, Issue`).
- **Scripts importing from repo**: When a script in `scripts/` needs to import `src.*`, it does so by running from repo root or by manipulating `sys.path` (see `tests/test_audio_diagnostics.py` pattern). Prefer package imports where possible.
- **No circular imports**: Keep `src/engine.py` independent of UI; checks should be independent of each other where practical.
- **Lazy imports**: Some modules use local imports inside functions to avoid heavy dependencies at import time.

## Types & Data
- **Dataclasses**: Use `@dataclass` for core models (`src/models.py`: `Caption`, `Issue`, etc.). Prefer explicit fields.
- **Type hints**: Use type hints where helpful (e.g. `Sequence[Caption]`, `float | None`).
- **Serialization**: JSON for caches/configs; YAML via `pyyaml`. Handle UTF-8 explicitly.

## Configuration
- **No hardcoded thresholds**: All numeric thresholds/rules come from config (see `config.yaml`, `color.yaml`, client YAMLs). Validate/coerce in loaders/checks.
- **YAML gotcha**: Unquoted `yes`/`no`/`on`/`off` become booleans - code must reject or normalize appropriately (see `src/checks/too_short.py`).
- **Client overrides**: Per-client YAMLs override defaults; merging is explicit via `src/config.py`.

## Checks
- **Signature**: Most rule-based checks follow `check(captions: Sequence[Caption], settings: dict, context: ...) -> list[Issue]` (see `src/checks/too_short.py`, `src/checks/spelling.py`).
- **Context**: `src/checks/context.py` carries shared state (glossary, word lists, config, etc.).
- **Exceptions**: Handle exceptions per caption/window; log and continue rather than crashing the whole run.
- **Confidence**: Many checks support `min_confidence` and may mark issues as lower severity.

## Testing
- **Location**: Tests in `tests/`, named `test_*.py`. Fixtures in `tests/fixtures/`.
- **Imports**: Import from `src.*` directly (pytest sets `pythonpath = .` in `pytest.ini`).
- **API calls**: Tests must never hit real external APIs. Use `monkeypatch` and fakes (see `tests/conftest.py`: `no_real_api_calls`, `no_slow_video_passes`).
- **Slow tests**: Mark with `@pytest.mark.slow` if they need ffmpeg/Whisper; skipped by default, run with `--run-slow`.
- **Deterministic**: Avoid network/filesystem side effects; use temp dirs/monkeypatches.
- **Every rule-based check needs tests**: Add unit tests for new rule-based checks.

## Scripts
- **Location**: Utility scripts in `scripts/`. They may need to import from `src/` or `scripts/` modules.
- **Path handling**: If importing local modules, use `sys.path` manipulation or run from repo root. Prefer making them importable or documenting usage.
- **CLI style**: Use `argparse` with clear help text and docstrings.
- **No side effects on import**: Avoid heavy work at import time where possible.

## Files & Paths
- **Paths**: Always use `pathlib.Path`. Convert to strings only when passing to external tools.
- **Encoding**: UTF-8 for all text files. Handle encoding errors gracefully.
- **Cross-platform**: Use `os.sep`/`pathlib` and `shutil.which` for executables (e.g. ffmpeg). No hardcoded Windows paths in shared code.
- **Output locations**: Never write to user Documents by default. Use configured dirs or OS app data via `platformdirs`.

## Logging & Errors
- **Logging**: Use `logging.getLogger("qcbot")` or module loggers; engine is UI-independent.
- **Errors**: Raise `QCError` for user-facing errors in engine/CLI paths where appropriate. Catch and log unexpected exceptions.
- **Warnings**: Use `warnings` module or logging; CLI formats warnings cleanly.

## Dependencies
- **Policy**: No new dependencies without listing in `video-qc-bot-project-plan.md`. No PyTorch.
- **Pinning**: Dependencies pinned in `requirements.txt` where specified.
