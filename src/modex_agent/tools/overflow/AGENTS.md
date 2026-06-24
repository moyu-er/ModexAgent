<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-10 -->

# overflow

Tool result overflow management — persists oversized tool outputs outside the LLM context, allowing agents to reference them on-demand.

## Key Files

| File | Description |
|------|-------------|
| `store.py` | `ToolOverflowStore` ABC — `initialize()`, `store()`, `retrieve()`, `delete()`, `cleanup()` |
| `handler.py` | `ToolResultOverflowHandler` — orchestrates store + cleaner; returns XML overflow reference with CDATA-wrapped preview chunk |
| `cleaner.py` | `OverflowCleaner` — manages overflow lifecycle, session cleanup |
| `local.py` | `LocalFileOverflowStore` — filesystem-backed implementation |
| `models.py` | `OverflowRef`, `OverflowMetadata`, `CleanRequest` — data models |

## Design Rules

- Overflow notice is XML format with CDATA-wrapped first chunk — agent reads full content via reference ID.
- Overflow notice is deliberately brief to prevent recursive overflow.
- Full content persisted on disk; agent retrieves via reference ID.
- Session-scoped cleanup via `cleanup()` on session end.
- XML output is detected by `ToolResult.to_message()` for truncatable path registration.
