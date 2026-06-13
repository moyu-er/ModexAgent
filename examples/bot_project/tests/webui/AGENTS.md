<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-13 -->

# tests/webui

Tests for the WebUI backend and frontend — server endpoints, WebSocket adapter, event streaming, pool routing, transcript store, process management, and React reducer behavior.

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Package marker |
| `test_server.py` | REST API endpoint tests — sessions CRUD, pools listing, workspace browse/cd, WebSocket attach/switch, streaming isolation |
| `test_pool_routing.py` | Pool routing tests — conversation→pool mapping, `/pool_name` switching, workspace filtering |
| `test_integration.py` | End-to-end integration tests — transcript roundtrip, event JSON roundtrip, multi-agent threads |
| `test_web_socket_adapter.py` | `WebSocketInputAdapter` lifecycle tests |
| `test_webui_emitter.py` | `WebBotEmitter` event emission tests |
| `test_events.py` | Event type serialization and deserialization |
| `test_transcript_store.py` | `TranscriptStore` persistence and retrieval |
| `test_fan_in_transcript.py` | Fan-in adapter multi-agent transcript merging |
| `test_process_stop.py` | Process stop/restart behavior via CLI |
| `test_cli.py` | CLI command parsing and execution |

## Frontend Tests

Frontend reducer tests live under `webui/src/hooks/`:

| File | Description |
|------|-------------|
| `webui/src/hooks/useWebUIStream.reducer.test.ts` | Pure reducer tests — session-scoped event filtering (`model_content_delta`, `turn_end`) |

Run frontend tests with:

```bash
cd examples/bot_project/webui
npm test -- --run
```

## For AI Agents

### Testing Requirements
- Backend: `python -m pytest examples/bot_project/tests/webui -q`
- Frontend: `cd examples/bot_project/webui && npm test -- --run`
- Server tests use `aiohttp.test_utils.TestClient` with `TestServer`.
- WebSocket tests verify the full action/attach/send protocol and session cleanup on switch.
- Pool routing tests cover both IM `/pool_name` commands and WebUI `set_pool()` external calls.

### Common Patterns
- `test_pool_routing.py` is the most comprehensive test for workspace/pool interaction.
- `test_server.py::test_ws_full_stream_isolation_across_conversations` exercises the real emitter → adapter → server → WebSocket pipeline.
- `test_server.py::test_ws_turn_end_streaming_stop_is_isolated` verifies `turn_end` scoping.
- Use `TestFixture` pattern: create `WebUIServer` with mock adapters, transcript store, and temp data dir.

<!-- MANUAL -->
