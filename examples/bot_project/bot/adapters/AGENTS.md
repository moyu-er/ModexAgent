<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-13 -->

# adapters

Input/output adapters that bridge external platforms (QQ, WebUI) to the agent pipeline.

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Package marker, exports key adapter classes |
| `qq.py` | QQ platform adapters — `QQInputAdapter` (C2C + group), `QQOutputAdapter` (message sending, file upload) |
| `web_socket.py` | `WebSocketInputAdapter` — manages WebSocket connections for WebUI chat |
| `register_websocket.py` | WebSocket adapter registration — wires WS input to pool router |
| `register_qq.py` | QQ adapter registration — wires QQ to the bot service |
| `fan_in.py` | `FanInOutputAdapter` — merges output from multiple agents into a single WebSocket stream per conversation |
| `channels.py` | `set_conv_channel()` / `get_conv_channel()` — tracks which platform (websocket/qq) owns each conversation |

## For AI Agents

### Working In This Directory
- All adapters implement `InputAdapter` or `OutputAdapter` from `framework/pipeline/adapters.py`.
- `fan_in.py` is critical for WebUI — it routes agent output to the correct WebSocket connection by conversation_id.
- `channels.py` enables platform-aware behavior (e.g., WebUI conversations skip certain IM-only commands).

### Common Patterns
- Adapters are created in `core.py` or `*_service.py` and passed to `PoolRouter` or `Pipeline`.
- The fan-in pattern: multiple pipelines share one `FanInOutputAdapter` that multiplexes to WebSocket clients.

## Dependencies

### Internal
- `framework/pipeline/adapters.py` — adapter ABCs
- `framework/core/types.py` — `InputMessage`, `OutputMessage`
- `bot/webui/events.py` — WebUI event types
