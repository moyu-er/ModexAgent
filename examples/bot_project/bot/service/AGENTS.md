<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-07-09 -->

# service

Bot service lifecycle, pool orchestration, and workspace management. This is the **central wiring hub** of the entire bot.

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Package marker |
| `core.py` | `BotService` — initialization, workspace stack assembly (multi-live), pool creation, pipeline assembly, lifecycle management. **Owns the shared MCP connection registry** (ADR-0017 Task 5a): built once in `initialize()` gated by `config/mcp/registry.json` `sharedRegistry` (default on), shut down in `stop()` after workspaces evict |
| `builders.py` | Tool registration, MCP tool loading, subagent memory/skill construction, terminal tool setup. `_load_agent_mcp_tools` has a shared-registry branch: when passed a `McpConnectionRegistry` it acquires a `SharedMcpBackend` facade instead of building a private `MCPClientManager` |
| `pool_builder.py` | `create_pool()` — assembles an `AgentPool` with main agent + subagent descriptors from config; per-workspace tool wrapping via `WorkspaceRootProvider`. Threads `mcp_registry` through to the main-agent MCP tool loader |
| `_external_coding_wiring.py` | External pool assembly. OpenCode prefers a warm SSE `opencode serve` backend with sticky `opencode run` fallback; Pi uses its per-turn backend. The composite close attempts every owned adapter and preserves the first failure |
| `pool_instance.py` | `PoolInstance` dataclass — holds pool config, `AgentPool` reference, main agent name |
| `pool_router.py` | `PoolRouter` — session→pool dispatch; `PoolSessionStore` persists session→pool mapping. Now delegates message processing to input pipeline (adapter produces seed envelope → pipeline stages → enqueue callback enters broker queue) |
| `web_ui_service.py` | `WebUIService` — the single IM + WebUI entry point. Assembles and starts the HTTP + WS server; **auto-discovers every `bot/adapters/register_*.py`** (QQ / Telegram / WebSocket) by importing them to fire the `@register` decorators, then builds enabled adapters from `ADAPTERS`; creates `PoolSkillManagerRegistry` and `BotInputContext`; wires pipeline into adapters |
| `qq_service.py` | `QQBotService` — a QQ-only `BotService` variant. The `modexbot` CLI start path runs `WebUIService` (which itself auto-discovers the QQ adapter), so this is a standalone/alternate entry, not the default |
| `session_store.py` | `WorkspacePoolSessionStore` — SessionInfo index partitioned by pool under a per-workspace `session_index` dir |
| `workspace_store.py` | Workspace- and pool-partitioned transcript store (ctxvar-routed writes); cross-cutting business concern |
| `recent_workspaces.py` | Recent-workspace tracker (JSON, max 20 paths) for the WebUI quick-switch dropdown |

## Workspace Model (multi-live)

The workspace system lives in `bot/workspace/` (generic half) + `bot/workspace/bundle/` (business half). It REPLACES the old single-active `WorkspaceManager` (deleted) with:

- **`WorkspaceRegistry[R]`** — holds multiple `WorkspaceContext`s + lazily-cached resource bundles (`R`). Resources materialize on first use and are LRU-evictable.
- **`SessionWorkspaceMap`** — per-session `session_id → target` pointer (replaces global `cwd.json`). `/cd` mutates only this pointer — no deactivation, no re-point.
- **`WorkspaceMessageDispatcher`** — per-message routing: resolves session → workspace → binds `current_workspace_root` contextvar → routes into that workspace's `PoolRouter`.
- **Per-workspace broker/inbox/bus** — each workspace owns its own; no shared re-point (fixes the old inbox-stranding defect).
- **`WorkspaceHandleRootProvider`** — per-workspace tool default-path binding (fixes the old tool-CWD defect).

Key architectural decisions:
- `workspace.enabled` flag in `WorkspaceConfig` (default `False`) → single-home stack (no `/cd`); `True` → full multi-live.
- `bot/workspace/` (generic half, except `bundle/`) has zero business imports — migration-ready to `modex_agent/workspace/`.
- `WorkspaceManager` (ABC, `modex_agent/workspace/resources.py`) is now a framework-level interface for pipeline workspace access, NOT a single-active switch engine.

## Workspace directory layout (paths.py as single authority)
```
<target>/.<data_dir_name>/
├── memory/<pool>/          # MemorySystem + pruned + fork_contexts
├── runtime_state/<pool>/   # turns, commands, trace, output
├── experiences/<pool>/<agent>/
├── inbox/                  # agent message delivery
├── pool_sessions/          # session→pool routing
├── sessions/               # transcript (WebUI)
├── session_index/          # SessionInfo
└── overflow/               # tool result overflow

Global tier (not under any workspace): `<home>/.<data_dir_name>/_registry/{workspaces.json, session_map.json}`
```

## Pool Routing Flow

Defined in `pool_router.py`:

