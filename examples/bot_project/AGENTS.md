<!-- Updated: 2026-06-22 | WorkspaceManager refactor -->

# bot_project

Primary end-to-end reference implementation for the ModexAgent framework. Demonstrates **Pool mode** (multi-agent collaboration), **multi-channel IM** (QQ + Telegram), and **WebUI** (React frontend with real-time streaming and per-conversation isolation).

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                        Input Adapters                            │
│  QQ adapter  │  Telegram adapter  │  WebSocket adapter (WebUI)  │
│        (auto-discovered by WebUIService via @register)           │
└──────┬───────┴──────────┬─────────┴────────────┬─────────────────┘
       │                    │                          │
       └────────┬───────────┘                          │
                ▼                                      │
     ┌─────────────────────┐                           │
     │   Input Pipeline     │  ← 7-stage convergence   │
     │  (S2–S8, per-channel)│     S4 runs first for IM │
     │  control + skill     │     channel-aware routing│
     │  persistence + queue │                           │
     └────────┬────────────┘                           │
              ▼                                       │
       ┌──────────────┐                              │
       │  PoolRouter   │  ← session→pool dispatch    │
       │  (pool_router)│     /pool_name switching     │
       └──────┬───────┘                              │
              │                                      │
  ┌───────────┼───────────┐                           │
  ▼           ▼           ▼                           │
┌────────┐ ┌────────┐ ┌────────┐                       │
│ main   │ │ coder │ │  ...   │  ← AgentPool instances│
│  pool  │ │  pool  │ │  pool  │    (each has main +   │
└────────┘ └────────┘ └────────┘     subagents)        │
                                                       │
       ┌──────────────────┐                          │
       │ WorkspaceContext │  ← cd/exit workspace     │
       │ (shared, global) │     switching with       │
       └──────────────────┘     active-agent guard │
