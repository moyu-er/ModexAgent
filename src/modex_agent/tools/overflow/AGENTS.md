<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-08-13 -->

# overflow

Tool result overflow management. Persists oversized tool outputs outside the LLM context so agents can reference the full text on disk instead of carrying it through every turn.

## Key Files

| File | Description |
|------|-------------|
| `store.py` | `ToolOverflowStore` ABC with `initialize()`, `store()`, `read_metadata()`, `delete()`, `list_tool_call_ids()`, `close()`, `clean()` |
| `handler.py` | `ToolResultOverflowHandler`: orchestrates store + cleaner, returns truncated text with a path notification |
| `truncate.py` | `render_overflow_text` + `split_head_tail`: single source of truth for model-facing truncation text (head + elision marker + tail + full-output notice) |
| `cleaner.py` | `OverflowCleaner`: manages overflow lifecycle, prunes stale entries per session |
| `local.py` | `LocalFileToolOverflowStore`: filesystem-backed implementation, single `full.txt` file |
| `models.py` | `OverflowRef`, `OverflowMetadata`, `CleanRequest` data models |

## Design Rules

- Overflow notice is plain text rendered by `render_overflow_text`: head (10% of `max_chars`) + explicit elision marker (states the omitted char/line count, unmissable as a marker rather than content) + tail (15% of `max_chars` — errors and exit codes cluster at the end; at the default 50K threshold the model sees 5K + 7.5K ≈ 25%, the elided 75%+ persists to disk) + `[Full output (N chars total) saved to: {path}/full.txt]` notice. Content at or under the threshold passes through unchanged. `full_output_path=None` renders the "NOT saved" variant for fallback paths.
- Full content is persisted on disk as a single `full.txt` file, not split into pieces.
- `OverflowRef` carries `dir_path`, `total_chars`, and `metadata_path`.
- `OverflowMetadata` carries `tool_name`, `tool_call_id`, `session_id`, `created_at`, and `total_chars`.
- Session-scoped pruning via `clean()` when a session ends, driven by `OverflowCleaner`.

## Directory Layout

`LocalFileToolOverflowStore` writes one entry per tool call:

```
{workspace}/tool_overflow/{session_id}/{tool_call_id}/
├── .meta.json
└── full.txt       ← raw content, no header
```

`.meta.json` holds the `OverflowMetadata` fields. `full.txt` holds the raw tool output with no header or wrapper.
