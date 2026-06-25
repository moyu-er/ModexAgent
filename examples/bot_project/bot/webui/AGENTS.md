<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-21 -->

# webui

WebUI backend — aiohttp server with REST API, WebSocket, and transcript storage. Serves the React frontend and provides real-time agent streaming.

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Package marker |
| `server.py` | `WebUIServer` — aiohttp HTTP+WS server. REST endpoints for sessions, pools, workspace. WebSocket chat routes through input pipeline; echoes `_request_id` in envelope metadata for frontend dedup. |
| `events.py` | WebUI event types — `ModelContentDelta`, `ModelReasoningDelta`, `ToolCallStart/End`, `TurnEnd`, `UserMessage` |
| `transcript_store.py` | `TranscriptStore` — per-agent JSONL transcript persistence for history replay |
| `emitter.py` | `WebBotEmitter` / `CompositeEmitter` — emits streaming events (content deltas, tool calls) to WebSocket clients |

## REST API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/pools` | List available pool names |
| GET | `/api/sessions?pool=&ws=` | List sessions visible in the current workspace. `?pool=X` filters to one pool; `?ws=<path>` scopes to a specific workspace (empty = home). Hard-partitioned by workspace. |
| POST | `/api/sessions` | Create a new session. Body `{"pool": "...", "ws": "..."}`. |
| GET | `/api/sessions/{conv}/messages?ws=` | Load transcript events (user messages + materialized assistant turns). |
| DELETE | `/api/sessions/{conv}?ws=` | Delete session (full session id) from transcript + session index. |
| GET | `/api/workspace` | Home path, recent workspaces, and timezone. |
| GET | `/api/workspace/browse?path=...` | Directory browser for workspace selection. |
| POST | `/api/workspace/cd` | Change workspace (`{"path": "/target"}`). |
| GET | `/api/workspace/recent` | Recently visited workspace paths. |
| GET | `/ws` | WebSocket for real-time chat and streaming. Attach with `{uuid_prefix, pool, ws}` for new conversations, or `{session_id, ws}` for existing ones. `send_message` payload includes `_request_id` for optimistic-message dedup. |

## Conversation Attribution

The server does NOT keep a single metadata dict. Conversation attribution is split across three mechanisms, each owned by a different layer:

- **Pool attribution — `PoolSessionStore`** (`bot/service/pool_router.py`). A service-singleton that persists session-prefix → pool-name as one JSON file per conversation under `pool_sessions/`. It is written on two paths: (1) by the S5 `ResolvePoolStage` (`bot/input_pipeline/stages/resolve_pool.py`) on every turn, which always re-persists the resolved pool; (2) by `_ws_attach` through `_pool_switch_callback` (with a failsafe direct write via `ctx.pool_session_store.set(...)` when the callback is not wired). `PoolRouter` reads it to dispatch each message.
- **Workspace attribution — the `?ws=` query param.** There is no persisted workspace field. The workspace is supplied per-request and resolved by the `_ws_root_of` / `_sessions_dir_of_ws` / `_index_dir_of_ws` helpers, which map the raw `ws` value to a workspace root, a transcript sessions dir, and a session-index dir. Empty `ws` means the home workspace. Every read path (list, load, delete, attach) and every write path (create session, send message) routes through the same resolver, so a message written under a workspace is always read back from that workspace — never leaked to another.
- **Session records — `SessionStore` (JSONL index).** `SessionInfo` records (session id, agent name, parent, timestamps) live in the per-workspace session-index directory resolved by `_index_dir_of_ws`. The concrete store is `WorkspacePoolSessionStore` (`bot/service/session_store.py`). When the index is empty/missing, `_derive_sessions_from_transcripts` falls back to deriving records from the transcript files so legacy workspaces still render.

`GET /api/sessions` lists ONLY the index/transcript entries under the resolved workspace; conversations from other workspaces are hidden by construction.

## WebSocket Session Management

Each WebSocket connection is tracked by `_WsConnectionState`:
- Registers the main session plus all pool-agent / subagent sessions for the attached conversation.
- On `attach` to a new conversation, **all previous sessions are unregistered and their forward tasks cancelled** — this prevents stale sessions from forwarding another conversation's stream after switching.
- On disconnect, all attached sessions and forward tasks are cleaned up.

## For AI Agents

### Working In This Directory
- `server.py` is the single entry point for all WebUI HTTP/WS interactions.
- User messages flow through the **input pipeline** before reaching `PoolRouter` — `_ws_send_message` produces a seed `UserInputEnvelope` and runs the WebUI pipeline (S4→S5→S6→S7→S8).
- The server echoes `_request_id` from the WS payload back in envelope metadata so the frontend can deduplicate optimistic messages.
- Conversation attribution is split (see "Conversation Attribution" above): pool lives in `PoolSessionStore` (written by S5 + the attach callback), workspace is derived from `?ws=` via `_ws_root_of` / `_sessions_dir_of_ws` / `_index_dir_of_ws`, and session records live in the per-workspace `SessionStore` JSONL index. There is no `_conv_meta` dict and no `conversations.json`.
- Late-binding configuration is injected by `WebUIService` after init via `set_pool_switch_callback()`, `set_workspace_control()` (NOT `set_workspace_context`), `set_pool_resolver()`, `set_agent_resolver()`, `set_workspace_index()`, `set_session_store()`, `set_session_factory()`, `set_input_pipeline()`, `set_input_context()`, `set_data_dir_name()`, `set_agent_pool_map()`, `set_pool_agent_names()`, `set_recent_workspaces()`.
- WebSocket messages follow `action`/`payload` protocol defined in `events.py`.
- **Session isolation**: switching conversations via `attach` must unregister every previous session (main + pool agents + subagents), not just the main session. `_WsConnectionState.cleanup()` handles this.

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