```

### Workspace / Pool / Session Hierarchy

- **Workspace**: Project root or cd target. Controls data directory (`.modex/`), memory storage paths, terminal cwd.
- **Pool**: A named group of agents (main + subagents). Routing is per-conversation via `PoolSessionStore`.
- **Conversation**: External chat scope, identified by `conversation_id`.
- **Session**: Agent-owned scope, format `{conversation_id}.{agent_name}[.{invocation_id}]`.

### Workspace Model (multi-live)

The workspace system lives in `bot/workspace/` (business) backed by `modex_agent/workspace/` (generic). Key properties:

1. **Multi-live**: Many workspaces coexist in a `WorkspaceRegistry`. Switching mutates only a per-session pointer (`SessionWorkspaceMap`), not a global `_active`. No `os.chdir`, no busy-check.
2. **Snapshot safety**: In-flight turns hold a `PipelineSnapshot` with pinned workspace references, unaffected by mid-turn switches.
3. **Lazy materialization**: Heavy resources (`PoolWorkspaceResources`) are built on first use via `PoolResourceFactory`, cached, and LRU-evictable. WorkspaceContext (identity) is cheap and always retained.
4. **Safe paths**: `WorkspacePaths` in `modex_agent/workspace/paths.py` provides containment-checked path accessors.
5. **Per-workspace isolation**: Each workspace owns its own broker/inbox/bus/interceptor. Inbox cross-consume is structurally impossible.
6. **Optional**: `workspace.enabled = False` → single-home stack (no `/cd`); `True` → full multi-live. Data layout is identical.

### Input Pipeline Convergence

All user messages (IM + WebUI) flow through the **Input Pipeline** (`bot/input_pipeline/`) before reaching `PoolRouter`. The pipeline provides:

- **Unified stage processing**: 7 stages (S2–S8) shared across channels with per-channel entry points
- **IM pipeline** (S4→S2→S3→S5→S6→S7→S8): Full path with control commands
- **WebUI pipeline** (S4→S5→S6→S7→S8): No S2/S3 (UI handles workspace/pool/session controls)
- **Single persistence path**: `PersistUserMessageStage` (S7) is the only place user messages are written to transcript store
- **Skill resolution**: `SkillParseStage` (S6) validates `/skillName` commands via pluggable `SkillRegistry` ABC

### WebUI vs IM Differences

| Aspect | WebUI | IM (QQ / Telegram) |
|--------|-------|-------------|
| Pool switching | UI selector → `PoolRouter.set_pool()` | `/pool_name` slash command (S2) |
| Workspace switching | File browser modal → `POST /api/workspace/cd` | `/cd target` command (S2) |
| Turn cancellation | Pause button (🧊) → `action: "pause"` WebSocket → `_ws_pause` → CANCEL_TURN via control channel, turn ends with `stop_reason=cancelled` + `turn_end` | `/stop` command (S3) → same control-channel path |
| Conversation listing | `GET /api/sessions?workspace=...` | N/A (single conversation) |
| Streaming isolation | Per-conversation filtering in `useWebUIStream.reducer.ts` + backend session cleanup | N/A (single conversation) |
| Message dedup | `request_id`-based optimistic matching | N/A (no optimistic UI) |

## Key Files

| File | Description |
| --- | --- |
| `bot/input_pipeline/context.py` | `BotInputContext` — concrete context with pool store, transcript store, enqueue callback |
| `bot/input_pipeline/assembly.py` | `build_im_pipeline()` / `build_webui_pipeline()` — stage ordering per channel |
| `bot/input_pipeline/stages/resolve_pool.py` | S5 — pool/agent resolution + `RoutingMeta` StrEnum for envelope metadata keys |
| `bot/input_pipeline/stages/skill_parse.py` | S6 — skill validation via `SkillRegistry` ABC + `PoolSkillManagerRegistry` concrete impl |
| `bot/input_pipeline/stages/persist_user_message.py` | S7 — single persistence path for user messages |
| `bot/input_pipeline/stages/enqueue.py` | S8 — builds `InputMessage` and enqueues |
| `bot/input_pipeline/stages/environment_control.py` | S2 — IM-only `/cd`, `/pool`, `/exit`, `/pwd` interception |
| `bot/input_pipeline/stages/session_control.py` | S3 — IM-only `/stop` turn cancellation |
| `bot/input_pipeline/stages/set_channel.py` | S4 — conversation channel tagging (runs first in IM pipeline) |
| `bot/service/core.py` | `BotService` — initialization, workspace context, pool creation, pipeline wiring |
| `bot/service/builders.py` | Tool registration, MCP tools, subagent memory/skill construction, terminal setup |
| `bot/service/pool_builder.py` | Pool mode assembly — creates `AgentPool`, subagent descriptors |
| `bot/service/pool_router.py` | `PoolRouter` — session→pool dispatch, `PoolSessionStore` persistence |
| `bot/service/pool_instance.py` | `PoolInstance` — pool runtime holder (config, pool, main_agent_name) |
| `bot/workspace/wiring.py` | `build_workspace_stack` / `build_single_workspace_stack` — workspace assembly |
| `bot/workspace/handle.py` | `PoolWorkspaceResources` — per-workspace resource bundle |
| `bot/workspace/dispatch.py` | `WorkspaceMessageDispatcher` — per-message workspace routing |
| `bot/workspace/pool_data.py` | `PoolData` — frozen per-pool data bundle |
| `modex_agent/workspace/registry.py` | `WorkspaceRegistry` — multi-live workspace holder with lazy resource materialization |
| `modex_agent/workspace/routing.py` | `SessionWorkspaceMap` — per-session workspace pointer |
| `bot/service/web_ui_service.py` | `WebUIService` — the single IM + WebUI entry point; auto-discovers every `bot/adapters/register_*.py` (QQ / Telegram / WebSocket), assembles and starts the HTTP/WS server |
| `bot/service/qq_service.py` | `QQBotService` — standalone QQ-only `BotService` variant (the CLI start path uses `WebUIService`) |
| `bot/adapters/qq.py` | QQ platform input/output adapters (C2C + group + file upload) |
| `bot/adapters/telegram.py` | Telegram input/output adapters (long-polling inbound, HTML chunked outbound) |
| `bot/adapters/web_socket.py` | WebSocket input adapter for WebUI real-time chat |
| `bot/adapters/fan_in.py` | Multi-agent output fan-in for WebUI (merges agents' streams to one WS) |
| `bot/adapters/channels.py` | Multi-channel spine — `@register` adapter registry (`ADAPTERS`), `ChannelRouterOutputAdapter`, conversation→channel tracking |
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
- `coder` pool: Main agent + subagents (planner, worker, reviewer, scout, oracle, delegate, context-builder).
- Communication: `send_to_agent` (async inbox-based).
- `SubagentAutoSendHook` auto-forwards subagent output to parent.
- Session ID format: `{conversation_id}.{agent_name}[.{invocation_id}]` (via `DefaultSessionIdStrategy`).

### External coding agent pools (Pi, OpenCode)

External CLI coding agents (Pi, OpenCode) can be registered as NORMAL main agents of their own dedicated pools. A framework-side harness (`ExternalCodingAgent`) executes them through provider backends, and they communicate back through the `modexctl send` CLI. The CLI sends XML-wrapped `<agent_message>` content through the target workspace's `InboxMQ.deliver()` implementation; `modexbot` is a backward-compatible facade over `modexctl`.

**Pool configuration** (`config/pools/<name>/pool.yml`):

```yaml
main_agent_name: opencode
execution_strategy: external_coding   # opt-in; default is "react"
provider_kind: opencode               # "pi" or "opencode"
peers:
  - default                           # explicit peer declaration required
