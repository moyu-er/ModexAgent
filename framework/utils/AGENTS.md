<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-22 -->

# utils

General-purpose utilities — no domain-specific logic. Stateless helpers free of framework dependencies.

## Key Files

| File | Description |
|------|-------------|
| `context_builder.py` | Context assembly from multiple sources |
| `deduplicator.py` | Message deduplication logic |
| `sanitizer.py` | Content sanitization and cleaning |
| `helpers.py` | General-purpose helper functions |
| `file_io.py` | Encoding-resilient JSON/JSONL readers (`read_json_robust`, `read_jsonl_robust`) |
| `timezone.py` | User timezone from `TIMEZONE` env (cached, cross-platform) |
| `xml.py` | Unified XML escaping utilities |
| `media_utils.py` | `MediaProcessor` — image/audio/video attachments, multi-modal content |
| `message_builder.py` | Message construction helpers |
| `think_tag.py` | Think-tag extraction (streaming + non-streaming) |

## Notes
- Keep utilities stateless and runtime-agnostic unless explicitly part of hook/interceptor/control integration.
- `MediaProcessor` builds OpenAI-compatible multi-modal content blocks.
