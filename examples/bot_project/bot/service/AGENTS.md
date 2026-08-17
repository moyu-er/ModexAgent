<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-08-17 -->

# service

Bot service lifecycle, pool orchestration, and workspace management. This is the **central wiring hub** of the entire bot.

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Package marker |
| `core.py` | `BotService` — initialization, workspace stack assembly (multi-live), pool creation, pipeline assembly, lifecycle management. **Owns the shared MCP connection registry** (ADR-0017 Task 5a): built once in `initialize()` gated by `config/mcp/registry.json` `sharedRegistry` (default on), shut down in `stop()` after workspaces evict. Extracted helpers: `_model_config_loader.py`, `_runtime_builders.py` |
| `builders.py` | Tool registration, MCP tool loading, subagent memory/skill construction, terminal tool setup. `_load_agent_mcp_tools` has a shared-registry branch: when passed a `McpConnectionRegistry` it acquires a `SharedMcpBackend` facade instead of building a private `MCPClientManager` |
| `pool/` | `create_pool()` subpackage — assembles an `AgentPool` with main agent + subagent descriptors from config. Split into `factory.py` (orchestrator), `assembly_context.py`, `external_subagent.py`, `strategy_registry.py`, `memory_defaults.py`, `tool_projection.py`, `agent_factory.py`, `pool_construction.py`, `communication.py`, `pipeline_wiring.py`. Per-workspace tool wrapping via `WorkspaceRootProvider`. Threads `mcp_registry` through to the main-agent MCP tool loader |
| `pool_instance.py` | `PoolInstance` dataclass — holds pool config, `AgentPool` reference, main agent name |
| `pool_router.py` | `PoolRouter` — session→pool dispatch; `PoolSessionStore` persists session→pool mapping. Now delegates message processing to input pipeline (adapter produces seed envelope → pipeline stages → enqueue callback enters broker queue) |
| `session_pool_index.py` | `SessionPoolIndex`: per-workspace, registration-based attribution index answering "which pool owns this session_id" from the session tree (`tree.pool_name`), never from the routing table. Registered by `create_pool` (`pool/factory.py:419-420`); see "Pool Attribution vs Routing" below |
| `web_ui_service.py` | `WebUIService` — the single IM + WebUI entry point. Assembles and starts the HTTP + WS server; **auto-discovers every `bot/adapters/register_*.py`** (QQ / Telegram / WebSocket) by importing them to fire the `@register` decorators, then builds enabled adapters from `ADAPTERS`; creates `PoolSkillManagerRegistry` and `BotInputContext`; wires pipeline into adapters. Extracted helpers: `bot/webui/workspace_providers.py`, `bot/webui/adapter_discovery.py` |
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

## Pool Attribution vs Routing (pool-attribution convergence)

Attribution and routing are distinct concerns with different authorities:

- **Attribution answers "which pool owns this session_id"**: per-session, fixed at pool assembly time (`create_pool` passes `pool_name` positionally into the emitter and subagent-callback closures, `pool/factory.py`). The authoritative carrier is the session tree (`tree.pool_name`); reads go through `SessionPoolIndex`. **Invariant: the routing table never answers attribution.** Nothing may infer session ownership from `PoolSessionStore`: a prefix reflects intent history, and a cross-pool peer session reuses the sender prefix while living in the receiving pool's tree.
- **Routing answers "which pool handles this conversation's next message"**: per-conversation, keyed by the agent-independent session prefix, persisted in `PoolSessionStore` (`pool_router.py`). Only explicit intent writes it: client `pool` request param, WebUI pool selector, IM `/pool_name` command, new-session bootstrap.
- **`SessionPoolIndex`** (`session_pool_index.py`): registration-based, per-workspace, read-only attribution surface. `create_pool` registers each pool's freshly built tree/node store pair immediately after `build_session_tree_stores` (`pool/factory.py:411,419-420`); `pool_of(session_id)` walks the registered node stores in registration order and returns `tree.pool_name` (the tree is the authority; the registration key only selects which stores are searched). Re-registering replaces the entry, so a pool rebuild swaps in fresh handles. One instance is built per workspace (`bot/workspace/wiring/resources.py`, held on `PoolWorkspaceResources` in `bot/workspace/handle.py`, released with the bundle on eviction) and passed to every `create_pool` as the `session_pool_index` kwarg. No pool-to-session enumeration, no routing mutation, no prefix inference (module contract, `session_pool_index.py:1-18`).
- **Routing writes are gated to explicit sources**: S5 `ResolvePoolStage` persists to `PoolSessionStore` only when `explicit_pool` is set; resolution order is `explicit_pool` > `RoutingMeta.TREE_RESOLVED_POOL` > stored route / default pool, and the tree-resolved value never lands in the table (`bot/input_pipeline/stages/resolve_pool.py:76-80,115-117`). IM messages carry no explicit key: they read the stored route and never write (behavior unchanged). WS attach writes only under the three eligibility rules implemented in `bot/webui/routes/websocket/attach.py` and documented in `bot/webui/AGENTS.md`.
- **`_routing_pool_for_prefix`** (`web_ui_service.py:465-477`): nullable prefix-route lookup reserved for infrastructure partitioning ONLY: session-index physical layout, GC placement, turn-store placement. Its four call sites each apply their own explicit `or _DEFAULT_AGENT_NAME` fallback (no silent default inside the helper). Display, envelopes, and transcripts must take pool from the first-class request parameter or the emitter argument; this helper is NOT an ownership source.
- **Emitter factory contract**: business emitter factories expose `(session_id, pool)` positionally (the `web_ui_service.py` composite factory; `bot/adapters/register_websocket.py`, `register_qq.py`, `register_telegram.py` leaf factories; `qq_service.py`). `create_pool` binds `pool_name` into `pool_bound_emitter` / `pool_bound_on_created` before assembly-context construction, and `_WorkspaceEmitterFactory` wraps the already-bound emitter, never the raw business factory, so the main-agent and external-subagent paths share one pool-bound form. `WebBotEmitter(pool=None)` does not self-report a pool: transcript appends and `DeltaEnvelope.pool` use `self._pool or ""` (`bot/webui/emitter/web_bot.py`, `bot/webui/emitter/_segments.py`).