```

**Availability gating:** if the provider CLI (`pi` / `opencode`) is not on `PATH`, the pool is silently skipped at startup (warning logged). Other pools are unaffected.

**Session continuity:** each ModexAgent session maps to a provider-side session file (`<workdir>/.modex/external/pi-session.jsonl` for Pi; provider-minted id for OpenCode). Follow-up turns on the same `modex_session_id` resume the provider's own session, preserving context.

**Persistence:** the session-id map follows the configured workspace backend.
FILE uses `<workdir>/.modex/external/session-map.json`; SQLite stores the same
mapping in the workspace `state.db`. Provider-native session data remains
owned by Pi/OpenCode.

**Provider lifetime:** OpenCode prefers one warm `opencode serve` SSE process
across turns and switches permanently to per-turn `opencode run` if SSE startup
is unavailable. Pi remains per-turn. Cancellation, failed startup, pool
shutdown, and workspace eviction terminate and reap complete provider process
trees; normal OpenCode turns retain the warm server for reuse.

**WebUI:** external_coding sessions appear in the WebUI session list with their `.pi` / `.opencode` suffix, alongside every other session. Streaming output (text, reasoning, tool calls/results, errors) is rendered through the canonical `TurnEvent` seam → `WebBotEmitter` projection into existing `ServerEvent`/transcript types. The `PoolEditor` settings view supports configuring external coding provider pools.

See ADR-0022 and `docs/design/external-coding-agent-integration/` for the full design.

## Skills (global library + per-agent assignment)

The global skill **library** has two sources, REPO PRIORITY:

- `local_skills/<name>/` — the repo library (CRUD target: `upload_skill` /
  `delete_skill` operate here only). A sibling of `skills/`, outside the
  per-pool tree so it can never collide with a pool literally named "global".
- `~/.agents/skills/<name>/` — user-installed skills (read-only augment; may
  themselves be links). On a name clash the repo copy wins
  (`SkillsStore._resolve_global_source`).

Per-agent skill dirs live at `skills/<pool>/<agent>/<name>/`. Disk is the single
source of truth: neither `pool.yml` nor the WebUI pool tree carries a `skills`
field; the runtime `SkillManager` and the WebUI both read
`skills/<pool>/<agent>/` directly. A per-agent dir may be either:

- a **real copy** — committed in the repo for portability, or manually placed;
  `unassign` removes it like any other dir, or
- a **link** created by `assign` → the resolved global source (repo first, then
  user home): a symlink on POSIX (relative target, portable) and on Windows
  when the symlink privilege is available, falling back to a directory junction
  (`mklink /J`, **no privilege / no Developer Mode needed**) otherwise.

`assign` only ever creates a link into a global source (never a copy).
`unassign` removes either shape. `SkillsStore._create_dir_link` / `_remove_link`
are the converged seams; no platform preconditions on any OS.

## Todo Tools (main + coder pools)

`todo_write` (full-replace) and `todo_read` (active-only: pending + in_progress) let the agent track a multi-step task list per session. The `TodoStore` is injected at registration in `pool_builder._build_tools` (same path-injection pattern as the experience tool); persisted to `<ws>/.modex/runtime_state/<pool>/todos/<session_id>.json` (ws+pool+session isolated).

- **Decoupled from the WebUI**: the tool does NOT emit a presentation event. The
  WebUI derives the task panel from the generic `tool_call_end` stream (the tool's
  JSON result is parsed out of `result_summary`). On history load, the panel
  scans the loaded assistant messages for the most recent todo tool block with a
  result and parses it. If history doesn't carry results reliably, fall back to
  a server fetch endpoint (see spec §12 — deferred).
- Enabled only for the `main` and `coder` pools' main agents (registered in `_build_tools`; subagents do not get these tools).
- No prompt injection (v1); the agent reads state via `todo_read` or tool results in history.

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `bot/` | Core business logic — service, adapters, tools, webui, input_pipeline (see `bot/AGENTS.md`, `bot/input_pipeline/AGENTS.md`) |
| `config/` | Configuration files (see `config/AGENTS.md`) |
| `webui/` | React frontend source (see `webui/AGENTS.md`) |
| `modexbot/` | CLI entry point for start/stop/restart (see `modexbot/AGENTS.md`) |
| `agents/` | Agent system prompt templates (see `agents/AGENTS.md`) |
| `skills/` | Agent skill definitions (self-documented via SKILL.md files) |
| `templates/` | Template files for knowledge, soul, user memory (see `templates/AGENTS.md`) |
| `tests/` | Test suites including `input_pipeline/` (see `tests/AGENTS.md`) |
| `plugins/` | Bot plugins (see `plugins/AGENTS.md`) |

## Testing

```powershell
python -m pytest examples/bot_project/tests -q
cd examples/bot_project/webui && npm test -- --run
```

Backend tests cover WebUI endpoints, streaming isolation, pool routing, input pipeline stages, and transcript store. Frontend tests cover the `useWebUIStream` reducer for per-conversation event filtering and `request_id`-based message dedup.

<!-- MANUAL -->
