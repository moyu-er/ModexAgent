<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-08-17 -->

# webui

WebUI backend — aiohttp server with REST API, WebSocket, and transcript storage. Serves the React frontend and provides real-time agent streaming.

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Package marker |
| `server.py` | `WebUIServer` — aiohttp HTTP+WS server constructor, late-binding configuration (`set_*` methods), workspace helpers, and route registration. Handler implementations live in `routes/`. |
| `types.py` | Shared types and constants — `_WsConnectionState`, `RuntimeStores`, `SessionListEntry`, `WorkspaceIndex`, `_safe_send_json`, `_materialize_partial_deltas`, path constants. Leaf dependency (no imports from `server.py`). |
| `routes/__init__.py` | Route package marker |
| `routes/models.py` | Models/Config/Restart routes — `GET /api/models`, `GET|PUT /api/config/{domain}`, `POST /api/system/restart`, `POST /api/models/fetch` |
| `routes/sessions/` | Session/Messages/Todos/Approvals/Attachments routes — `GET|POST /api/sessions`, `GET /api/sessions/{id}/messages`, todos, approvals, attachments, media config. Split into `__init__.py` (register + helpers), `lifecycle.py`, `messages.py`, `approvals.py`, `attachments.py` |
| `routes/workspace.py` | Workspace routes — `GET /api/workspace`, `POST /api/workspace/cd`, `POST /api/workspace/pick`, `GET /api/workspace/recent`, media tmp sweep |
| `routes/pool_config/` | Pool/MCP/Skills/Prompts routes — 22 handlers for `GET|POST|PUT|DELETE /api/pools/*`, `/api/mcp/*`, `/api/skills/*`, `/api/prompts/*`. Split into `__init__.py` (register + helpers), `pools.py`, `mcp.py`, `skills.py`, `prompts.py` |
| `routes/websocket/__init__.py` | WebSocket entry point + action dispatch — `GET /ws`, `dispatch_ws_message` |
| `routes/websocket/attach.py` | WS ATTACH action: session registration, eligibility-gated pool-route writes (rules in "Conversation Attribution"), deferred materialize |
| `routes/websocket/messaging.py` | WS SEND_MESSAGE action — user message → input pipeline → enqueue |
| `routes/websocket/control.py` | WS PAUSE + DELETE_CONVERSATION actions |
| `routes/websocket/graph.py` | WS SUBSCRIBE_GRAPH + UNSUBSCRIBE_GRAPH actions — graph event subscription, per-instance queue registry + `forward_graph_events` drain loop |
| `routes/websocket/streaming.py` | Delta forwarding — `forward_deltas`, `watch_new_queues`, queue connection filtering |
| `events.py` | WebUI event types — `ModelContentDelta`, `ModelReasoningDelta`, `ToolCallStart/End`, `TurnEnd`, `UserMessage` |
| `transcript_store.py` | `TranscriptStore` — per-agent JSONL transcript persistence for history replay |
| `emitter/` | `WebBotEmitter` / `CompositeEmitter` — emits streaming events (content deltas, tool calls) to WebSocket clients. Split into `__init__.py` (re-exports), `web_bot.py`, `_segments.py`, `composite.py` |

## REST API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/pools` | List available pool names |
| GET | `/api/sessions?pool=&ws=` | List sessions visible in the current workspace. `?pool=X` filters to one pool; `?ws=<path>` scopes to a specific workspace (empty = home). Hard-partitioned by workspace. |
| POST | `/api/sessions` | Create a new session. Body `{"pool": "...", "ws": "..."}`. |
| GET | `/api/sessions/{conv}/messages?ws=&pool=` | Load transcript events (user messages + materialized assistant turns). |
| GET | `/api/sessions/{conv}/todos?ws=&pool=` | Load active todos. |
| GET | `/api/sessions/{conv}/approvals?ws=&pool=` | Load pending approvals. |
| POST | `/api/sessions/{conv}/approvals?ws=&pool=` | Submit approve/deny decision. |
| GET | `/api/sessions/{conv}/attachments/{att_id}?ws=&pool=` | Download attachment. |
| POST | `/api/sessions/{conv}/attachments?ws=&pool=` | Upload attachment (multipart). |
| DELETE | `/api/sessions/{conv}?ws=&pool=` | Delete session (full session id) from transcript + session index. |
| GET | `/api/workspace` | Home path, recent workspaces, and timezone. |
| GET | `/api/workspace/browse?path=...` | Directory browser for workspace selection. |
| POST | `/api/workspace/cd` | Change workspace (`{"path": "/target"}`). |
| GET | `/api/workspace/recent` | Recently visited workspace paths. |
| GET | `/ws` | WebSocket for real-time chat and streaming. Attach with `{uuid_prefix, pool, ws}` for new conversations, or `{session_id, ws, pool}` for existing ones. `send_message` payload includes `_request_id` for optimistic-message dedup and `pool` for pool-scoped writes. |

