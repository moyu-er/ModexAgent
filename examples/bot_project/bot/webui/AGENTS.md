<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-13 -->

# webui

WebUI backend — aiohttp server with REST API, WebSocket, and transcript storage. Serves the React frontend and provides real-time agent streaming.

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Package marker |
| `server.py` | `WebUIServer` — aiohttp HTTP+WS server with REST endpoints for sessions, pools, workspace, and WebSocket chat |
| `events.py` | WebUI event types — `ModelContentDelta`, `ModelReasoningDelta`, `ToolCallStart/End`, `TurnEnd`, `UserMessage` |
| `transcript_store.py` | `TranscriptStore` — per-agent JSONL transcript persistence for history replay |
| `emitter.py` | `WebBotEmitter` / `CompositeEmitter` — emits streaming events (content deltas, tool calls) to WebSocket clients |

## REST API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/pools` | List available pool names |
| GET | `/api/sessions` | List conversations (filtered by workspace and pool query params) |
| GET | `/api/sessions/{conv}/messages?all=true` | Load transcript events |
| DELETE | `/api/sessions/{conv}` | Delete conversation |
| GET | `/api/workspace` | Current workspace path |
| GET | `/api/workspace/browse?path=...` | Directory browser for workspace selection |
| POST | `/api/workspace/cd` | Change workspace (`{"path": "/target"}`) |
| GET | `/ws` | WebSocket for real-time chat and streaming. Attach with `{uuid_prefix, pool}` for new conversations, or `{session_id}` for existing ones. |

## Conversation Metadata

`WebUIServer` maintains `_conv_meta` (persisted to `conversations.json` in data dir):
- Each conversation tracks `pool` (assigned pool) and `workspace` (which workspace created it).
- `GET /api/sessions` filters by current workspace — conversations from other workspaces are hidden.
- IM conversations auto-fill `workspace` with current workspace on first encounter.

## WebSocket Session Management

Each WebSocket connection is tracked by `_WsConnectionState`:
- Registers the main session plus all pool-agent / subagent sessions for the attached conversation.
- On `attach` to a new conversation, **all previous sessions are unregistered and their forward tasks cancelled** — this prevents stale sessions from forwarding another conversation's stream after switching.
- On disconnect, all attached sessions and forward tasks are cleaned up.

## For AI Agents

### Working In This Directory
- `server.py` is the single entry point for all WebUI HTTP/WS interactions.
- Conversation metadata persistence uses `_conv_meta` dict backed by `conversations.json`.
- `set_pool_switch_callback()` and `set_workspace_context()` are late-binding — called by `WebUIService` after init.
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
- `bot/webui/events.py` — Event types shared between server and emitter
- `framework/workspace/context.py` — WorkspaceContext for cd/workspace APIs

### External
- `aiohttp` — HTTP/WS server framework

<!-- MANUAL -->
