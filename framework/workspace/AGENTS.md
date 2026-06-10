<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-10 -->

# workspace

Workspace switching — allows agents to operate in different project directories without restart. Supports `cd`/`exit` commands with callback notification, `os.chdir()`, and `cwd.json` persistence.

## Key Files

| File | Description |
|------|-------------|
| `context.py` | `WorkspaceContext` ABC, `DefaultWorkspaceContext` — cd/exit/restore with async lock, callback chain, path validation, active-agent guard, persistence |
| `models.py` | `CdResult` (frozen dataclass), `CdError` (StrEnum), `WorkspaceSwitchCallback` Protocol |
| `parse.py` | `parse_user_path()` — resolves user input to absolute path |
| `handlers.py` | `CdCommandHandler`, `ExitCommandHandler`, `PwdCommandHandler` — slash command handlers for workspace operations |

## Switch Flow

```
validate path → check agent idle → notify callbacks → os.chdir() → persist cwd.json → update state
```

All steps run under `asyncio.Lock` to prevent concurrent switches. Callback failures abort the switch without changing state.

## For AI Agents

- `WorkspaceContext` is injected into the pipeline; subsystems register callbacks via `register_callback()`
- `data_dir` property returns `current / .modex/` (configurable via `MODEX_DATA_DIR` env var)
- `restore()` called at startup to resume last workspace; returns `None` if no persisted state
- Commands: `/cd <path>` switches workspace, `/exit` returns to home, `/pwd` shows current path