## Conversation Attribution

The server does NOT keep a single metadata dict. Pool ownership, pool routing, workspace scope, and session records are owned by different layers:

- **Pool attribution: session tree first, prefix route only for legacy sessions.** The shared seam is `WebUIServer._resolve_session_pool(session_id, ws_raw)`: exact session-id ownership via the materialized workspace's `PoolWorkspaceResources.session_pool_index`, resolved through `_graph_workspace_resolver`, then `PoolSessionStore` prefix routing only when the session has no tree node. Unknown to both returns `None` (the session is omitted from listings, never silently assigned to the default pool). Session list, `?pool=` filtering, transcript derivation, and DELETE consume this seam (`_resolve_session_pool_for_request` adds client-pool-first for DELETE). Pool is never inferred from `agent_name`; the prefix route must never answer ownership (attribution contract in `bot/service/AGENTS.md`).
- **Pool routing: first-class request parameter with gated writes.** Routing (which pool handles the conversation's next message) is carried by every request (`?pool=` on REST, `pool` in WS payload); `_resolve_pool_for_request(client_pool, session_prefix)` is the request-level convergence, client pool → prefix store → default (`server.py:291-304`). Only explicit sources persist: a WS send with a client pool sets `explicit_pool`; a send without one carries tree ownership as `RoutingMeta.TREE_RESOLVED_POOL` (`routes/websocket/messaging.py:101-106,183`), which S5 resolves but never writes; IM sends (no explicit key) read the stored route without writing (`bot/input_pipeline/stages/resolve_pool.py:115-117`).
- **WS attach writes routing under exactly three rules** (`routes/websocket/attach.py:195-210`): (1) a new-conversation bootstrap (`{uuid_prefix, pool}`) writes unconditionally; (2) an existing-session attach writes only when the attached id equals `{session_prefix}.{root agent name of the currently routed pool}` (the routed pool is resolved WITHOUT the client override; this is a pool-switch intent); (3) every other attach (viewing a peer or subagent session) only registers forwarding and never writes. Writes go through `_pool_switch_callback`, with a direct `pool_session_store.set` failsafe when the callback is unwired.
- **Envelopes and transcripts take pool from the emitter's constructor-owned value.** `WebBotEmitter` stores `pool` at construction (`emitter/web_bot.py:90,104`); transcript appends and `DeltaEnvelope.pool` use only that value, and `None` becomes `""`, i.e. no self-reporting (`emitter/web_bot.py:142`, `emitter/_segments.py:114`). Ownership is fixed by the factory at emit time; there is no per-emit pool lookup.
- **Workspace attribution — the `?ws=` query param.** There is no persisted workspace field. The workspace is supplied per-request and resolved by the `_ws_root_of` / `_sessions_dir_of_ws` / `_index_dir_of_ws` helpers, which map the raw `ws` value to a workspace root, a transcript sessions dir, and a session-index dir. Empty `ws` means the home workspace. Every read path (list, load, delete, attach) and every write path (create session, send message) routes through the same resolver, so a message written under a workspace is always read back from that workspace — never leaked to another.
- **Session records — `SessionStore` (JSONL index).** `SessionInfo` records (session id, agent name, parent, timestamps) live in the per-workspace session-index directory resolved by `_index_dir_of_ws`. The concrete store is `WorkspacePoolSessionStore` (`bot/service/session_store.py`). When the index is empty/missing, `_derive_sessions_from_transcripts` falls back to deriving records from the transcript files so legacy workspaces still render.

`GET /api/sessions` lists ONLY the index/transcript entries under the resolved workspace; conversations from other workspaces are hidden by construction.

## WebSocket Session Management

Each WebSocket connection is tracked by `_WsConnectionState`:
- Registers the main session plus all pool-agent / subagent sessions for the attached conversation.
- On `attach` to a new conversation, **all previous sessions are unregistered and their forward tasks cancelled** — this prevents stale sessions from forwarding another conversation's stream after switching.
- On disconnect, all attached sessions and forward tasks are cleaned up.

## WebSocket Graph Event Subscriptions

`subscribe_graph` / `unsubscribe_graph` (PRD §11.2, graph-visualization-redesign) open an instance-scoped event channel on the same connection:
- Subscribe registers a per-connection `asyncio.Queue` in the workspace's `graph_event_subscribers[instance_id]` (on `PoolWorkspaceResources`, assembled with `graph_event_store`) and starts a `forward_graph_events` drain loop; events arrive as `{"type": "graph_event", "graph_instance_id": "<str>", "event": <GraphOutput.model_dump(mode="json")>}` (id as `str` — snowflake ids exceed JS `MAX_SAFE_INTEGER`).
- The only event source is `WebUIGraphOutputAdapter.emit()` (dual channel: event store for REST polling + fan-out to subscriber queues). WS handlers never emit events themselves.
- Graph subscriptions are orthogonal to `attach`: switching conversations does NOT clear them (`cleanup(include_graphs=False)`); unsubscribe and disconnect deregister queue + cancel task via `cleanup_graph_subscriptions()`.

## For AI Agents

### Working In This Directory
- `server.py` is the single entry point for all WebUI HTTP/WS interactions.
- User messages flow through the **input pipeline** before reaching `PoolRouter` — `_ws_send_message` produces a seed `UserInputEnvelope` and runs the WebUI pipeline (S4→S5→S6→S7→S8).
- The server echoes `_request_id` from the WS payload back in envelope metadata so the frontend can deduplicate optimistic messages.
- Conversation attribution is split (see "Conversation Attribution" above): pool ownership resolves tree-first via `SessionPoolIndex`, with the `PoolSessionStore` prefix route only as a legacy fallback; routing is a first-class parameter carried by every request (`?pool=` on REST, `pool` in WS payload) whose writes are gated to explicit intent (S5 `explicit_pool`, attach bootstrap / main-session switch). Workspace is derived from `?ws=` via `_ws_root_of` / `_sessions_dir_of_ws` / `_index_dir_of_ws`, and session records live in the per-workspace `SessionStore` JSONL index. There is no `_conv_meta` dict, no `conversations.json`, and no `agent_name → pool` reverse-lookup map: pool is never inferred from agent_name.
- Late-binding configuration is injected by `WebUIService` after init via `set_pool_switch_callback()`, `set_workspace_control()` (NOT `set_workspace_context`), `set_pool_resolver()`, `set_graph_workspace_resolver()`, `set_agent_resolver()`, `set_workspace_index()`, `set_session_store()`, `set_session_factory()`, `set_input_pipeline()`, `set_input_context()`, `set_data_dir_name()`, `set_pool_agent_names()`, `set_recent_workspaces()`.
- WebSocket messages follow `action`/`payload` protocol defined in `events.py`.
- **Session isolation**: switching conversations via `attach` must unregister every previous session (main + pool agents + subagents), not just the main session. `_WsConnectionState.cleanup()` handles this.
- **Multicast delta queues**: each session maps to one delta queue PER attached connection (workspace tabs can duplicate a conversation across pods). `register_connection` returns the caller's own queue, `send_envelope` fans out to all of a session's queues, and `cleanup`/`unregister_connection` only ever touch the calling connection's queues — closing one tab must never kill another tab's stream. `register_subagent(child, parent)` is the dispatch-time seam that pairs the anonymous pre-attach buffer with its genealogy link; the buffer is adopted by the first registrant, or dropped on `turn_end` if no ancestor has a live connection (IM-driven turns no browser opens), so queue entries never accumulate unboundedly. The genealogy map itself is append-only — late envelopes after all observers detach still resolve parent ids (`get_parent`/`ancestors`).

### Testing
- Tests in `tests/webui/` cover server endpoints, pool routing, transcript store, events, fan-in, and streaming isolation.
- Use `aiohttp.test_utils.TestClient` with `TestServer`.
- End-to-end streaming tests use the real `WebBotEmitter` → `WebSocketOutputAdapter` → `WebUIServer` pipeline.

## Dependencies

### Internal
- `bot/adapters/web_socket.py` — WebSocket input adapter
- `bot/adapters/fan_in.py` — Multi-agent output fan-in
- `bot/input_pipeline/` — Converged pipeline (server.py produces seed envelopes, runs WebUI pipeline)
- `bot/webui/events.py` — Event types shared between server and emitter
- `modex_agent/workspace/context.py` — WorkspaceContext for cd/workspace APIs
- `modex_agent/input_pipeline/` — Generic input pipeline abstractions

### External
- `aiohttp` — HTTP/WS server framework

<!-- MANUAL -->
