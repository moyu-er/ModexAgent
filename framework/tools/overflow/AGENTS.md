<!-- Parent: ../AGENTS.md -->

# overflow

Tool result overflow management — persists oversized tool outputs outside the LLM context, allowing agents to reference them on-demand.

## Key Files

| File | Description |
|------|-------------|
| `store.py` | `ToolOverflowStore` ABC — `initialize()`, `store()`, `retrieve()`, `delete()`, `cleanup()` |
| `handler.py` | `ToolResultOverflowHandler` — orchestrates store + cleaner; returns brief truncation notice + preview |
| `cleaner.py` | `OverflowCleaner` — manages overflow lifecycle, session cleanup |
| `local.py` | `LocalFileOverflowStore` — filesystem-backed implementation |
| `models.py` | `OverflowRef`, `OverflowMetadata`, `CleanRequest` — data models |

## Design Rules

- Overflow notice is deliberately short to prevent recursive overflow.
- Full content persisted on disk; agent retrieves via reference ID.
- Session-scoped cleanup via `cleanup()` on session end.
