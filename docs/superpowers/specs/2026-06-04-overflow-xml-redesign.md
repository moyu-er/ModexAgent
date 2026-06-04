# Overflow XML Redesign

## Problem Statement

The current tool-result overflow mechanism has three critical flaws:

1. **Infinite recursion risk**: The trigger threshold (`max_chars=10000`) and the chunk size (`max_chunk_size=9800`) are too close. Any wrapper text added by the overflow handler itself can push the returned notice back over the threshold, causing the interceptor to process it again on the next turn.

2. **Plain-text return format**: The overflow notice is returned as unstructured plain text (`[TOOL_RESULT_TRUNCATED]...`). This is inconsistent with the framework's existing XML special formats (e.g., `command_result`, `terminal_result`) and misses governance-level metadata (`content_format`, `truncatable_paths`).

3. **No self-identification for skip logic**: There is no explicit XML-level marker that tells the interceptor "this result has already been overflow-processed, skip it." The only protection is an internal boolean flag (`overflow_processed`) on `ToolResult`, which is invisible to the LLM and not robust against edge cases.

## Goals

1. Return overflow results as structured XML with governance metadata.
2. Ensure overflow-processed content is never re-processed by the interceptor.
3. Eliminate the threshold/chunk-size proximity that causes recursion.
4. Give the LLM the first chunk immediately, plus metadata to retrieve remaining chunks via the `read` tool.
5. Keep the implementation converged — no parallel XML conversion pipelines.
6. `examples/bot_project/` uses the new behavior with zero code changes.
7. Preserve the existing cleanup mechanism (`OverflowCleaner`).

## Non-Goals

- Backward compatibility with the old plain-text notice format.
- Changing the on-disk storage layout (chunk files remain raw text).
- Adding a new dedicated "read_overflow_chunk" tool.

## Design

### 1. New Default Constants

| Constant | Old Value | New Value | Rationale |
|----------|-----------|-----------|-----------|
| `ToolResultLimitInterceptor._DEFAULT_MAX_CHARS` | 10,000 | **50,000** | Large enough that typical tool results pass through; only truly oversized results trigger overflow. |
| `LocalFileToolOverflowStore._DEFAULT_MAX_CHUNK_SIZE` | 9,800 | **10,000** | Fixed chunk size for storage. The gap to the trigger threshold (50,000) leaves ~40,000 chars of headroom for XML wrapper overhead. |
| `ToolResultOverflowHandler._PREVIEW_CHARS` | 500 | **removed** | No longer needed; the first full chunk (up to 10,000 chars) is returned instead of a preview. |

The `examples/bot_project/bot/service/core.py` configuration is updated to match:

```python
overflow_store = LocalFileToolOverflowStore(
    workspace=overflow_dir,
    max_chunk_size=10_000,
)
chain.add(
    ToolResultLimitInterceptor(
        overflow_handler=overflow_handler,
        max_chars=50_000,
    )
)
```

### 2. XML Return Format

When overflow is triggered, the interceptor returns a `ToolResult` whose `result` is a single XML document:

```xml
<tool_result_overflow
    tool="read_file"
    total_chars="60000"
    total_chunks="6"
    current_chunk="1"
    max_chunk_size="10000"
    skip_overflow="true">
  <storage
      dir="/abs/path/to/workspace/tool_overflow/session_id/tool_call_id/"
      session="session_id"
      tool_call="tool_call_id" />
  <instruction>
    This result was too large and has been split into 6 chunks of ~10000
    chars each. Use the read tool with path="{dir}/N.full.txt" to load
    any chunk. This message itself is already processed — no further
    overflow handling is needed.
  </instruction>
  <chunk index="1"><![CDATA[
{raw content of chunk 1, up to 10000 chars, no escaping needed}
]]></chunk>
</tool_result_overflow>
```

#### Attribute semantics

| Attribute | Meaning |
|-----------|---------|
| `tool` | Name of the tool that produced the original oversized result. |
| `total_chars` | Total length of the original result. |
| `total_chunks` | Number of chunks written to disk. |
| `current_chunk` | Which chunk is embedded in this XML (always `1` on first return). |
| `max_chunk_size` | Configured chunk size (for LLM reference). |
| `skip_overflow` | **Human-readable hint** that this message is already processed. The interceptor does **not** parse this attribute; it relies on the `overflow_processed` flag. |

#### Governance metadata

