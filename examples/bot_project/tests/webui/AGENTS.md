<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-22 -->

# tests/webui

Tests for the WebUI backend and frontend — server endpoints, WebSocket adapter, event streaming, pool routing, transcript store, workspace isolation, process management, and React reducer behavior.

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Package marker |
| `_pipeline_fixture.py` | Shared pipeline test fixtures (stub registries, mock adapters, test context) |
| `test_server.py` | REST API endpoint tests — sessions CRUD, pools listing, workspace browse/cd, WebSocket attach/switch, streaming isolation, `_request_id` echo |
| `test_integration.py` | End-to-end integration tests — transcript roundtrip, event JSON roundtrip, multi-agent threads, pipeline convergence |
| `test_events.py` | Event type serialization and deserialization |
| `test_envelope.py` | `DeltaEnvelope` structured transport tests |
| `test_transcript_store.py` | `TranscriptStore` persistence and retrieval |
| `test_transcript_persistence_e2e.py` | Transcript persistence end-to-end |
| `test_transcript_workspace_resolver.py` | Per-workspace transcript resolution |
| `test_webui_emitter.py` | `WebBotEmitter` event emission tests |
| `test_web_socket_adapter.py` | `WebSocketInputAdapter` lifecycle tests |
| `test_fan_in_transcript.py` | Fan-in adapter multi-agent transcript merging (uses pipeline fixtures) |
| `test_channel_router.py` | `ChannelRouterOutputAdapter` routing tests |
| `test_adapter_discovery.py` | Adapter discovery / registration tests |

### Pool routing

| File | Description |
|------|-------------|
| `test_pool_routing.py` | Conversation→pool mapping, `/pool_name` switching via pipeline, workspace filtering |
| `test_pool_session_lifecycle.py` | Pool session lifecycle — creation, caching, invalidation |
| `test_pool_workspace_isolation.py` | Pool–workspace isolation tests |
| `test_pool_attach_persists.py` | Pool attribution persists across WS attach |
| `test_pool_routing_workspace_stack.py` | Pool routing across the workspace stack |
| `test_multi_channel_command_isolation.py` | Cross-channel command isolation — IM commands don't leak to WebUI and vice versa |

### Workspace isolation & switching

| File | Description |
|------|-------------|
| `test_session_index_pool_subdir.py` | Session index partitioned by pool subdir |
| `test_session_index_workspace_leak.py` | Session-index does not leak across workspaces |
| `test_sessions_ws_filter.py` | `GET /api/sessions` workspace filtering |
| `test_workspace_cd_register.py` | `/cd` registers workspace + session routing |
| `test_workspace_store_session_aware.py` | Workspace store session-awareness |
| `test_workspace_switch_integration.py` | Workspace switching end-to-end |
| `test_workspace_switch_no_effect.py` | No-effect workspace switch paths |
| `test_workspace_switching_disabled.py` | Behavior when `workspace.enabled=false` |
| `test_ws_isolation_regression.py` | WebSocket session isolation regression |
| `test_ws_partitioning_convergence.py` | Workspace partitioning convergence |
| `test_ws_send_message_workspace.py` | `send_message` scoped to the right workspace |
| `test_home_ws_repro.py` | Home-workspace repro case |
| `test_webui_service_workspace_wiring.py` | WebUI service workspace wiring |

### CLI / process

| File | Description |
|------|-------------|
| `test_cli.py` | CLI command parsing and execution |
| `test_process_stop.py` | Process stop/restart behavior via CLI |

## Frontend Tests

Frontend tests live alongside their source under `webui/src/`:

| File | Description |
|------|-------------|
| `webui/src/hooks/useWebUIStream.reducer.test.ts` | Pure reducer tests — session-scoped event filtering |
| `webui/src/hooks/useWebUIStream.attach-ws.test.ts` | WebSocket attach behavior |
| `webui/src/hooks/useWebUIStream.newconv.test.ts` | New-conversation lifecycle |
| `webui/src/lib/api.test.ts` | REST API client tests |
| `webui/src/lib/ws-client.reconnect.test.ts` | WebSocket reconnect/backoff tests |
| `webui/src/App.workspace.test.tsx` / `App.workspace.race.test.tsx` | Workspace switch + stale-response race tests |
| `webui/src/components/Sidebar.recent.test.ts` | Sidebar recent-workspace rendering |

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
- `test_server.py::test_ws_full_stream_isolation_across_conversations` exercises the real emitter -> adapter -> server -> WebSocket pipeline.
- `test_server.py::test_ws_turn_end_streaming_stop_is_isolated` verifies `turn_end` scoping.
- Use `_pipeline_fixture.py` for shared test infrastructure (stub registries, mock adapters, context).

<!-- MANUAL -->