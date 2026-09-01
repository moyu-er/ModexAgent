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

- Overflow notice is plain text rendered by `render_overflow_text`: head (10% of `max_chars`) + blank line + explicit elision marker (states the omitted char/line count, unmissable as a marker rather than content; blank lines separate it from head/tail, which cut at arbitrary character positions) + blank line + tail (15% of `max_chars` — errors and exit codes cluster at the end; at the default 50K threshold the model sees 5K + 7.5K ≈ 25%, the elided 75%+ persists to disk) + blank line + `[Full output (N chars total) saved to: {path}/full.txt]` notice. Content at or under the threshold passes through unchanged. `full_output_path=None` renders the "NOT saved" variant for fallback paths.
- Full content is persisted on disk as a single `full.txt` file, not split into pieces. `.meta.json` is written LAST — it is the entry's commit marker (`list_tool_call_ids` counts only directories that carry it), so a crash mid-write never leaves a listed entry whose full.txt is missing.
- Cleanup ordering (interceptor): the kept-set is scheduled BEFORE the entry is stored. A clean pass firing between store and schedule would see the on-disk entry absent from every kept-set and delete it as stale.
- `OverflowRef` carries `dir_path`, `total_chars`, and `metadata_path`.
- `OverflowMetadata` carries `tool_name`, `tool_call_id`, `session_id`, `created_at`, and `total_chars`. All three models are frozen Pydantic `BaseModel` (`.meta.json` round-trips via `model_dump_json`/`model_validate_json`).
- Session-scoped pruning via `clean()` when a session ends, driven by `OverflowCleaner`. Session deletion also removes the session's whole overflow directory (`core/cleanup.py` artifact unit 11).

## Directory Layout

`LocalFileToolOverflowStore` writes one entry per tool call:

```
{workspace}/tool_overflow/{session_id}/{tool_call_id}/
├── .meta.json
└── full.txt       ← raw content, no header
```

`.meta.json` holds the `OverflowMetadata` fields. `full.txt` holds the raw tool output with no header or wrapper.
