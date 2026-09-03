<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-07-08 -->

# adapters

Multi-channel input/output adapters that bridge external platforms (QQ, Telegram, WebUI) to the agent pipeline. The set is **open and plugin-style**: each platform is a `register_<name>.py` module that self-registers via the `@register` decorator, and `WebUIService` auto-discovers every `register_*.py` at startup — adding a new IM requires no change to `WebUIService` or any service code.

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Package marker, exports key adapter classes |
| `channels.py` | The multi-channel spine — `ADAPTERS` registry, `@register` decorator, `AdapterBuildContext`, `set_conv_channel`/`get_conv_channel`, and `ChannelRouterOutputAdapter`. `WebUIService` imports every `register_*.py` to fire the decorators, then iterates `ADAPTERS` to build enabled adapters |
| `qq/` | QQ platform adapters — `QQInputAdapter` (C2C + group), `QQOutputAdapter` (message sending, file upload). Split into `__init__.py` (re-exports), `_ws_state.py`, `input.py`, `output.py`, `emitter.py`. Uses ABC `configure_input_pipeline` default (stores pipeline/ctx/output) |
| `telegram.py` | Telegram adapters — `TelegramInputAdapter` (long-polling inbound via injected PTB hooks), `TelegramOutputAdapter` (HTML render + 4096-char chunking). PTB-free and unit-testable in isolation; real polling wired by `register_telegram.py` |
| `web_socket.py` | `WebSocketInputAdapter` — manages WebSocket connections for WebUI chat. No-op `configure_input_pipeline` override (pipeline is held by `WebUIServer`) |
| `register_qq.py` | QQ adapter registration — `@register("qq")`; wires QQ to the bot service. Returns `None` when disabled/unconfigured |
| `register_telegram.py` | Telegram adapter registration — `@register("telegram")`; builds a `python-telegram-bot` (PTB) `Application`, wires inbound `MessageHandler`, injects PTB start/stop hooks, returns a channel-filtered emitter factory. `None` when disabled/no token |
| `register_websocket.py` | WebSocket adapter registration — wires WS input to pool router |
| `fan_in.py` | `FanInOutputAdapter` — merges output from multiple agents into a single WebSocket stream per conversation |

## Channel Routing Model

`channels.py` is the single source of truth for multi-channel behavior:

- **`@register(name, enabled=...)`** — decorator that appends an `AdapterSpec` (name + build factory) to `ADAPTERS`. Each `register_*.py` module declares one adapter.
- **`AdapterBuildContext`** — passed to each factory: `config_dir`, `project_dir`, `raw_config` (for IM credentials), `transcript_store`.
- **`set_conv_channel(conv_id, channel)` / `get_conv_channel(conv_id)`** — records which channel originated each conversation (defaults to `"websocket"`). Per-channel emitters read this to avoid cross-talk.
- **`ChannelRouterOutputAdapter`** — multiplexes outbound output to the per-channel adapter that owns the conversation. Transient notices (`message_type=notice`) are fanned out to the originating channel **and** the WebUI (universal observer), so an IM-originated notice is visible in both IM and the browser — unless the turn already originated on WebUI (no duplicate bubble), or no websocket adapter is registered (pure-IM deploy).
- **WebUI as universal observer** — the websocket emitter records ALL conversations to the transcript store, so the frontend can view history from any channel.

## For AI Agents

### Working In This Directory
- All adapters implement `InputAdapter` (`modex_agent/pipeline/adapters.py`) or `OutputAdapter` (`modex_agent/adapters/output.py`).
- `InputAdapter.configure_input_pipeline()` is a typed ABC method (stores `_input_pipeline`, `_input_ctx`, `_output_adapter`). Override with no-op when the pipeline is held externally (see `web_socket.py`).
- **Adding a new IM** (Discord, Feishu, DingTalk, …): create `<platform>.py` (Input/Output adapters) + `register_<platform>.py` decorated with `@register`, read credentials from `ctx.raw_config`, return `None` when unconfigured. Restart — `WebUIService` auto-discovers it. No other code changes.
- Telegram keeps PTB isolated inside `register_telegram.py`; the adapter itself (`telegram.py`) is PTB-free via `set_lifecycle_hooks` so it stays unit-testable.
- `fan_in.py` is critical for WebUI — it routes agent output to the correct WebSocket connection by conversation_id.

### Common Patterns
- Adapters are created by their `register_*.py` factory (driven by `WebUIService` iterating `ADAPTERS`) and passed to `PoolRouter` or `Pipeline`.
- **Channel-filtered emitter**: each IM register returns an emitter factory whose emitter silently drops output for conversations not originated on its channel (e.g. `_ChannelFilteredTelegramEmitter`) — no cross-talk.
- The fan-in pattern: multiple pipelines share one `FanInOutputAdapter` that multiplexes to WebSocket clients.

## Dependencies

### Internal
- `modex_agent/pipeline/adapters.py` — `InputAdapter` ABC
- `modex_agent/adapters/output.py` — `OutputAdapter` ABC + bundled implementations
- `modex_agent/adapters/emitter.py` — `StreamingAwareEmitter`
- `modex_agent/adapters/filters.py` — content filters
- `modex_agent/core/types.py` — `InputMessage`, `OutputMessage`
- `modex_agent/adapters/platform.py` — `StreamingMode`
- `bot/webui/events.py` — WebUI event types

### External
- `qq-botpy` — QQ Bot SDK (framework `gateway` extra)
- `python-telegram-bot` (PTB) — Telegram Bot API (bot_project dependency)

<!-- MANUAL -->
