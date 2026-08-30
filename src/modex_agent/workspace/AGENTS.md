<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-08-22 -->

# modex_agent/workspace

Generic workspace mechanism — identifies, isolates, and routes per-workspace resources. Pool-agnostic — pool concepts are business concerns and live in `bot/workspace/`. Since the scope-assembly addressing convergence (ADR-0042): the registry is scope-generic (`ScopeRegistry[R]`, renamed from `WorkspaceRegistry`), and path resolution goes through one scope-path resolver.

## Architecture

```
ScopeRegistry[R]             ← holds multiple WorkspaceContext + lazily-cached R
  ├── WorkspaceContext       ← identity/value object (path + data_dir_name)
  ├── ResourceFactory[R]     ← materialize() / evict() ABC
  └── ScopeRegistryStore     ← persistence ABC (GlobalWorkspaceStore impl)

ScopePath + resolve_scope_path ← the ONE scope-path resolver (workspace_root + optional
  │                             pool segment → resource bundle lookup along the parent chain)
  ▼
WorkspaceResolver[R]         ← session target → live workspace handle
SessionWorkspaceMap          ← per-session session_id → target-workspace pointer
  │                           (replaces global cwd.json)
  ▼
WorkspaceControlPort         ← cd() / exit() / pwd() contract
  │                           (implemented by WorkspaceController)
  ▼
WorkspaceMessageDispatcher   ← per-message: resolve workspace → bind contextvar → route
```

## Key Files

| File | Description |
|------|-------------|
| `context.py` | `WorkspaceContext` — immutable identity object (path + data_dir_name) |
| `factory.py` | `ResourceFactory[R]` — generic ABC for workspace resource materialize/evict |
| `registry.py` | `ScopeRegistry[R]` (renamed from `WorkspaceRegistry`) — holds multiple workspaces, lazy-cached resources, LRU eviction; `ScopeRegistryStore` ABC + `GlobalWorkspaceStore` persistence; begin_turn/end_turn turn-bracketing machinery |
| `scope_path.py` | `ScopePath` (frozen: `workspace_root`, optional `pool_name`) + `resolve_scope_path(manager, path)` — the single scope-path resolver (replaces the deleted `WorkspacePathResolver`) |
| `routing.py` | `SessionWorkspaceMap` (per-session pointer) + `WorkspaceMessageDispatcher` (per-message binding) |
| `port.py` | `WorkspaceControlPort` — cd/exit/pwd interface for slash commands |
| `control.py` | Workspace control commands |
| `models.py` | `CdResult`, `CdError` — result types |
| `parse.py` | `parse_user_path()` — user-level path resolution |
| `paths.py` | `WorkspacePaths` — safe on-disk layout with containment checks |
| `record.py` | `WorkspaceRecord` — persisted workspace identity (registry store payload) |
| `resources.py` | `WorkspaceResources` / `WorkspaceManager` ABCs (framework view of workspace resources) |
| `runtime.py` | `bind_workspace_root` contextvar + `resolve_workspace_root` — per-turn workspace-root binding |
| `store.py` | `GlobalWorkspaceStore` — the default `ScopeRegistryStore` (workspace registry persistence) |
| `routing.py` | `WorkspaceResolver[R]` — session target → live workspace handle |
| `control.py` | `WorkspaceController[R]` — `WorkspaceControlPort` implementation (cd/exit/pwd) |

## For AI Agents

### Working In This Directory
- This package is pool-agnostic — no `bot` imports should appear here.
- `ResourceFactory[R]` is a generic ABC — business implementations like `PoolResourceFactory` implement it.
- `SessionWorkspaceMap` replaces the old global `cwd.json` approach — switching is per-session, not global.
- Addressing resolves through `ScopePath` + `resolve_scope_path` — explicit persistent mapping or explicit declaration only; a miss returns `None` for the caller's loud handling, never a synthesized CWD path.

### Common Patterns
- Generic type `R` parameter on `ScopeRegistry` / `ResourceFactory` — business layer provides concrete `PoolWorkspaceResources`
- `WorkspaceContext` is cheap (value object) — always retained; `R` is heavy — lazily materialized and LRU-evictable
- `WorkspacePaths` path accessors all use `safe_segment()` + `is_relative_to()` containment to prevent escape

## Dependencies

### Internal
- `modex_agent/workspace/` — self-contained module, minimal framework imports

### External
- None beyond standard library + `pathlib`

<!-- MANUAL: -->
