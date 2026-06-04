# Overflow XML Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the plain-text overflow notice with a structured XML format containing the first chunk, metadata, and governance markers; raise the trigger threshold to 50,000 and chunk size to 10,000.

**Architecture:** Three-layer change — (1) constants + registry, (2) handler XML generation with CDATA, (3) `ToolResult.to_message()` detection. Storage format and cleanup remain untouched.

**Tech Stack:** Python 3.12, pytest, existing framework modules (`framework/tools/overflow/*`, `framework/interceptor/builtin/result_limit.py`, `framework/core/tool_manager.py`)

---

## File Map

| File | Responsibility | Action |
|------|---------------|--------|
| `framework/tools/overflow/local.py` | `LocalFileToolOverflowStore` — writes chunks to disk | Modify default `max_chunk_size`; add `max_chunk_size` to `OverflowRef` |
| `framework/tools/overflow/models.py` | `OverflowRef` dataclass | Add `max_chunk_size: int` field |
| `framework/tools/overflow/handler.py` | `ToolResultOverflowHandler` — returns notice to LLM | Replace plain-text notice with XML + CDATA; remove `_PREVIEW_CHARS` |
| `framework/interceptor/builtin/result_limit.py` | `ToolResultLimitInterceptor` — decides when to trigger | Change `_DEFAULT_MAX_CHARS` from 10,000 → 50,000 |
| `framework/tools/terminal/types.py` | `_TERMINAL_XML_TRUNCATABLE` registry | Add `"tool_result_overflow": ["chunk", "instruction"]` |
| `framework/core/tool_manager.py` | `ToolResult.to_message()` — LLM message conversion | Detect `<tool_result_overflow>` and inject `content_format`/`truncatable_paths` |
| `examples/bot_project/bot/service/core.py` | BotService runtime assembly | Update `_build_interceptor_chain()` defaults |
| `tests/unit/tools/overflow/test_handler.py` | Handler unit tests | Replace plain-text assertions with XML assertions |
| `tests/unit/interceptor/test_tool_result_limit_overflow.py` | Interceptor unit tests | Update mock return values and threshold |
| `tests/unit/memory/test_terminal_xml_truncation.py` | XML truncation detection tests | Add `tool_result_overflow` coverage |

---

## Task 1: Add `max_chunk_size` to `OverflowRef`

**Files:**
- Modify: `framework/tools/overflow/models.py`
- Modify: `framework/tools/overflow/local.py`
- Test: `tests/unit/tools/overflow/test_handler.py` (indirectly via handler tests)

- [ ] **Step 1: Add field to `OverflowRef`**

```python
# framework/tools/overflow/models.py
@dataclass(frozen=True)
class OverflowRef:
    dir_path: str
    chunk_count: int
    total_chars: int
    metadata_path: str
    max_chunk_size: int = 10_000  # NEW
```

- [ ] **Step 2: Populate field in `LocalFileToolOverflowStore.store()`**

```python
# framework/tools/overflow/local.py:111-116
return OverflowRef(
    dir_path=str(absolute_dir),
    chunk_count=total_chunks,
    total_chars=total_chars,
    metadata_path=str(meta_path.resolve()),
    max_chunk_size=self._max_chunk_size,  # NEW
)
```

- [ ] **Step 3: Run existing overflow tests to confirm no breakage**

