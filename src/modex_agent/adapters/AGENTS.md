<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 -->

# adapters

I/O adapter base classes — decouple platform I/O from agent logic.

## Key Files

| File | Description |
|------|-------------|
| `platform.py` | `PlatformAdapter` ABC, `AdapterRegistry`, `StreamingMode` enum |

## Dependencies
- `modex_agent.core.types` — `AgentEvent`, `ChatMessage` types used in adapter interfaces
- `abc`, `enum` — standard library

## Notes
- Concrete adapter implementations live in example projects (e.g., `examples/bot_project/bot/adapters/`).
- `InputAdapter` / `OutputAdapter` are referenced here and consumed by `AgentPipeline`.

<!-- MANUAL: -->
