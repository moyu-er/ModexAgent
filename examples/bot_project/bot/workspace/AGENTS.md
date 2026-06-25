<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-06-22 -->

# bot/workspace

Business half of the workspace mechanism — pool-scoped resource bundle and BotService wiring. The generic half lives in `modex_agent/workspace/`.

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Re-exports key types; intentionally omits `wiring` to avoid service-layer dependency |
| `handle.py` | `PoolWorkspaceResources`, `WorkspaceHandle`, `WorkspaceHandleRootProvider`, `WorkspaceResolverCell` — resource bundle and handle types |
| `factory.py` | `PoolResourceFactory` — orchestrates workspace resource build/evict via injected closures |
| `dispatch.py` | `WorkspaceMessageDispatcher` — per-message workspace routing, contextvar binding |
| `pool_data.py` | `PoolData` (frozen dataclass) + `build_pool_data` — bundles per-pool stores |
| `wiring.py` | `build_workspace_stack` / `build_single_workspace_stack` — workspace assembly (imports service layer) |
| `background.py` | `BackgroundTaskRunner` — per-workspace background tasks (dream, curation, retention) |

## For AI Agents

### Working In This Directory
- `wiring.py` is the assembly hub — read it to understand how workspaces are built end-to-end.
- `pool_data.py` is a pure data transformation — no framework imports, easily testable.
- `factory.py` uses dependency injection (closures) for testability — the real build logic lives in `wiring.py`.

### Common Patterns
- Resource factory pattern: `PoolResourceFactory` implements `ResourceFactory[PoolWorkspaceResources]` from `modex_agent.workspace`
- Per-workspace isolation: each workspace gets its own broker, inbox, bus, interceptor, background tasks
- PoolData is `frozen=True` dataclass — immutable after construction

## Dependencies

### Internal
- `modex_agent/workspace/` — generic workspace mechanism (registry, resolver, context, paths, control, store)
- `bot/service/pool_builder.py` — pool construction (via wiring)

<!-- MANUAL -->
