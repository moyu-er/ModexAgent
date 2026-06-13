<!-- Parent: ../../AGENTS.md -->
<!-- Updated: 2026-06-13 -->

# bot_project

Primary end-to-end reference implementation for the ModexAgent framework. Demonstrates **Pool mode** (multi-agent collaboration) and **WebUI** (React frontend with real-time streaming and per-conversation isolation).

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        Input Adapters                           │
│  QQ adapter  │  WebSocket adapter (WebUI)  │  CLI (modexbot)   │
└──────┬───────┴────────────┬────────────────┴────────┬──────────┘
       │                    │                          │
       └────────┬───────────┘                          │
                ▼                                      │
         ┌──────────────┐                              │
         │  PoolRouter   │  ← session→pool dispatch    │
         │  (pool_router)│     /pool_name switching     │
         └──────┬───────┘                              │
                │                                      │
    ┌───────────┼───────────┐                           │
    ▼           ▼           ▼                           │
┌────────┐ ┌────────┐ ┌────────┐                       │
│ main   │ │ coding │ │  ...   │  ← AgentPool instances│
│  pool  │ │  pool  │ │  pool  │    (each has main +   │
└────────┘ └────────┘ └────────┘     subagents)        │
                                                       │
         ┌──────────────────┐                          │
         │ WorkspaceContext │  ← cd/exit workspace     │
         │ (shared, global) │     switching with       │
         └──────────────────┘     active-agent guard   │
```

### Workspace / Pool / Session Hierarchy

- **Workspace**: Project root or cd target. Controls data directory (`.modex/`), memory storage paths, terminal cwd.
- **Pool**: A named group of agents (main + subagents). Routing is per-conversation via `PoolSessionStore`.
- **Conversation**: External chat scope, identified by `conversation_id`.
- **Session**: Agent-owned scope, format `{conversation_id}.{agent_name}[.{invocation_id}]`.

### Workspace Switching Rules

1. **Guard**: `active_checker()` verifies ALL pools have zero active sessions before allowing switch.
2. **Callbacks** (registered in `core.py`):
   - `_on_ws_stop_and_rebuild`: Stop background tasks → rebuild pool memory stores → rebuild shared infra.
   - `_on_ws_terminal_reset`: Close all terminal sessions.
3. **After switch**: `os.chdir()` updates cwd; `cwd.json` persists for restart recovery.
4. **Data source switch**: Memory stores rebuild to new `data_dir`. Conversation metadata (`conversations.json`) is global and filtered by workspace.

### WebUI vs IM Differences

| Aspect | WebUI | IM (QQ etc.) |
|--------|-------|-------------|
| Pool switching | UI selector → `PoolRouter.set_pool()` | `/pool_name` slash command |
| Workspace switching | File browser modal → `POST /api/workspace/cd` | `/cd target` command |
| Conversation listing | `GET /api/sessions?workspace=...` | N/A (single conversation) |
| Streaming isolation | Per-conversation filtering in `useWebUIStream.reducer.ts` + backend session cleanup | N/A (single conversation) |

## Key Files

| File | Description |
| --- | --- |
| `bot/service/core.py` | `BotService` — initialization, workspace context, pool creation, lifecycle |
| `bot/service/builders.py` | Tool registration, MCP tools, subagent memory/skill construction, terminal setup |
| `bot/service/pool_builder.py` | Pool mode assembly — creates `AgentPool`, subagent descriptors |
| `bot/service/pool_router.py` | `PoolRouter` — session→pool dispatch, `PoolSessionStore` persistence |
| `bot/service/pool_instance.py` | `PoolInstance` — pool runtime holder (config, pool, main_agent_name) |
| `bot/service/web_ui_service.py` | `WebUIService` — assembles and starts the WebUI HTTP/WS server |
| `bot/service/qq_service.py` | QQ platform service wiring |
| `bot/adapters/qq.py` | QQ platform input/output adapters (C2C + group + file upload) |
| `bot/adapters/web_socket.py` | WebSocket input adapter for WebUI real-time chat |
| `bot/adapters/fan_in.py` | Multi-agent output fan-in for WebUI (merges agents' streams to one WS) |
| `bot/adapters/channels.py` | Conversation→channel tracking (websocket vs qq) |
| `bot/webui/server.py` | aiohttp REST+WS server (sessions, pools, workspace APIs) |
| `bot/webui/transcript_store.py` | Per-agent transcript persistence (JSONL) for history replay |
| `bot/webui/events.py` | WebUI event types (model deltas, tool calls, turn lifecycle) |
| `bot/webui/emitter.py` | Emits WebUI events via fan-in adapter |
| `modexbot/cli.py` | CLI entry point — 3-layer process discovery for start/stop/restart |
| `modexbot/main.py` | CLI→service bootstrap |
| `config/bot_config.yml` | Agent, memory, tool, runtime, observability config. `${ENV_VAR}` interpolation |
| `config/mcp/*.json` | MCP server configs per agent (stdio/SSE/streamable_http) |
| `config/pools/*.yml` | Pool definitions — agents, roles, subagent templates |

## Multi-Agent Setup

- `main` pool: Main agent with all MCP tools, file/shell tools, communication tools + subagents (office-expert, query-12306).
- `coding` pool: Main agent + subagents (planner, worker, reviewer, scout, oracle, delegate, context-builder).
- Communication: `send_to_agent` (async inbox-based).
- `SubagentAutoSendHook` auto-forwards subagent output to parent.
- Session ID format: `{conversation_id}.{agent_name}[.{invocation_id}]` (via `DefaultSessionIdStrategy`).

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `bot/` | Core business logic — service, adapters, tools, webui (see `bot/AGENTS.md`) |
| `config/` | Configuration files (see `config/AGENTS.md`) |
| `webui/` | React frontend source (see `webui/AGENTS.md`) |
| `modexbot/` | CLI entry point for start/stop/restart (see `modexbot/AGENTS.md`) |
| `agents/` | Agent system prompt templates (see `agents/AGENTS.md`) |
| `skills/` | Agent skill definitions (self-documented via SKILL.md files) |
| `templates/` | Template files for knowledge, soul, user memory (see `templates/AGENTS.md`) |
| `tests/` | Test suites (see `tests/AGENTS.md`) |
| `plugins/` | Bot plugins (see `plugins/AGENTS.md`) |

## Testing

```powershell
python -m pytest examples/bot_project/tests -q
cd examples/bot_project/webui && npm test -- --run
```

Backend tests cover WebUI endpoints, streaming isolation, pool routing, and transcript store. Frontend tests cover the `useWebUIStream` reducer for per-conversation event filtering.

<!-- MANUAL -->