`ToolResult.to_message()` detects the `<tool_result_overflow>` root tag and injects:

```python
msg["content_format"] = "xml"
msg["truncatable_paths"] = ["chunk", "instruction"]
```

This allows the governance layer to truncate the embedded chunk or instruction if the overall message grows too large, without breaking XML structure.

### 3. CDATA for Raw Content

Chunk content is embedded inside `<![CDATA[ ... ]]>` to avoid XML-escaping overhead and to prevent the chunk's own XML/text from interfering with the wrapper document.

Edge case: if the raw content contains the literal sequence `]]>`, it is split into two adjacent CDATA sections:

```xml
<chunk index="1"><![CDATA[content before ]]]]><![CDATA[>content after]]></chunk>
```

### 4. Storage Remains Raw Text

On-disk chunk files (`1.full.txt`, `2.full.txt`, ...) continue to store **raw text only** — no XML wrapper. This keeps the `read` tool simple: calling `read(path=".../2.full.txt")` returns exactly the second chunk's content without any parsing.

### 5. Interceptor Skip Logic

The interceptor already checks `result.overflow_processed` before processing:

```python
if result.error or result.result is None or result.overflow_processed:
    return result
```

This behavior is **retained unchanged**. The XML attribute `skip_overflow="true"` is purely for LLM readability, not for code-level routing.

### 6. Implementation Scope

#### Files to modify

| File | Change |
|------|--------|
| `framework/tools/overflow/handler.py` | Rewrite `store_overflow()` to return XML with CDATA instead of plain-text notice. Remove `_PREVIEW_CHARS`. |
| `framework/tools/overflow/local.py` | Change default `max_chunk_size` from 9,800 to 10,000. |
| `framework/interceptor/builtin/result_limit.py` | Change default `max_chars` from 10,000 to 50,000. |
| `framework/tools/terminal/types.py` | Add `"tool_result_overflow": ["chunk", "instruction"]` to `_TERMINAL_XML_TRUNCATABLE` (or a new overflow-specific registry). |
| `framework/core/tool_manager.py` | Update `ToolResult.to_message()` to detect `<tool_result_overflow>` and set `content_format` + `truncatable_paths`. |
| `examples/bot_project/bot/service/core.py` | Update `_build_interceptor_chain()` to pass `max_chars=50_000` and `max_chunk_size=10_000`. |
| `tests/unit/tools/overflow/test_handler.py` | Update assertions to check for XML structure instead of `[TOOL_RESULT_TRUNCATED]`. |
| `tests/unit/interceptor/test_tool_result_limit_overflow.py` | Update expected results and thresholds. |

#### Files NOT to modify

| File | Reason |
|------|--------|
| `framework/tools/overflow/store.py` (ABC) | Interface unchanged. |
| `framework/tools/overflow/models.py` | `OverflowRef`, `OverflowMetadata` unchanged. |
| `framework/tools/overflow/cleaner.py` | Cleanup logic unchanged. |
| `framework/tools/standard/file_tool.py` | `read` tool behavior unchanged. |

### 7. Examples/Bot_Project Impact

`examples/bot_project/bot/service/core.py` needs only two line changes in `_build_interceptor_chain()`:

1. Pass `max_chunk_size=10_000` to `LocalFileToolOverflowStore(...)`.
2. Pass `max_chars=50_000` to `ToolResultLimitInterceptor(...)`.

No other code in `examples/bot_project/` is affected.

## Testing Strategy

1. **Unit — handler**: Verify `store_overflow()` returns valid XML with correct attributes, CDATA wrapping, and `]]>` splitting.
2. **Unit — interceptor**: Verify threshold 50,000 triggers overflow; results with `overflow_processed=True` are skipped.
3. **Unit — `to_message()`**: Verify `content_format="xml"` and `truncatable_paths` are set for overflow XML.
4. **Integration — bot_project**: Run the bot service, trigger a tool result > 50,000 chars, confirm the LLM receives XML with chunk 1 embedded and can `read` subsequent chunks.

## Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| CDATA `]]>` edge case | Split into two CDATA sections (standard XML technique). |
| LLM ignores `instruction` and tries to re-read chunk 1 via `read` | No harm; chunk 1 is on disk as raw text, identical to embedded content. |
| Old tests assert on `[TOOL_RESULT_TRUNCATED]` | All related tests are updated as part of this change; no backward compatibility required. |