1. `WorkspaceMessageDispatcher` resolves the session's workspace and routes messages into that workspace's `PoolRouter`.
2. `PoolRouter` looks up session's current pool from `PoolSessionStore` (default: configured default pool).
3. Routes message to pool's main agent via `BrokerMessage`.
4. **Pool-switch commands** (`/pool_name`) are handled upstream by the input pipeline (S2), not by `PoolRouter` directly.

## Shared MCP Connection Registry (ADR-0017 Task 5a)

`BotService` owns a service-scoped `McpConnectionRegistry` — one subprocess per configured MCP server, shared across all pools/agents/workspaces, deduped by canonical config-hash. This is the **main-agent path only** in 5a; subagent wiring is deferred to Task 5b.

- **Gate**: `config/mcp/registry.json` top-level `sharedRegistry` boolean (default `true`, read by `bot.config.mcp_registry.read_shared_registry_flag`). Fail-open: missing file / malformed JSON / absent key → `true` (the registry is an optimization; worst case falls back to today's per-pool path). Set `sharedRegistry: false` to disable.
- **Lifecycle**: built in `initialize()` AFTER config load, BEFORE the workspace stack materializes the home pools. `${ENV}` interpolation runs before the registry hashes/connects (else `${TOKEN}` reaches the subprocess literally). `start_connecting` fires all supervisor tasks concurrently so connections are READY by the time pools call `registry.acquire`.
- **Main-agent path**: `_load_agent_mcp_tools(agent, selection, project_dir, mcp_registry=reg)` → `reg.acquire(selection)` → `SharedMcpBackend` facade (no `MCPClientManager`, no `registry.json` read — the registry already holds the full server map).
- **Teardown convergence**: `_stop_resources` calls `mcp_manager.release()`. Both `MCPClientManager` and `SharedMcpBackend` are `McpBackend` with `release()` — on the legacy path it closes connections (== `disconnect_all`), on the shared path it only detaches the facade (real connections close at registry shutdown). One call works for both; no conditional.
- **Stop ordering**: `stop()` evicts workspaces first (→ `release()` per pool, detach facades), THEN `registry.shutdown()` closes the actual shared subprocesses.
- **Flag-off path byte-for-byte**: `sharedRegistry: false` (or registry absent) → `self._mcp_registry = None` → `_load_agent_mcp_tools` takes the legacy `MCPClientManager` branch unchanged → `release()` == `disconnect_all()` (today's behavior).

## External Coding Lifecycle (ADR-0022)

- `build_external_coding_backend()` returns one `StreamingProviderBackend`;
  callers do not branch during shutdown.
- OpenCode's composite owns both `OpenCodeServerBackend` and
  `OpenCodeBackend`. SSE startup failure activates a sticky subprocess
  fallback, but close always attempts both adapters.
- `build_external_session_map_store()` follows workspace persistence config:
  FILE uses `LocalFileExternalSessionMapStore`; SQLITE uses
  `SqliteExternalSessionMapStore` with the workspace connection and scope.
- Workspace teardown calls `AgentPool.shutdown_all()`, which reaches
  `ExternalCodingAgent.stop()` and then backend `close()`. Only successful
  owners are removed; failures remain available for retry.
- A normal external-agent turn never closes the warm OpenCode server. It is
  released on backend/agent/pool/workspace shutdown, while Pi and
  `opencode run` children are reaped per turn.

## For AI Agents

### Working In This Directory
- `core.py` is the single most important file — read it first to understand the entire initialization chain.
- The workspace stack is assembled in `bot/workspace/bundle/wiring.py` (`build_workspace_stack` / `build_single_workspace_stack`); `core.py` branches on `workspace.enabled`.
- Per-workspace resources (broker, inbox, bus, interceptor, background tasks, pools) are built inside `_build_resources` (`wiring.py`); each workspace gets its own set.
- Pool creation order matters: per-workspace infra (broker, inbox, retention) is built first, then pools individually.
- `web_ui_service.py` wires the entire input pipeline per adapter (IM vs WebUI entry points).

### Common Patterns
- IOC pattern: `AppConfig` is the single source of truth; all config flows from it.
- Builder pattern: `builders.py` and `pool_builder.py` construct complex objects with many dependencies.
- Multi-live pattern: switching mutates only a per-session pointer; resources are lazy + cached + evictable; in-flight turns are unaffected.

## Dependencies

### Internal
- `modex_agent/workspace/port.py` — `WorkspaceControlPort` (per-session cd/exit/pwd contract)
- `bot/workspace/` — generic workspace mechanism (registry, resolver, session map, controller)
- `bot/workspace/bundle/` — business resource factory, dispatcher, per-workspace wiring
- `modex_agent/multi_agent/pool.py` — `AgentPool`
- `modex_agent/workspace/resources.py` — `WorkspaceManager` ABC (framework view of workspace resources)
- `modex_agent/pipeline/pipeline.py` — `Pipeline`
- `modex_agent/memory/` — memory system, store registries
- `bot/adapters/` — input/output adapters
- `bot/webui/` — WebUI server and events