Run: `pytest tests/unit/tools/overflow/ -v`
Expected: PASS (field addition is backward-compatible for tests that don't assert exact field count)

- [ ] **Step 4: Commit**

```bash
git add framework/tools/overflow/models.py framework/tools/overflow/local.py
git commit -m "feat: add max_chunk_size to OverflowRef"
```

---

## Task 2: Adjust Default Constants

**Files:**
- Modify: `framework/tools/overflow/local.py`
- Modify: `framework/interceptor/builtin/result_limit.py`

- [ ] **Step 1: Change default chunk size**

```python
# framework/tools/overflow/local.py:31
max_chunk_size: int = 10_000,  # was 9800
```

- [ ] **Step 2: Change default trigger threshold**

```python
# framework/interceptor/builtin/result_limit.py:21
_DEFAULT_MAX_CHARS = 50_000  # was 10000
```

- [ ] **Step 3: Run overflow tests**

Run: `pytest tests/unit/tools/overflow/ -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add framework/tools/overflow/local.py framework/interceptor/builtin/result_limit.py
git commit -m "feat: raise overflow threshold to 50k and chunk size to 10k"
```

---

## Task 3: Extend XML Truncation Registry

**Files:**
- Modify: `framework/tools/terminal/types.py`
- Modify: `tests/unit/memory/test_terminal_xml_truncation.py`

- [ ] **Step 1: Add `tool_result_overflow` to registry**

```python
# framework/tools/terminal/types.py:77-81
_TERMINAL_XML_TRUNCATABLE: dict[str, list[str]] = {
    "command_result": ["output"],
    "process_result": ["output"],
    "terminal_result": ["output", "cursor"],
    "tool_result_overflow": ["chunk", "instruction"],  # NEW
}
```

- [ ] **Step 2: Add test coverage**

```python
# tests/unit/memory/test_terminal_xml_truncation.py
# Add new test function (exact insertion point depends on existing test layout)

def test_get_truncatable_paths_detects_overflow_result() -> None:
    content = '<tool_result_overflow tool="read_file" total_chars="60000">...</tool_result_overflow>'
    paths = get_terminal_xml_truncatable_paths(content)
    assert paths == ["chunk", "instruction"]
```

- [ ] **Step 3: Run truncation tests**

Run: `pytest tests/unit/memory/test_terminal_xml_truncation.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add framework/tools/terminal/types.py tests/unit/memory/test_terminal_xml_truncation.py
git commit -m "feat: register tool_result_overflow XML truncatable paths"
```

---

## Task 4: Rewrite Overflow Handler to Return XML

**Files:**
- Modify: `framework/tools/overflow/handler.py`
- Test: `tests/unit/tools/overflow/test_handler.py`

### 4.1 Write the new handler code

- [ ] **Step 1: Replace entire `handler.py`**

```python
# framework/tools/overflow/handler.py
from __future__ import annotations

from framework.tools.overflow.cleaner import OverflowCleaner
from framework.tools.overflow.models import OverflowRef
from framework.tools.overflow.store import ToolOverflowStore


def _wrap_cdata(text: str) -> str:
    """Wrap text in CDATA, handling embedded ]]> sequences."""
    if "]]>" not in text:
        return f"<![CDATA[{text}]]>"
    escaped = text.replace("]]>", "]]]]><![CDATA[>")
    return f"<![CDATA[{escaped}]]>"


class ToolResultOverflowHandler:
    """Orchestrates overflow: store full content, return XML-wrapped first chunk.

    The returned message is a structured XML document containing chunk 1
    embedded in CDATA, plus metadata instructing the LLM how to read
    remaining chunks via the read tool. The XML is marked with
    skip_overflow="true" for human readability; the interceptor's skip
    logic relies on ToolResult.overflow_processed, not this attribute.
    """

    def __init__(
        self,
        store: ToolOverflowStore,
        cleaner: OverflowCleaner,
        max_chars: int = 10_000,
    ) -> None:
        self._store = store
        self._cleaner = cleaner
        self.max_chars = max_chars

    async def store_overflow(
        self,
        session_id: str,
        tool_call_id: str,
        tool_name: str,
        content: str,
    ) -> tuple[str, OverflowRef]:
        ref = await self._store.store(session_id, tool_call_id, tool_name, content)

        chunk1 = await self._store.read_chunk(session_id, tool_call_id, 1)
        if chunk1 is None:
            chunk1 = ""

        cdata = _wrap_cdata(chunk1)

        xml = (
            f'<tool_result_overflow tool="{tool_name}" '
            f'total_chars="{ref.total_chars}" '
            f'total_chunks="{ref.chunk_count}" '
            f'current_chunk="1" '
            f'max_chunk_size="{ref.max_chunk_size}" '
            f'skip_overflow="true">\n'
            f'  <storage dir="{ref.dir_path}" session="{session_id}" tool_call="{tool_call_id}" />\n'
            f'  <instruction>\n'
            f'    This result was too large and has been split into {ref.chunk_count} chunk(s) '
            f'of ~{ref.max_chunk_size} chars each. Use the read tool with '
            f'path="{ref.dir_path}/N.full.txt" to load any chunk. This message itself '
            f'is already processed — no further overflow handling is needed.\n'
            f'  </instruction>\n'
            f'  <chunk index="1">{cdata}</chunk>\n'
            f'</tool_result_overflow>'
        )
        return xml, ref

    def schedule_cleanup(self, session_id: str, kept_call_ids: set[str]) -> None:
        self._cleaner.schedule_cleanup(session_id, kept_call_ids)
```

### 4.2 Write failing tests first (TDD)

- [ ] **Step 2: Replace handler tests**

```python
# tests/unit/tools/overflow/test_handler.py
from __future__ import annotations

import pytest
from pathlib import Path

from framework.tools.overflow.cleaner import OverflowCleaner
from framework.tools.overflow.handler import ToolResultOverflowHandler
from framework.tools.overflow.local import LocalFileToolOverflowStore


@pytest.fixture
async def handler(tmp_path: Path) -> ToolResultOverflowHandler:
    store = LocalFileToolOverflowStore(workspace=tmp_path, max_chunk_size=50)
    await store.initialize()
    cleaner = OverflowCleaner(store)
    h = ToolResultOverflowHandler(store=store, cleaner=cleaner, max_chars=100)
    yield h
    await cleaner.stop()


class TestStoreOverflow:
    @pytest.mark.asyncio
    async def test_store_overflow_returns_xml(self, tmp_path: Path, handler: ToolResultOverflowHandler) -> None:
        content = "x" * 250
        xml, ref = await handler.store_overflow(
            session_id="sess_1",
            tool_call_id="call_1",
            tool_name="read_file",
            content=content,
        )

        assert xml.startswith('<tool_result_overflow')
        assert 'tool="read_file"' in xml
        assert 'total_chars="250"' in xml
        assert 'total_chunks="5"' in xml
        assert 'current_chunk="1"' in xml
        assert 'skip_overflow="true"' in xml
        assert '<storage dir=' in xml
        assert '<instruction>' in xml
        assert '<chunk index="1"><![CDATA[' in xml
        assert ref.total_chars == 250
        assert ref.chunk_count == 5

    @pytest.mark.asyncio
    async def test_store_overflow_short_content(self, tmp_path: Path, handler: ToolResultOverflowHandler) -> None:
        content = "short content"
        xml, ref = await handler.store_overflow(
            session_id="sess_1",
            tool_call_id="call_1",
            tool_name="read_file",
            content=content,
        )

        assert xml.startswith('<tool_result_overflow')
        assert ref.total_chars == 13
        assert ref.chunk_count == 1
        assert "short content" in xml

    @pytest.mark.asyncio
    async def test_store_overflow_escapes_cdata(self, tmp_path: Path, handler: ToolResultOverflowHandler) -> None:
        content = "hello ]]> world"
        xml, ref = await handler.store_overflow(
            session_id="sess_1",
            tool_call_id="call_2",
            tool_name="read_file",
            content=content,
        )

        assert "hello ]]]]><![CDATA[> world" in xml
```

- [ ] **Step 3: Run handler tests**

Run: `pytest tests/unit/tools/overflow/test_handler.py -v`
Expected: PASS (all 3 tests)

- [ ] **Step 4: Commit**

```bash
git add framework/tools/overflow/handler.py tests/unit/tools/overflow/test_handler.py
git commit -m "feat: overflow handler returns XML with CDATA-wrapped first chunk"
```

---

## Task 5: Wire XML Detection in `ToolResult.to_message()`

**Files:**
- Modify: `framework/core/tool_manager.py`
- Test: `tests/unit/memory/test_terminal_xml_truncation.py` (or new test file)

- [ ] **Step 1: Modify `to_message()`**

```python
# framework/core/tool_manager.py:258-283
# Existing code (preserve terminal tool detection):
try:
    from framework.tools.terminal.types import get_terminal_xml_truncatable_paths
except ImportError:
    return msg
paths = get_terminal_xml_truncatable_paths(content_str)
if paths is not None:
    msg["content_format"] = "xml"
    msg["truncatable_paths"] = paths
    return msg

# NEW: overflow XML detection (uses same registry function)
# Already covered by get_terminal_xml_truncatable_paths since we added
# "tool_result_overflow" to _TERMINAL_XML_TRUNCATABLE in Task 3.
# No additional code needed here — the registry addition is sufficient.
```

Wait — `get_terminal_xml_truncatable_paths` already checks for `<{root_tag}>` in content. Since we added `"tool_result_overflow"` to the registry, the existing call in `to_message()` will automatically detect it. **No change to `tool_manager.py` is needed.**

Correct approach: verify this works by writing a test.

- [ ] **Step 2: Write integration test for `to_message()`**

```python
# tests/unit/memory/test_terminal_xml_truncation.py (append)

def test_tool_result_to_message_detects_overflow_xml() -> None:
    from framework.core.tool_manager import ToolResult

    xml = (
        '<tool_result_overflow tool="read_file" total_chars="60000" '
        'total_chunks="6" current_chunk="1" max_chunk_size="10000" '
        'skip_overflow="true">\n'
        '  <chunk index="1"><![CDATA[chunk content]]></chunk>\n'
        '</tool_result_overflow>'
    )
    result = ToolResult(tool_name="read_file", result=xml, call_id="tc_1")
    msg = result.to_message()

    assert msg.get("content_format") == "xml"
    assert msg.get("truncatable_paths") == ["chunk", "instruction"]
```

- [ ] **Step 3: Run test**

Run: `pytest tests/unit/memory/test_terminal_xml_truncation.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add tests/unit/memory/test_terminal_xml_truncation.py
git commit -m "test: verify overflow XML is detected by to_message()"
```

---

## Task 6: Update Interceptor Tests

**Files:**
- Modify: `tests/unit/interceptor/test_tool_result_limit_overflow.py`

- [ ] **Step 1: Replace mock return values and assertions**

```python
# tests/unit/interceptor/test_tool_result_limit_overflow.py
# Replace class TestLongResultOverflows entirely

class TestLongResultOverflows:
    @pytest.mark.asyncio
    async def test_long_result_overflows(self) -> None:
        handler = AsyncMock(spec=ToolResultOverflowHandler)
        handler.max_chars = 50_000
        handler.store_overflow = AsyncMock(return_value=(
            '<tool_result_overflow tool="read_file" total_chars="100" '
            'total_chunks="2" current_chunk="1">...</tool_result_overflow>',
            MagicMock(),
        ))

        interceptor = ToolResultLimitInterceptor(overflow_handler=handler, max_chars=50)
        long_content = "a" * 100
        result = ToolResult(tool_name="read_file", result=long_content, call_id="tc_1")

        async def next_call() -> ToolResult:
            return result

        ctx = _make_ctx()
        call = _make_call()
        out = await interceptor.around_tool_call(ctx, call, next_call)

        assert out.overflow_processed is True
        assert out.result.startswith("<tool_result_overflow")
        handler.store_overflow.assert_awaited_once()
        handler.schedule_cleanup.assert_called_once()
```

The fallback truncation test (no handler) should remain unchanged — it still returns plain text when `overflow_handler=None`.

- [ ] **Step 2: Run interceptor tests**

Run: `pytest tests/unit/interceptor/test_tool_result_limit_overflow.py -v`
Expected: PASS (all 4 test classes)

- [ ] **Step 3: Commit**

```bash
git add tests/unit/interceptor/test_tool_result_limit_overflow.py
git commit -m "test: update interceptor tests for XML overflow format"
```

---

## Task 7: Update BotService Configuration

**Files:**
- Modify: `examples/bot_project/bot/service/core.py`

- [ ] **Step 1: Update `_build_interceptor_chain()`**

```python
# examples/bot_project/bot/service/core.py:818-831
overflow_dir = self._project_dir / "data"
max_chars = 50_000  # was 10000
overflow_store = LocalFileToolOverflowStore(
    workspace=overflow_dir,
    max_chunk_size=10_000,  # NEW — was defaulting to 9800
)
overflow_cleaner = OverflowCleaner(overflow_store)
overflow_handler = ToolResultOverflowHandler(
    store=overflow_store,
    cleaner=overflow_cleaner,
    max_chars=max_chars,
)
chain.add(
    ToolResultLimitInterceptor(
        overflow_handler=overflow_handler,
        max_chars=50_000,  # was 10000
    )
)
```

- [ ] **Step 2: Commit**

```bash
git add examples/bot_project/bot/service/core.py
git commit -m "feat(bot): raise overflow threshold to 50k and chunk size to 10k"
```

---

## Task 8: Full Regression Test

**Files:** All of the above

- [ ] **Step 1: Run all overflow-related tests**

```bash
pytest tests/unit/tools/overflow/ tests/unit/interceptor/test_tool_result_limit_overflow.py tests/unit/memory/test_terminal_xml_truncation.py -v
```

Expected: ALL PASS

- [ ] **Step 2: Run broader test suite**

```bash
pytest tests/unit/ -x --timeout=60
```

Expected: PASS (or fail only on pre-existing unrelated failures)

- [ ] **Step 3: Final commit**

```bash
# Only if there are uncommitted changes from test fixes
git add -A
git commit -m "test: align all overflow tests with XML redesign" || echo "nothing to commit"
```

---

## Self-Review Checklist

### 1. Spec coverage

| Spec requirement | Plan task |
|------------------|-----------|
| XML special identifier + truncatable paths | Task 3 (registry), Task 5 (to_message detection) |
| Anti-recursion (`overflow_processed` + XML hint) | Task 4 (handler XML with `skip_overflow`), Task 6 (interceptor skip test) |
| Threshold 50,000 / chunk 10,000 | Task 2 (constants), Task 7 (bot_project config) |
| First chunk embedded in XML | Task 4 (handler rewrite) |
| CDATA for raw content | Task 4 (`_wrap_cdata`) |
| Storage remains raw text | No change needed — `local.py` unchanged except default |
| Implementation converged | Only one XML path: handler generates, registry detects |
| examples/bot_project no-op | Task 7 (two line changes in config only) |
| Preserve cleanup mechanism | No changes to `cleaner.py` |
| No backward compatibility | Old plain-text format fully removed |

**Gap found:** None.

### 2. Placeholder scan

- No "TBD", "TODO", "implement later", "fill in details" found.
- All code blocks contain complete, runnable code.
- All test commands have expected outputs.

### 3. Type consistency

- `OverflowRef.max_chunk_size: int` — added in Task 1, consumed in Task 4 via `ref.max_chunk_size`.
- `ToolResultOverflowHandler.max_chars` — still present (unused but no removal planned).
- `get_terminal_xml_truncatable_paths` — registry addition in Task 3, consumed automatically by `to_message()` in Task 5.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-04-overflow-xml-redesign.md`.**

Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using `executing-plans`, batch execution with checkpoints.

Which approach?
