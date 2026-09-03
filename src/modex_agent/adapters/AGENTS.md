<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-09-01 | B4 Output/Emitter move -->

# adapters

Platform I/O contracts and the emitter bridge — decouple platform I/O from agent logic (B4: Output/Emitter/filter moved here from `pipeline/` + `core/emitter.py`).

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Facade (ADR-0005) — re-exports `StreamingMode`/`PlatformAdapter`/`AdapterRegistry`, the `OutputAdapter` family, `StreamingAwareEmitter`, and the `ContentFilter` family |
| `platform.py` | `PlatformAdapter` ABC, `AdapterRegistry`, `StreamingMode` enum (NATIVE/PSEUDO/NONE) |
| `output.py` | `OutputAdapter` ABC (`send`/`send_delta`/`flush_deltas`, `streaming_mode` default PSEUDO, optional `content_filter`) + `NullOutputAdapter` (NONE, drops all output), `CLIOutputAdapter` (true streaming to terminal), `HTTPOutputAdapter` (SSE) |
| `emitter.py` | `StreamingAwareEmitter[E]` — bridges `ContentEmitter` (core contract) to `OutputAdapter`: NATIVE forwards deltas immediately, PSEUDO/NONE buffer and flush at stream end/complete; `_safe_adapter_send` timeout guard; attachment projection in `emit_complete` |
| `filters.py` | `ContentFilter` ABC + `ChainedContentFilter`, `ReasoningContentFilter` (strip/keep), `WhitespaceFilter` (collapse/strip). Applied by `OutputAdapter._apply_filter()` |

## Dependencies
- `modex_agent.core.emitter` — `ContentEmitter`, `AgentResult` (emitter contract stays in core)
- `modex_agent.core.events` — `EmitterConfig`
- `modex_agent.core.types` — `OutputMessage`
- No pipeline imports (B4 invariant: adapters never import pipeline; `InputAdapter` stays in `pipeline/adapters.py`)

## Notes
- Concrete adapter implementations live in example projects (e.g., `examples/bot_project/bot/adapters/`).
- `InputAdapter` stays in `pipeline/adapters.py` (it carries input-pipeline/control-channel coupling).

<!-- MANUAL: -->
