<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-22 -->

# framework/workspace

Generic workspace mechanism — identifies, isolates, and routes per-workspace resources. Pool-agnostic — pool concepts are business concerns and live in `bot/workspace/`.

## Architecture

```
WorkspaceRegistry[R]        ← holds multiple WorkspaceContext + lazily-cached R
  ├── WorkspaceContext       ← identity/value object (path + data_dir_name)
  └── ResourceFactory[R]    ← materialize() / evict() ABC

SessionWorkspaceMap          ← per-session session_id → target-workspace pointer
  │                           (replaces global cwd.json)
  ▼
WorkspaceControlPort         ← cd() / exit() / pwd() contract
  │                           (implemented by bot service layer)
  ▼
WorkspaceMessageDispatcher   ← per-message: resolve workspace → bind contextvar → route
```

## Key Files

| File | Description |
|------|-------------|
| `context.py` | `WorkspaceContext` — immutable identity object (path + data_dir_name) |
| `factory.py` | `ResourceFactory[R]` — generic ABC for workspace resource materialize/evict |
| `registry.py` | `WorkspaceRegistry[R]` — holds multiple workspaces, lazy-cached resources, LRU eviction |
| `routing.py` | `SessionWorkspaceMap` (per-session pointer) + `WorkspaceMessageDispatcher` (per-message binding) |
| `port.py` | `WorkspaceControlPort` — cd/exit/pwd interface for slash commands |
| `control.py` | Workspace control commands |
| `models.py` | `CdResult`, `CdError` — result types |
| `parse.py` | `parse_user_path()` — user-level path resolution |
| `paths.py` | `WorkspacePaths` — safe on-disk layout with containment checks |
| `resources.py` | Resource type helpers |
| `runtime.py` | Runtime support for workspace operations |
| `store.py` | Workspace-scoped storage support |

## For AI Agents

### Working In This Directory
- This package is pool-agnostic — no `bot` imports should appear here.
- `ResourceFactory[R]` is a generic ABC — business implementations like `PoolResourceFactory` implement it.
- `SessionWorkspaceMap` replaces the old global `cwd.json` approach — switching is per-session, not global.

### Common Patterns
- Generic type `R` parameter on `WorkspaceRegistry` / `ResourceFactory` — business layer provides concrete `PoolWorkspaceResources`
- `WorkspaceContext` is cheap (value object) — always retained; `R` is heavy — lazily materialized and LRU-evictable
- `WorkspacePaths` path accessors all use `safe_segment()` + `is_relative_to()` containment to prevent escape

## Dependencies

### Internal
- `framework/workspace/` — self-contained module, minimal framework imports

### External
- None beyond standard library + `pathlib`

<!-- MANUAL: -->
