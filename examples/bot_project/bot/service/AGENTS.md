<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-13 -->

# service

Bot service lifecycle, pool orchestration, and workspace management. This is the **central wiring hub** of the entire bot.

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Package marker |
| `core.py` | `BotService` — initialization, workspace context, pool creation, workspace switch callbacks, lifecycle management |
| `builders.py` | Tool registration, MCP tool loading, subagent memory/skill construction, terminal tool setup |
| `pool_builder.py` | `create_pool()` — assembles an `AgentPool` with main agent + subagent descriptors from config |
| `pool_instance.py` | `PoolInstance` dataclass — holds pool config, `AgentPool` reference, main agent name |
| `pool_router.py` | `PoolRouter` — routes incoming messages to the correct pool; `PoolSessionStore` persists session→pool mapping |
| `web_ui_service.py` | `WebUIService` — assembles and starts the WebUI HTTP + WebSocket server |
| `qq_service.py` | QQ platform service — wires QQ adapters to the bot |

## Workspace Switching Flow

Defined in `core.py`:

1. `DefaultWorkspaceContext` created with `active_checker` (checks all pools for running sessions).
2. Two callbacks registered: `_on_ws_stop_and_rebuild` and `_on_ws_terminal_reset`.
3. On `cd(target)`: check idle → run callbacks → `os.chdir()` → persist `cwd.json`.
4. `_on_ws_stop_and_rebuild`: stop dream engine → clear subagent caches → rebuild pool memory → rebuild shared infra.
5. `_on_ws_terminal_reset`: close all terminal sessions.

## Pool Routing Flow

Defined in `pool_router.py`:

1. `PoolRouter.run()` receives messages from `InputAdapter`.
2. Checks for `/pool_name` command → updates `PoolSessionStore` if match.
3. Otherwise looks up session's current pool from store (default: configured default pool).
4. Routes message to pool's main agent via `BrokerMessage`.

## For AI Agents

### Working In This Directory
- `core.py` is the single most important file — read it first to understand the entire initialization chain.
- The callback pattern in workspace switching means new subsystems only need `register_callback()`.
- Pool creation order matters: shared infra (broker, inbox, retention) is built first, then pools individually.
- `pool_router.py` handles both IM `/pool_name` commands and WebUI `set_pool()` external calls.

### Common Patterns
- IOC pattern: `AppConfig` is the single source of truth; all config flows from it.
- Builder pattern: `builders.py` and `pool_builder.py` construct complex objects with many dependencies.
- Callback pattern: workspace switching, pool lifecycle hooks.

## Dependencies

### Internal
- `framework/workspace/context.py` — `DefaultWorkspaceContext`
- `framework/multi_agent/pool.py` — `AgentPool`
- `framework/multi_agent/router.py` — `DefaultMeshRouter`
- `framework/pipeline/pipeline.py` — `Pipeline`
- `framework/memory/` — memory system, store registries
- `bot/adapters/` — input/output adapters
- `bot/webui/` — WebUI server and events
