<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-09-02 | E1 transport ownership -->

# adapters

Platform I/O contracts and the delivery-policy sink — decouple platform I/O from agent logic (B4: Output/filter moved here from `pipeline/`; the buffering sink face lives here on the core `TurnEventSink` seam).

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Facade (ADR-0005) — re-exports `StreamingMode`/`PlatformAdapter`/`AdapterRegistry`, the `OutputAdapter` family, `BufferingSink`/`DeliveryPolicy`, and the `ContentFilter` family |
| `platform.py` | `PlatformAdapter` ABC, `AdapterRegistry`, `StreamingMode` enum (NATIVE/PSEUDO/NONE) |
| `output.py` | `OutputAdapter` ABC (`send`/`send_delta`/`flush_deltas`, `streaming_mode` default PSEUDO, optional `content_filter`) + `NullOutputAdapter` (NONE, drops all output), `CLIOutputAdapter` (true streaming to terminal), `HTTPOutputAdapter` (SSE) |
| `emitter.py` | `BufferingSink` — a `TurnEventSink` bridging the core `TurnEvent` stream onto `OutputAdapter` with a `DeliveryPolicy` derived from `StreamingMode` (NATIVE→STREAMING forwards every text delta immediately; PSEUDO→SEGMENT buffers and flushes on `iteration_finished`; NONE→TURN buffers and flushes on `turn_finished`; attachments ride `turn_finished`); `_safe_adapter_send` timeout guard; `turn_errored`/terminal-error message rendering |
| `filters.py` | `ContentFilter` ABC + `ChainedContentFilter`, `ReasoningContentFilter` (strip/keep), `WhitespaceFilter` (collapse/strip). Applied by `OutputAdapter._apply_filter()` |

## Dependencies
- `modex_agent.core.emitter` — `TurnEventSink`, `KindGate` (the sink contract stays in core)
- `modex_agent.core.turn_events` — the `TurnEvent` union
- `modex_agent.messaging` — `OutputMessage` transport model
- No pipeline imports (B4 invariant: adapters never import pipeline; `InputAdapter` stays in `pipeline/adapters.py`)

## Notes
- Concrete adapter implementations live in example projects (e.g., `examples/bot_project/bot/adapters/`).
- `InputAdapter` stays in `pipeline/adapters.py` (it carries input-pipeline/control-channel coupling).
- Input/output message DTOs and approval input transport live in `modex_agent.messaging`, not in this adapter package.

<!-- MANUAL: -->
