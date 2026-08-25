<!-- Updated: 2026-08-22 | scope-assembly doc sync -->

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
- **Session**: Agent-owned scope, format `{prefix}.{agent_name}` (two dot-separated segments; the prefix is `encode_snowflake(conversation_id)` for main sessions and the verbatim `invocation_id` for subagent sessions — never a third segment).

### Workspace Model (multi-live)

The workspace system lives in `bot/workspace/` (business) backed by `modex_agent/workspace/` (generic). Key properties:

1. **Multi-live**: Many workspaces coexist in a `WorkspaceRegistry`. Switching mutates only a per-session pointer (`SessionWorkspaceMap`), not a global `_active`. No `os.chdir`, no busy-check.
2. **Snapshot safety**: In-flight turns hold a `PipelineSnapshot` with pinned workspace references, unaffected by mid-turn switches.
3. **Lazy materialization**: Heavy resources (`PoolWorkspaceResources`) are built on first use via `PoolResourceFactory`, cached, and LRU-evictable. WorkspaceContext (identity) is cheap and always retained.
4. **Safe paths**: `WorkspacePaths` in `modex_agent/workspace/paths.py` provides containment-checked path accessors.
5. **Per-workspace isolation**: Each workspace owns its own broker/inbox/bus/interceptor. Inbox cross-consume is structurally impossible.
6. **Stack shape** (ticket 14): a workspace-layer scope declaration (`config/scopes/bot.yml`) boots full multi-live; its absence (pool-as-root or no declaration) boots the single-home stack (no `/cd`). Data layout is identical.

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
| `bot/service/builders.py` | Service-level construction helpers (inbox/turn-state/session/routing/todo stores, external session map, slash-command processor) — tool construction glue is gone (scope assembly resolves tools from the compiled declaration) |
| `bot/service/pool/` | Pool mode assembly — creates `AgentPool` from the compiled scope declaration (`declaration.py` boots: load → validate → compile; `factory.py` assembles). Split into 8 focused modules |
| `modex_agent/multi_agent/pool_router.py` | `PoolRouter` (framework) — session→pool dispatch shell, `PoolRoutingStore` persistence, declaration-lookup agent→pool ownership |
| `modex_agent/multi_agent/pool_instance.py` | `PoolInstance` — pool runtime holder (config, pool, root agent name) |
| `bot/workspace/wiring/` | `build_workspace_stack` — workspace assembly (stack + resources; the workspace layer's resource selection — memory backend/path layout/MCP set — is declared in `config/scopes/bot.yml`, ticket 14) |
| `bot/workspace/handle.py` | `PoolWorkspaceResources` — per-workspace resource bundle |
| `bot/workspace/dispatch.py` | `WorkspaceMessageDispatcher` — per-message workspace routing |
| `bot/workspace/pool_data.py` | `PoolData` — frozen per-pool data bundle |
| `modex_agent/workspace/registry.py` | `ScopeRegistry[R]` (renamed from `WorkspaceRegistry`) — multi-live workspace holder with lazy resource materialization |
| `modex_agent/workspace/scope_path.py` | `ScopePath` + `resolve_scope_path` — the one scope-path resolver (addressing convergence) |
| `modex_agent/workspace/routing.py` | `SessionWorkspaceMap` — per-session workspace pointer |
| `bot/service/web_ui_service.py` | `WebUIService` — the single IM + WebUI entry point; auto-discovers every `bot/adapters/register_*.py` (QQ / Telegram / WebSocket), assembles and starts the HTTP/WS server |
| `bot/service/qq_service.py` | `QQBotService` — standalone QQ-only `BotService` variant (the CLI start path uses `WebUIService`) |
| `bot/adapters/qq/` | QQ platform input/output adapters (C2C + group + file upload) — split into 5 modules |
| `bot/adapters/telegram.py` | Telegram input/output adapters (long-polling inbound, HTML chunked outbound) |
| `bot/adapters/web_socket.py` | WebSocket input adapter for WebUI real-time chat |
| `bot/adapters/fan_in.py` | Multi-agent output fan-in for WebUI (merges agents' streams to one WS) |
| `bot/adapters/channels.py` | Multi-channel spine — `@register` adapter registry (`ADAPTERS`), `ChannelRouterOutputAdapter`, conversation→channel tracking |
| `bot/webui/server.py` | aiohttp REST+WS server (sessions, pools, workspace APIs) |
| `bot/webui/transcript_store.py` | Per-agent transcript persistence (JSONL) for history replay |
| `bot/webui/events.py` | WebUI event types (model deltas, tool calls, turn lifecycle) |
| `bot/webui/emitter/` | Emits WebUI events via fan-in adapter — split into 4 modules |
| `bot/graph/` | Graph scheduling bridge — `BotAgentNode` (agent-backed graph node), `BotAgentNodeFactory` (spec → node), `GraphSpecLoader` (YAML → compiled spec store), `WebUIGraphOutputAdapter` (dual-channel event emission: REST store + WS fan-out) |
| `bot/webui/routes/graph_routes.py` | Graph REST API — specs CRUD, instance lifecycle (run/pause/resume/stop), events, deliver, topology endpoint |
| `modexbot/cli.py` | CLI entry point — 3-layer process discovery for start/stop/restart |
| `modexbot/main.py` | CLI→service bootstrap |
| `config/bot_config.yml` | Runtime safety, memory, tool, observability config. `${ENV_VAR}` interpolation |
| `config/mcp/*.json` | MCP server registry (stdio/SSE/streamable_http) |
| `config/scopes/bot.yml` | Scope declaration — the single source of pool/agent assembly (workspace resource selection + all pool trees + peer links) |
| `config/scopes/eval/` | Eval-only declaration face — pool-mode arm overlays plus pool-as-root single-agent harness declarations; never loaded by production boot |
| `config/graphs/*.yml` | Declarative graph specifications (DAG workflows) — loaded by `GraphSpecLoader` at startup |

## Multi-Agent Setup

- `default` pool: General-purpose assistant with file/shell tools, MCP tools (playwright), communication tools + subagents (office-expert).
- `coder` pool: Main agent (orchestrator) + subagents (explore, general). Orchestrator is the primary implementer — investigates, plans, writes code, and verifies. Delegates to explore for read-only codebase investigation and to general for tasks needing a fresh, isolated context.
- Communication: `task` (subagent dispatch, agents with declared children)
  + `send_to_peer` (peer messaging, session-mode only, roots of pools with
  links) + `send_to_agent` (subagent→parent consultation, every non-root).
  All converge on `AgentCommunicationService.send_async`. The three tools are
  compiler-derived entries in each agent's compiled spec — injected by the
  ScopeCompiler from the declaration tree (never roster-declared), resolved at
  assembly time by the TOOL-slot FW factories.
- `SubagentAutoSendHook` auto-forwards subagent output to parent.
- Session ID format: `{prefix}.{agent_name}` (two segments, via `SessionIdFactory`; subagent sessions use the minted `invocation_id` as the prefix — see `SessionInfo.session_id_prefix`).

### External coding agent pools (Pi, OpenCode)

External CLI coding agents (Pi, OpenCode) can be registered as NORMAL main agents of their own dedicated pools. A framework-side harness (`ExternalAgent`) executes them through provider backends, and they communicate back through the `modexctl send` CLI. The CLI sends markdown message content through the target workspace's `InboxMQ.deliver()` implementation; `modexbot` is a backward-compatible facade over `modexctl`.

**Pool declaration** (a root agent in `config/scopes/bot.yml`):

```yaml
opencode:
  agents:
    opencode:                     # root agent (no parent)
      execution_strategy: external   # opt-in; default is "react"
      provider_kind: opencode          # "pi" or "opencode"
peers: [default]                   # on the pool — explicit peer declaration required
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

**WebUI:** external sessions appear in the WebUI session list with their `.pi` / `.opencode` suffix, alongside every other session. Streaming output (text, reasoning, tool calls/results, errors) is rendered through the canonical `TurnEvent` seam → `WebBotEmitter` projection into existing `ServerEvent`/transcript types. External pools are configured through the same scope declaration (Settings → Scope tab).

See ADR-0022 and `docs/design/external-agent-integration/` for the full design.

## Memory + Experience Presets (Target State)

All native agents receive memory + experience configuration from the single
converged source `modex_agent/scope/defaults.py` (position-derived memory
families backed by `modex_agent/memory/presets.py` + `modex_agent/multi_agent/pool_config/experience.py`).
A root agent's archive/core toggle is user-editable per pool through the WebUI
or the `memory:` block on the agent in `config/scopes/bot.yml`. The schema
enforces the AND relationship: core memory can be enabled only when archive
memory is enabled.

Detailed configuration remains baked: `ArchiveConfig`/`CoreMemoryConfig`
internals, dream-engine derivation, session, governance, pruned, and experience
settings are not per-pool overrides. Non-root agents stay session-only by
position default (the non-root memory family); a nested agent's `memory:`
block carries only the session token-budget override, and there is no
user-editable experience block.

### Preset surface (`modex_agent/memory/presets.py` + `modex_agent/multi_agent/pool_config/experience.py`)

| Preset | Used by | Contents |
|---|---|---|
| `main_agent_memory(max_context_tokens, archive_enabled, core_enabled)` | every native main agent | session (token-budget compression, `max_context_tokens` from `model.yml`) + governance (tool_chain_repair + lossy_compaction) + pruned. archive/core follow the per-pool `MemoryToggle`; dream is enabled only when both are on. |
| `main_agent_experience()` | every native main agent | `ExperienceConfig(enabled=True)` — fires `ExperienceReviewHook` |
| `subagent_memory()` | every native subagent | session + governance (tool_chain_repair only, NO lossy_compaction) + pruned. archive/core/dream = None. No experience preset — review is main-agent-only. |

### Wiring chain (consumers perform NO additional config construction)

Declaration road — the compiled spec carries the position-derived memory
family; the deps channel threads it to every consumer:

```
pool/declaration.py (boot): load bot.yml → validate → compile_scope
  └─ declared_pool_build (per pool): root PositionDefaults + overrides
       └─ wiring._position_deps → PoolAssemblyDeps(memory=…, experience=…)
            │
            ├─ pool_data.build_pool_data()
            │    ├─ create_memory_system(memory_cfg)        → MemorySystem (archive/core/dream/pruned layers)
            │    ├─ _build_experience_manager(exp_cfg)      → ExperienceManager (None when exp_cfg disabled)
            │    └─ MemorySystemContextManager(experience_manager=...)  → ExperienceProvider injects XML into system prompt
            │
            ├─ pool/pipeline_wiring.py
            │    └─ builder.governance = create_governance(memory)  → CompositeGovernance (lossy + tool_chain_repair)
            │
            ├─ bot.yml `hooks: [+experience_review, +user_notice_cleanup]`
            │    └─ HOOK-slot factories resolve at Stage 4 (ExperienceReviewHook via
            │       PoolRuntimeDeps.experience_review_provider — the bot-global default LLM
            │       from model.yml, running ReAct with forked parent history)
            │
            └─ background.BackgroundTaskRunner
                 └─ ExperienceCurator(experience_dir, meta_store, max_experiences)  → LRU eviction loop

create_pool() (subagents)
  └─ AgentTemplate.materialize()
       ├─ build_session_only_memory(cfg)          → session-only MemorySystem (archive/core = None)
       ├─ factory.create_subagent_governance(cfg) → ToolChainRepairGovernance only
       └─ resolver.pruned_manager()               → reuses the parent pool's PrunedManager
```

### Memory lifecycle hooks

Memory cleanup dispatches lifecycle events through a dedicated
`MemoryHookRunner` (separate from the ReAct `HookRunner`) — no ReAct
coupling. Two hooks are registered in deterministic order at pool assembly
(`factory.py`):

1. **`UserNoticeCleanupHook`** (`CleanupTriggeredHook` +
   `CleanupFinishedHook`) — sends transient user-facing notices
   ("Consolidating conversation memory, please wait…" / "Memory
   consolidated.") via `AgentNotificationService.send_notice`. Notices are
   never written to session memory.
2. **`TodoReorientationHook`** (`CleanupFinishedHook`, in
   `modex_agent.memory.cleanup_hooks`) — persists a `<system-reminder>` USER
   message so the agent re-orients after compaction prunes messages.

Both register via `memory_system.add_cleanup_hook(hook)` on the shared
`DefaultMemorySystem._hook_runner` (one runner per memory system, passed by
reference to every `ScopedMessageHistory` — late registration is visible to
all histories).

**Event flow**: `cleanup_session()` dispatches `CLEANUP_TRIGGERED` (after
early-return checks, before compact generation) and `CLEANUP_FINISHED`
(before every `triggered=True` return). Dispatch uses a tuple-snapshot of
registered hooks with 10s per-hook timeout and log-and-continue error
isolation — cleanup never aborts due to a hook failure.

**Todo reorientation persistence**: `TodoReorientationHook` detects cleanup
purely via `cleanup_result.messages_pruned > 0` (event-driven, no heuristic
history-diff). It persists the reminder through
`SessionMemoryManager.add_messages` directly (Path A) — NOT
`ScopedMessageHistory.append` — bypassing `MemoryAppendRecorder` /
`MemoryProvider` fan-out and preventing cleanup recursion. The reminder is
visible on the next iteration via `ScopedMessageHistory.to_list()`.

### External (external) exclusion — structural, not config-based

External main agents and subagents are excluded at **three** independent
points, so the presets never reach them regardless of config:

1. **Subagent**: `AgentTemplate.materialize` (`template.py`) early-dispatches
   to `_materialize_external` when `execution_strategy == EXTERNAL` —
   skips native memory/tool/skill/hooks assembly entirely.
2. **Main agent pipeline**: `pool.create_pool` (`pool/factory.py`)
   takes the external branch (capability-flag driven, not identity checks),
   skipping `_wire_main_pipeline` (no governance, no hooks, no approval
   renderer).
3. **Experience hook**: the external strategy's `assemble_main()` returns a
   `StrategyAssembly` with no pipeline and no memory/experience
   collaborators — "external doesn't consume memory" is the strategy
   component's explicit return shape, not a scattered call-site check.

### Experience review mechanism (three coupled components)

The experience system is **reviewer + hook + injection** working together.
All three must be active for experience to function:

1. **ExperienceManager** (`pool_data.py:151`): built when `assembly_deps.experience`
   is enabled. Held by `MemorySystemContextManager`. At turn load
   (`system.py:366-377`), `build_prompt()` renders saved experiences as XML
   metadata → `ExperienceProvider` injects into the main agent's system prompt
   so the LLM sees `<available_experiences>` and can call the `experience` tool.

2. **ExperienceReviewHook** (a HOOK-slot roster reference `+experience_review`
   in `bot.yml`, resolved at Stage 4): registered on the main
   agent's `pipeline.hook_runner`. Fires `after_graph` when
   `stop_reason == completed` and history ≥ `min_messages`. Spawns a
   background task that runs `ExperienceReviewAgent.review()` — a ReAct loop
using **the bot-global default LLM provider** (`service._default_provider`,
from `model.yml`) with **forked parent history** (`conversation_messages`
parameter) so the reviewer sees the full structured conversation (tool_calls,
tool_results) rather than a flattened text snapshot. The provider is shared
across all pools (native and external) — experience review is a background
task that does NOT depend on any pool's own provider. When `default_provider`
is None (model.yml unconfigured), experience review is skipped with a
warning; the bot itself boots and runs normally.

3. **ExperienceCurator** (`background.py:109-178`): a background loop per pool
   that runs LRU eviction when experience count exceeds `max_experiences`.
   Pinned experiences are immune; the least-recently-used unpinned ones are
   deleted permanently.

If any of these three is missing, experience degrades silently:
- No `ExperienceManager` → no `<available_experiences>` in system prompt →
  LLM never learns from past sessions.
- No `ExperienceReviewHook` → no review after turns → EXPERIENCE.md files
  never created/updated.
- No `ExperienceCurator` → no eviction → experience dir grows unbounded.

### Governance derivation

Governance is **derived from memory config**, never configured independently:

- **Main agent**: `pool/pipeline_wiring.py` calls `create_governance(memory)` →
  `CompositeGovernance` with `LossyContentCompactionGovernance` (truncates
  oversized tool_results/assistant/user content per `LossyConfig`) +
  `ToolChainRepairGovernance` (repairs broken tool_call/tool_result pairing
  before LLM call). Injected onto `turn_context_builder.governance`.
- **Subagent**: `factory.py:292` calls `create_subagent_governance(memory)` →
  `ToolChainRepairGovernance` only (no lossy compaction — subagents are
  short-lived task workers with small context windows). Injected onto
  `turn_context_builder.governance` via `_build_turn_runner`.

### Why `subagent_memory()` carries `governance` and `pruned` fields

These are **consumed at different points** than `build_session_only_memory`:

- `governance`: consumed by `factory.create_subagent_governance(descriptor.memory_config)`
  (`factory.py:292`), NOT by `build_session_only_memory`. The subagent's
  `MemorySystemContextManager` has no governance field — governance lives on
  `TurnContextBuilder`.
- `pruned`: the subagent's `PrunedManager` comes from
  `resolver.pruned_manager()` (`template.py:156`), which returns the
  **parent pool's** `PrunedManager`. The `pruned` field in `subagent_memory()`
  is structurally redundant but kept for symmetry with `main_agent_memory()`.
- `archive`/`core`/`dream_engine`: `None` — correctly ignored by
  `build_session_only_memory` (subagents have no long-term layers).

## Skills (global library + per-agent assignment)

The global skill **library** has two sources, REPO PRIORITY:

- `local_skills/<name>/` — the repo library (CRUD target: `upload_skill` /
  `delete_skill` operate here only). A sibling of `skills/`, outside the
  per-pool tree so it can never collide with a pool literally named "global".
- `~/.agents/skills/<name>/` — user-installed skills (read-only augment; may
  themselves be links). On a name clash the repo copy wins
  (`SkillsStore._resolve_global_source`).

Per-agent skill dirs live at `skills/<pool>/<agent>/<name>/`. Disk is the single
source of truth: neither the scope declaration nor the WebUI carries a `skills`
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

`todo_write` (full-replace) and `todo_read` (active-only: pending + in_progress) let the agent track a multi-step task list per session. The `TodoStore` comes from the supplied-infra chain (`build_pool_todo_store` → `pool_runtime.todo_store` → the FW `TodoToolFactory`); persisted to `<ws>/.modex/runtime_state/<pool>/todos/<session_id>.json` (ws+pool+session isolated).

- **Decoupled from the WebUI**: the tool does NOT emit a presentation event. The
  WebUI derives the task panel from the generic `tool_call_end` stream (the tool's
  JSON result is parsed out of `result_summary`). On history load, the panel
  scans the loaded assistant messages for the most recent todo tool block with a
  result and parses it. If history doesn't carry results reliably, fall back to
  a server fetch endpoint (see spec §12 — deferred).
- Registered via `tool_supplements: [todo]` on any agent in the scope declaration (root or nested). Both root agents and subagents can opt in; the bundled nested agents (office-expert, explore, general) all include `todo`. `TodoContinuationHook` is registered on every agent (root + nested) via `register_tree_aware_hooks` in `hook/wiring.py` — the shared convergence function called from both `_wire_main_pipeline` and `AgentTemplate.materialize`.
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
| `templates/` | Template files for core memory, soul, user memory (see `templates/AGENTS.md`) |
| `tests/` | Test suites including `input_pipeline/` (see `tests/AGENTS.md`) |
| `plugins/` | Bot plugins (see `plugins/AGENTS.md`) |
| `experiences/` | Self-learned EXPERIENCE.md storage — runtime-populated by `ExperienceReviewAgent` (not committed; created on first use) |
| `docs/langfuse/` | Langfuse usage guides — `langfuse-deployment.md`(deploy + eval + training-data)、`trace-reading-guide.md`(span/metric 含义、面板操作、问题排查) |
| `packaging/` | Windows installer build — Inno Setup + Tauri + python-build-standalone (see `packaging/README.md`) |
| `subworkspace/` | Workspace isolation/runtime target — runtime-populated (`.modex/` state only; not committed) |

## Testing

```powershell
python -m pytest examples/bot_project/tests -q
cd examples/bot_project/webui && npm test -- --run
```

Backend tests cover WebUI endpoints, streaming isolation, pool routing, input pipeline stages, transcript store, graph scheduling (node-level events, WS subscription protocol, topology endpoint), and graph REST routes. Frontend tests cover the `useWebUIStream` reducer, `request_id`-based message dedup, graph topology components (deliver pulse, active ring, diff logic, YAML editor, execution viewer, spec/instance list pages), and i18n/token coverage.

<!-- MANUAL -->

## Observability, Eval & Training Data

When tracing agent trajectories, running eval experiments, replaying golden
cassettes, or exporting SFT/DPO training data, read
`docs/langfuse/langfuse-deployment.md` — the single usage guide covering
Langfuse v4 deployment, `.env` configuration, the 12 trajectory metrics, eval
CLI commands, golden cassette 4-gate replay, `TrainingDataExporter` API, and
troubleshooting. To **read traces in the Langfuse panel** — understanding
span types, trace_id/span_id, the 12 metrics, and filtering for problem
diagnosis — read `docs/langfuse/trace-reading-guide.md`. Architecture details
live in `bot/eval/AGENTS.md` and `src/modex_agent/trace/AGENTS.md`.

<!-- BEGIN MODEX-RUNTIME (auto-managed; do not edit) -->
## ModexAgent runtime

You are running as a subagent inside ModexAgent.

Your final reply is your deliverable — it is forwarded to your caller automatically when your turn ends. Output your result in your reply text.

Use `modexctl send --to <name> --content <text>` only to ask a question or request a decision when you cannot proceed without input.

- The `.modex/` directory is framework-managed internal state. Do NOT read, modify, or delete anything under `.modex/`.
<!-- END MODEX-RUNTIME -->
