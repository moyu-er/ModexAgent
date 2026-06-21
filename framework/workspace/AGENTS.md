<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-19 -->

# workspace

Workspace primitives — the framework layer provides a `WorkspaceControlPort` ABC (per-session) and slash-command handlers for workspace switching. The concrete controller (`WorkspaceController`) lives in `bot/workspace/control.py`; the per-session pointer is `SessionWorkspaceMap` in `bot/workspace/routing.py`. The workspace model is **multi-live**: many workspaces coexist, switching = mutating a per-session pointer; no `os.chdir`, no busy-check, no single `_active`.

## Key Files

| File | Description |
|------|-------------|
| `port.py` | `WorkspaceControlPort` ABC — per-session `switch(session_id)`/`exit(session_id)`/`current(session_id)`/`home`; consumed by handlers |
| `models.py` | `CdResult` (frozen dataclass), `CdError` (StrEnum) |
| `parse.py` | `parse_user_path()` — resolves user input to absolute path |
| `handlers.py` | `CdCommandHandler`, `ExitCommandHandler`, `PwdCommandHandler` — slash-command handlers over the port; inject a per-session id extractor |
| `runtime.py` | `current_workspace_root` contextvar — bound per-turn by the dispatcher so tools read the workspace target |
| `resources.py` | `WorkspaceResources` ABC — framework view of workspace pool_data (type contract for pipeline + comm service) |
| `AGENTS.md` | This file |

## Switch Flow (multi-live)

The framework layer does NOT perform switching — it only routes commands:
1. `/cd <path>` → `CdCommandHandler` → calls `port.switch(session_id, target)` → business `WorkspaceController.switch`.
2. `/exit` → `ExitCommandHandler` → calls `port.exit(session_id)`.
3. `/pwd` → `PwdCommandHandler` → reads `port.current(session_id)` / `port.home`.

The business `WorkspaceController.switch` only mutates the per-session pointer (`SessionWorkspaceMap`) and registers a `WorkspaceContext` in the registry (resources materialize lazily on first turn). No workspace is deactivated, no broker/inbox is re-pointed. In-flight turns hold their materialized `R` and finish unaffected.

## For AI Agents

- The `WorkspaceControlPort` ABC is the framework contract; the business implementation is `bot.workspace.control.WorkspaceController`.
- `data_dir_name` is a config field on `AppConfig.paths` (default `.modex`), not an env var.
- Busy-check, `os.chdir`, `DefaultWorkspaceContext`, `cwd.json`, `on_activate`/`on_deactivate` callbacks are all removed — the multi-live model replaces all of them.
- `WorkspaceManager` (ABC, `framework/multi_agent/communication.py`) is a framework-level ABC for the pipeline/comm service's workspace resource access, NOT a single-active switch engine.
