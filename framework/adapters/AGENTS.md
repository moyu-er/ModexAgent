<!-- Parent: ../AGENTS.md -->

# adapters

I/O adapter base classes — decouple platform I/O from agent logic.

## Key Files

| File | Description |
|------|-------------|
| `platform.py` | `PlatformAdapter` ABC, `AdapterRegistry`, `StreamingMode` enum |

## Notes
- Concrete adapter implementations live in example projects (e.g., `examples/bot_project/bot/adapters/`).
- `InputAdapter` / `OutputAdapter` are referenced here and consumed by `AgentPipeline`.