## Shared MCP Connection Registry (ADR-0017 Task 5a)

`BotService` owns a service-scoped `McpConnectionRegistry` — one subprocess per configured MCP server, shared across all pools/agents/workspaces, deduped by canonical config-hash. This is the **main-agent path only** in 5a; subagent wiring is deferred to Task 5b.

- **Gate**: `config/mcp/registry.json` top-level `sharedRegistry` boolean (default `true`, read by `bot.config.mcp_registry.read_shared_registry_flag`). Fail-open: missing file / malformed JSON / absent key → `true` (the registry is an optimization; worst case falls back to today's per-pool path). Set `sharedRegistry: false` to disable.
- **Lifecycle**: built in `initialize()` AFTER config load, BEFORE the workspace stack materializes the home pools. `${ENV}` interpolation runs before the registry hashes/connects (else `${TOKEN}` reaches the subprocess literally). `start_connecting` fires all supervisor tasks concurrently so connections are READY by the time pools call `registry.acquire`.
- **Main-agent path**: `_load_agent_mcp_tools(agent, selection, project_dir, mcp_registry=reg)` → `reg.acquire(selection)` → `SharedMcpBackend` facade (no `MCPClientManager`, no `registry.json` read — the registry already holds the full server map).
- **Teardown convergence**: `_stop_resources` calls `mcp_manager.release()`. Both `MCPClientManager` and `SharedMcpBackend` are `McpBackend` with `release()` — on the legacy path it closes connections (== `disconnect_all`), on the shared path it only detaches the facade (real connections close at registry shutdown). One call works for both; no conditional.
- **Stop ordering**: `stop()` evicts workspaces first (→ `release()` per pool, detach facades), THEN `registry.shutdown()` closes the actual shared subprocesses.
- **Flag-off path byte-for-byte**: `sharedRegistry: false` (or registry absent) → `self._mcp_registry = None` → `_load_agent_mcp_tools` takes the legacy `MCPClientManager` branch unchanged → `release()` == `disconnect_all()` (today's behavior).

## External Coding Lifecycle (ADR-0022)

- `build_external_backend()` returns one `StreamingProviderBackend`;
  callers do not branch during shutdown.
- OpenCode uses `OpenCodeServerBackend`, a thin wrapper that borrows the
  shared `opencode serve` process from the `OpenCodeServerManager` singleton.
  There is no fallback mechanism; the manager plus watchdog guarantee
  reliability.
- `BotService.start()` enters `async with OpenCodeServerManager.lifecycle():`.
  On stop, the context exit calls `_shutdown()`, which stops the watchdog,
  waits up to 5s for active sessions, then terminates the process.
- `build_external_session_map_store()` follows workspace persistence config:
  FILE uses `LocalFileExternalSessionMapStore`; SQLITE uses
  `SqliteExternalSessionMapStore` with the workspace connection and scope.
- Workspace teardown calls `AgentPool.shutdown_all()`, which reaches
  `ExternalAgent.stop()` and then backend `close()`. Only successful
  owners are removed; failures remain available for retry.
- A normal external-agent turn never closes the shared OpenCode server. It is
  released only on `BotService.stop()` via `lifecycle()` exit, while Pi and
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
- Builder pattern: `builders.py` and `pool/` construct complex objects with many dependencies.
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
