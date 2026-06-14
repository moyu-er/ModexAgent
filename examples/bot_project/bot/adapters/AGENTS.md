<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-13 -->

# adapters

Input/output adapters that bridge external platforms (QQ, WebUI) to the agent pipeline.

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Package marker, exports key adapter classes |
| `qq.py` | QQ platform adapters — `QQInputAdapter` (C2C + group), `QQOutputAdapter` (message sending, file upload). Uses ABC `configure_input_pipeline` default (stores pipeline/ctx/output). |
| `web_socket.py` | `WebSocketInputAdapter` — manages WebSocket connections for WebUI chat. Has no-op `configure_input_pipeline` override (pipeline is held by `WebUIServer`). |
| `register_websocket.py` | WebSocket adapter registration — wires WS input to pool router |
| `register_qq.py` | QQ adapter registration — wires QQ to the bot service |
| `fan_in.py` | `FanInOutputAdapter` — merges output from multiple agents into a single WebSocket stream per conversation |
| `channels.py` | `set_conv_channel()` / `get_conv_channel()` — tracks which platform (websocket/qq) owns each conversation |

## For AI Agents

### Working In This Directory
- All adapters implement `InputAdapter` or `OutputAdapter` from `framework/pipeline/adapters.py`.
- `InputAdapter.configure_input_pipeline()` is a typed ABC method (stores `_input_pipeline`, `_input_ctx`, `_output_adapter`). Override with no-op when the pipeline is held externally (see `web_socket.py`).
- `fan_in.py` is critical for WebUI — it routes agent output to the correct WebSocket connection by conversation_id.
- `channels.py` enables platform-aware behavior (e.g., WebUI conversations skip IM-only pipeline stages like S2/S3).

### Common Patterns
- Adapters are created in `core.py` or `*_service.py` and passed to `PoolRouter` or `Pipeline`.
- The fan-in pattern: multiple pipelines share one `FanInOutputAdapter` that multiplexes to WebSocket clients.

## Dependencies

### Internal
- `framework/pipeline/adapters.py` — adapter ABCs
- `framework/core/types.py` — `InputMessage`, `OutputMessage`
- `bot/webui/events.py` — WebUI event types
