# AGENTS.md

## Project rules (for opencode and all agents)

- **Cross-platform**: Windows 10/11 and macOS. Use `pathlib.Path`, UTF-8 everywhere, `platformdirs` for OS paths. No OS-specific code in shared logic; put OS-specific code in `src/platform/`.
- **No hardcoded thresholds**: All thresholds, rules, and toggles must come from config files (`config.yaml`, `color.yaml`, client YAMLs) or be user-configurable. Do not hardcode numeric thresholds in checks.
- **Every rule-based check needs unit tests**: Any new rule-based check must have unit tests. Prefer deterministic tests; tests that call external APIs must be mocked/faked.
- **Never write to Documents by default**: Default output directories must use OS-appropriate app data paths via `platformdirs` or project-local `reports/`/`cache/` as configured. Do not write to the user's Documents folder by default.
- **No new dependencies without listing them in the plan**: Do not add new dependencies without adding them to `video-qc-bot-project-plan.md` (Tech stack / decisions) and justifying them.
- **No PyTorch**: Do not add PyTorch or PyTorch-dependent packages.
- **Keep the engine independent of the UI**: `src/engine.py` must remain UI-independent. Progress via callbacks, logging via loggers, no direct Qt/UI imports.
- **Pointer to the plan**: See `video-qc-bot-project-plan.md` for current scope, architecture, and decisions.
