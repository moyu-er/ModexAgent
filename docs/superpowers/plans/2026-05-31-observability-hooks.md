# Observability Hooks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enhance RunLoggingHook and ProgressReportHook for full observability, add TraceFileWriter, integrate into bot_project.

**Architecture:** Enhance two existing framework hooks (RunLoggingHook adds agent_name/iteration/two-line format; ProgressReportHook adds full-content events). Add TraceFileWriter as a ControlEventBus subscriber. Wire everything in bot_project's core.py.

**Tech Stack:** Python 3.12+, asyncio, logging, dataclasses, pytest

**Spec:** `docs/superpowers/specs/2026-05-31-observability-hooks-design.md`

---

## File Map

| Action | Path | Responsibility |
|---|---|---|
| Modify | `framework/hook/builtin/logging.py` | Enhanced two-line log format with agent_name, iteration |
| Modify | `framework/hook/builtin/progress_report.py` | Full-content events with agent_name, iteration, max_iterations |
| Create | `framework/hook/builtin/trace_writer.py` | ControlEventBus subscriber → JSON-lines file with rotation |
| Modify | `framework/hook/builtin/__init__.py` | Export TraceFileWriter |
| Modify | `examples/bot_project/bot/service/core.py` | Wire CallbackControlEventBus + ProgressReportHook + TraceFileWriter |
| Modify | `tests/unit/test_hooks.py` | Update RunLoggingHook tests for new format, add ProgressReportHook tests |

---

### Task 1: Enhance RunLoggingHook

**Files:**
- Modify: `framework/hook/builtin/logging.py`
- Modify: `tests/unit/test_hooks.py`

The hook currently logs on a single line with `session_id` only. We add `agent_name` and `iteration`, switch to two-line format, and skip toolCall arguments in `after_llm_response`.

- [ ] **Step 1: Add helper methods to RunLoggingHook**

Add `_get_agent_name` and `_get_iteration` static methods at the end of the class (before the existing `_format_*` methods):

```python
@staticmethod
def _get_agent_name(ctx: AgentContext[Any]) -> str:
    if ctx.session_meta is not None:
        return ctx.session_meta.agent_name
    if ctx.identity is not None:
        return ctx.identity.agent_id
    return "<unknown>"

@staticmethod
def _get_iteration(ctx: AgentContext[Any]) -> int:
    state = ctx.runtime.state if ctx.runtime else None
    return getattr(state, "iteration", 0)
```

- [ ] **Step 2: Rewrite `after_llm_response` to two-line format**

Replace the existing `after_llm_response` method body:

```python
async def after_llm_response(self, ctx: AgentContext[Any], response: LLMResponse) -> None:
    tool_names = [call.tool_name for call in response.tool_calls]
    agent = self._get_agent_name(ctx)
    iteration = self._get_iteration(ctx)
    self._logger.log(
        self._level,
        "[LLM] session=%s agent=%s iter=%d finish=%s usage=%s\n  content=%s",
        ctx.session_id,
        agent,
        iteration,
        response.finish_reason,
        self._format_value(response.usage, self._max_content_chars),
        self._format_text(response.content, self._max_content_chars),
    )
```

Note: no toolCall arguments printed, only tool name list is gone (intentionally removed from this event per spec). The `reasoning_content` is still logged separately if present — see step 3.

Wait — the spec says tool name list is fine but no arguments. Currently the method prints `tool_calls=%s` with `tool_names` list. Let me include that:

```python
async def after_llm_response(self, ctx: AgentContext[Any], response: LLMResponse) -> None:
    tool_names = [call.tool_name for call in response.tool_calls]
    agent = self._get_agent_name(ctx)
    iteration = self._get_iteration(ctx)
    self._logger.log(
        self._level,
        "[LLM] session=%s agent=%s iter=%d finish=%s tools=%s usage=%s\n  content=%s",
        ctx.session_id,
        agent,
        iteration,
        response.finish_reason,
        self._format_value(tool_names, self._max_content_chars),
        self._format_value(response.usage, self._max_content_chars),
        self._format_text(response.content, self._max_content_chars),
    )
```

- [ ] **Step 3: Rewrite `before_tool_execution` to two-line format**

```python
async def before_tool_execution(
    self,
    ctx: AgentContext[Any],
    tool_calls: list[Any] | None = None,
) -> None:
    if tool_calls is None:
        return
    self._pending_tool_calls[ctx.session_id] = list(tool_calls)
    agent = self._get_agent_name(ctx)
    iteration = self._get_iteration(ctx)
    for tool_call in tool_calls:
        tool_name = getattr(tool_call, "tool_name", "<unknown>")
        call_id = getattr(tool_call, "call_id", None)
        arguments = getattr(tool_call, "arguments", {}) or {}
        self._logger.log(
            self._level,
            "[TOOL_CALL] session=%s agent=%s iter=%d tool=%s call_id=%s\n  arguments=%s",
            ctx.session_id,
            agent,
            iteration,
            tool_name,
            call_id,
            self._format_value(arguments, self._max_content_chars),
        )
```

- [ ] **Step 4: Rewrite `after_tool_execution` to two-line format**

```python
async def after_tool_execution(
    self,
    ctx: AgentContext[Any],
    results: list[Any] | None = None,
) -> None:
    if results is None:
        return
    agent = self._get_agent_name(ctx)
    iteration = self._get_iteration(ctx)
    pending = self._pending_tool_calls.get(ctx.session_id, [])
    pending_by_call_id = {getattr(call, "call_id", None): call for call in pending}
    pending_by_name = {getattr(call, "tool_name", None): call for call in pending}

    for result in results:
        tool_name = self._result_tool_name(result)
        call_id = self._result_call_id(result)
        tool_call = pending_by_call_id.get(call_id) or pending_by_name.get(tool_name)
        arguments = getattr(tool_call, "arguments", {}) if tool_call is not None else {}
        error = self._result_error(result)
        output = self._result_output(result)
        self._logger.log(
            self._level,
            "[TOOL_RESULT] session=%s agent=%s iter=%d tool=%s call_id=%s success=%s\n  result=%s",
            ctx.session_id,
            agent,
            iteration,
            tool_name,
            call_id,
            error is None,
            self._format_value(
                output if error is None else {"error": error},
                self._max_result_chars,
            ),
        )

    self._pending_tool_calls.pop(ctx.session_id, None)
```

- [ ] **Step 5: Update existing tests in `tests/unit/test_hooks.py`**

The test assertions check for old format strings. Update `TestRunLoggingHook`:

- `test_logs_llm_output_with_session_id`: change `"LLM response"` to `"[LLM]"`, add assertions for `agent=` and `iter=`
- `test_logs_tool_call_and_result_with_session_id_and_arguments`: change `"Tool call start"` to `"[TOOL_CALL]"`, `"Tool call end"` to `"[TOOL_RESULT]"`
- `test_collapses_newlines_and_truncates_long_content`: the newline assertion `assert "\n" not in record.message` needs updating since the new two-line format intentionally contains `\n`. Change to verify the format: line 1 has the tag, line 2 starts with `  content=` or `  arguments=` or `  result=`

```python
# In test_collapses_newlines_and_truncates_long_content, replace:
#   for record in caplog.records:
#       assert "\n" not in record.message
# with:
for record in caplog.records:
    lines = record.message.split("\n")
    assert len(lines) == 2  # exactly two lines
    # Content line has no embedded newlines
    assert "\n" not in lines[1]
```

- [ ] **Step 6: Run tests to verify**

Run: `python -m pytest tests/unit/test_hooks.py -v`
Expected: all tests PASS

- [ ] **Step 7: Commit**

```bash
git add framework/hook/builtin/logging.py tests/unit/test_hooks.py
git commit -m "feat: enhance RunLoggingHook with agent_name, iteration, two-line format"
```

---

### Task 2: Enhance ProgressReportHook

**Files:**
- Modify: `framework/hook/builtin/progress_report.py`

The hook currently emits minimal metadata. We add `agent_name`, `iteration`, `max_iterations`, and full content (no truncation).

- [ ] **Step 1: Add helper methods and update `_emit`**

Add `_get_agent_name` (same pattern as RunLoggingHook) and update `_get_iteration` to also extract `max_iterations`. Replace the existing `_get_iteration` and add the new helper after it:

```python
def _get_agent_name(ctx: AgentContext[Any]) -> str:
    if ctx.session_meta is not None:
        return ctx.session_meta.agent_name
    if ctx.identity is not None:
        return ctx.identity.agent_id
    return "<unknown>"


def _get_max_iterations(ctx: AgentContext[Any]) -> int:
    return ctx.max_iterations
```

Update `_emit` to inject common fields:

```python
async def _emit(self, ctx: AgentContext[Any], payload: dict[str, Any]) -> None:
    try:
        payload["agent_name"] = _get_agent_name(ctx)
        payload["session_id"] = ctx.session_id
        payload["iteration"] = _get_iteration(ctx)
        payload["max_iterations"] = _get_max_iterations(ctx)
        await self._event_bus.emit(ControlEvent(
            event_id=uuid.uuid4().hex,
            type=ControlEventType.AGENT_PROGRESS,
            scope=ControlScope(session_id=ctx.session_id),
            payload=payload,
        ))
    except Exception:
        logger.debug("ProgressReportHook emit failed", exc_info=True)
```

Remove the old `session_id` that was passed via `ControlScope` only — now it's also in payload for convenience.

- [ ] **Step 2: Enhance `before_iteration` and `after_iteration`**

```python
async def before_iteration(self, ctx: AgentContext[Any]) -> None:
    await self._emit(ctx, {"phase": "iteration_start"})

async def after_iteration(self, ctx: AgentContext[Any]) -> None:
    await self._emit(ctx, {"phase": "iteration_end"})
```

The iteration and max_iterations are now added by `_emit` automatically.

- [ ] **Step 3: Enhance `after_llm_response` with full content**

```python
async def after_llm_response(
    self, ctx: AgentContext[Any], response: Any,
) -> None:
    content = getattr(response, "content", None) or ""
    reasoning = getattr(response, "reasoning_content", None)
    finish_reason = getattr(response, "finish_reason", None)
    usage = getattr(response, "usage", None)
    tool_names = [getattr(tc, "tool_name", "?") for tc in (getattr(response, "tool_calls", None) or [])]
    payload: dict[str, Any] = {
        "phase": "llm_response",
        "content": content,
        "finish_reason": finish_reason,
        "tool_names": tool_names,
    }
    if reasoning is not None:
        payload["reasoning"] = reasoning
    if usage is not None:
        payload["usage"] = usage
    await self._emit(ctx, payload)
```

- [ ] **Step 4: Enhance `before_tool_execution` and `after_tool_execution`**

```python
async def before_tool_execution(
    self, ctx: AgentContext[Any], tool_calls: list[Any],
) -> None:
    for tc in tool_calls:
        tool_name = getattr(tc, "tool_name", "?")
        call_id = getattr(tc, "call_id", None)
        arguments = getattr(tc, "arguments", None)
        payload: dict[str, Any] = {
            "phase": "tool_execution_start",
            "tool_name": tool_name,
            "call_id": call_id,
        }
        if arguments is not None:
            payload["arguments"] = arguments
        await self._emit(ctx, payload)

async def after_tool_execution(
    self, ctx: AgentContext[Any], results: list[Any],
) -> None:
    for r in results:
        tool_name = getattr(r, "tool_name", "?")
        call_id = getattr(r, "call_id", None)
        error = getattr(r, "error", None)
        result = getattr(r, "result", None)
        payload: dict[str, Any] = {
            "phase": "tool_execution_end",
            "tool_name": tool_name,
            "call_id": call_id,
            "success": error is None,
        }
        if error is not None:
            payload["error"] = str(error)
        if result is not None:
            payload["result"] = result
        await self._emit(ctx, payload)
```

- [ ] **Step 5: Enhance `after_turn` with phase subdivision**

```python
async def after_turn(
    self, ctx: AgentContext[Any], result: Any,
) -> None:
    stop_reason = getattr(result, "stop_reason", "") if result else ""
    if stop_reason == "max_iterations":
        await self._emit(ctx, {
            "phase": "turn_max_iterations",
            "stop_reason": stop_reason,
        })
    elif stop_reason == "error":
        error = getattr(result, "error", None) if result else None
        await self._emit(ctx, {
            "phase": "turn_error",
            "stop_reason": stop_reason,
            "error": str(error) if error else None,
        })
    elif stop_reason == "turn_cancelled":
        partial = getattr(result, "partial_content", None) if result else None
        await self._emit(ctx, {
            "phase": "turn_cancelled",
            "stop_reason": stop_reason,
            "partial_content": partial,
        })
    else:
        await self._emit(ctx, {
            "phase": "turn_complete",
            "stop_reason": stop_reason,
        })
```

- [ ] **Step 6: Run existing tests**

Run: `python -m pytest tests/ -v -k "not slow"`
Expected: all tests PASS (ProgressReportHook has no dedicated unit tests currently; verify no regressions)

- [ ] **Step 7: Commit**

```bash
git add framework/hook/builtin/progress_report.py
git commit -m "feat: enhance ProgressReportHook with full-content events and phase subdivision"
```

---

### Task 3: Create TraceFileWriter

**Files:**
- Create: `framework/hook/builtin/trace_writer.py`
- Modify: `framework/hook/builtin/__init__.py`

A `ControlEventBus` subscriber that writes `AGENT_PROGRESS` events as JSON-lines to a file with rotation.

- [ ] **Step 1: Create `trace_writer.py`**

```python
"""TraceFileWriter — ControlEventBus subscriber that writes trace events to JSON-lines file."""

from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from framework.control.types import ControlEvent, ControlEventType

logger = logging.getLogger(__name__)


class TraceFileWriter:
    """Writes AGENT_PROGRESS events to a JSON-lines file with rotation.

    Usage::

        writer = TraceFileWriter(path=Path("logs/trace.jsonl"))
        await event_bus.subscribe(ControlEventType.AGENT_PROGRESS, writer.handle)
    """

    def __init__(
        self,
        path: Path,
        max_bytes: int = 20 * 1024 * 1024,
        backup_count: int = 5,
    ) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handler = logging.handlers.RotatingFileHandler(
            str(path),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )

    async def handle(self, event: ControlEvent) -> None:
        if event.type != ControlEventType.AGENT_PROGRESS:
            return
        try:
            entry = {
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                **event.payload,
            }
            line = json.dumps(entry, ensure_ascii=False, default=str)
            self._handler.emit(logging.LogRecord(
                name="trace",
                level=logging.INFO,
                pathname="",
                lineno=0,
                msg=line,
                args=None,
                exc_info=None,
            ))
        except Exception:
            logger.debug("TraceFileWriter write failed", exc_info=True)

    def close(self) -> None:
        self._handler.close()
```

- [ ] **Step 2: Update `__init__.py` to export TraceFileWriter**

Add import:
```python
from framework.hook.builtin.trace_writer import TraceFileWriter
```

Add to `__all__`:
```python
"TraceFileWriter",
```

- [ ] **Step 3: Run tests to verify no import errors**

Run: `python -m pytest tests/unit/test_hooks.py -v`
Expected: all tests PASS

- [ ] **Step 4: Commit**

```bash
git add framework/hook/builtin/trace_writer.py framework/hook/builtin/__init__.py
git commit -m "feat: add TraceFileWriter — ControlEventBus subscriber with file rotation"
```

---

### Task 4: Integrate into bot_project

**Files:**
- Modify: `examples/bot_project/bot/service/core.py`

Wire `CallbackControlEventBus` + `ProgressReportHook` + `TraceFileWriter` into the bot service lifecycle.

- [ ] **Step 1: Add event_bus and trace_writer fields to BotService**

In the class body (near other `self._*` fields in `__init__` or `_initialize_pool`), add:

In `_initialize_pool` (after shared hook runner creation, around line 590):

```python
# 4b. Shared infra: Observability event bus + trace writer
from framework.control import CallbackControlEventBus, ControlEventType
from framework.hook.builtin import ProgressReportHook, TraceFileWriter

self._event_bus = CallbackControlEventBus()
trace_dir = self._project_dir / "logs"
trace_dir.mkdir(exist_ok=True)
self._trace_writer = TraceFileWriter(path=trace_dir / "trace.jsonl")
await self._event_bus.subscribe(ControlEventType.AGENT_PROGRESS, self._trace_writer.handle)
shared_hooks.append(ProgressReportHook(event_bus=self._event_bus))
```

Note: `shared_hooks` is a list, `shared_hook_runner` is already built from it. The ProgressReportHook must be appended to `shared_hooks` BEFORE `_build_hook_runner(shared_hooks)` is called. So the insertion point must be before line 589 (`shared_hook_runner = self._build_hook_runner(shared_hooks)`).

Reorder the block (lines 587-590) to:

```python
# 4. Shared infra: Hooks & Interceptors
shared_hooks = self._collect_run_hooks()

# 4b. Shared infra: Observability event bus + trace writer
from framework.control import CallbackControlEventBus, ControlEventType
from framework.hook.builtin import ProgressReportHook, TraceFileWriter

self._event_bus = CallbackControlEventBus()
trace_dir = self._project_dir / "logs"
trace_dir.mkdir(exist_ok=True)
self._trace_writer = TraceFileWriter(path=trace_dir / "trace.jsonl")
await self._event_bus.subscribe(ControlEventType.AGENT_PROGRESS, self._trace_writer.handle)
shared_hooks.append(ProgressReportHook(event_bus=self._event_bus))

shared_hook_runner = self._build_hook_runner(shared_hooks)
shared_interceptor_chain = self._build_interceptor_chain()
```

- [ ] **Step 2: Verify bot_project can start**

Run: `cd examples/bot_project && python -c "from bot.service import BotService; print('import ok')"`
Expected: `import ok`

- [ ] **Step 3: Commit**

```bash
git add examples/bot_project/bot/service/core.py
git commit -m "feat: wire ProgressReportHook + TraceFileWriter into bot_project"
```

---

### Task 5: Integration test

**Files:**
- Modify: `tests/unit/test_hooks.py`

Add tests for the enhanced ProgressReportHook to verify event payload structure.

- [ ] **Step 1: Add ProgressReportHook test class**

```python
from framework.control.event_bus import CallbackControlEventBus
from framework.control.types import ControlEvent, ControlEventType, ControlScope
from framework.hook.builtin import ProgressReportHook


class TestProgressReportHook:
    """ProgressReportHook emits full-content AGENT_PROGRESS events."""

    @pytest.mark.asyncio
    async def test_llm_response_event_contains_full_content(self):
        events: list[ControlEvent] = []

        async def capture(event: ControlEvent) -> None:
            events.append(event)

        bus = CallbackControlEventBus()
        await bus.subscribe(ControlEventType.AGENT_PROGRESS, capture)

        hook = ProgressReportHook(event_bus=bus)
        ctx = AgentContext(
            system_prompt="",
            history=None,
            tool_manager=None,
            session_id="s-1",
            session_meta=AgentSessionMeta(
                conversation_id="c-1",
                agent_name="main",
                comm_kind=None,  # type: ignore
            ),
        )

        response = LLMResponse(
            content="full response content",
            reasoning_content="thinking...",
            finish_reason="stop",
            usage={"prompt_tokens": 10},
            tool_calls=[ToolCall(tool_name="read", arguments={"path": "/x"}, call_id="c1")],
        )
        await hook.after_llm_response(ctx, response)

        assert len(events) == 1
        p = events[0].payload
        assert p["phase"] == "llm_response"
        assert p["content"] == "full response content"
        assert p["agent_name"] == "main"
        assert p["iteration"] == 0
        assert p["tool_names"] == ["read"]
        assert "arguments" not in p  # no tool call args in LLM response event

    @pytest.mark.asyncio
    async def test_turn_max_iterations_phase(self):
        events: list[ControlEvent] = []

        async def capture(event: ControlEvent) -> None:
            events.append(event)

        bus = CallbackControlEventBus()
        await bus.subscribe(ControlEventType.AGENT_PROGRESS, capture)

        hook = ProgressReportHook(event_bus=bus)
        ctx = AgentContext(
            system_prompt="",
            history=None,
            tool_manager=None,
            session_id="s-2",
            max_iterations=50,
        )

        from framework.core.emitter import AgentResult
        result = AgentResult(content="stopped", stop_reason="max_iterations")
        await hook.after_turn(ctx, result)

        assert len(events) == 1
        assert events[0].payload["phase"] == "turn_max_iterations"
        assert events[0].payload["max_iterations"] == 50

    @pytest.mark.asyncio
    async def test_tool_execution_events_contain_full_arguments(self):
        events: list[ControlEvent] = []

        async def capture(event: ControlEvent) -> None:
            events.append(event)

        bus = CallbackControlEventBus()
        await bus.subscribe(ControlEventType.AGENT_PROGRESS, capture)

        hook = ProgressReportHook(event_bus=bus)
        ctx = AgentContext(
            system_prompt="",
            history=None,
            tool_manager=None,
            session_id="s-3",
        )

        tool_call = ToolCall(tool_name="write", arguments={"path": "/a", "content": "x" * 100}, call_id="c2")
        await hook.before_tool_execution(ctx, [tool_call])

        assert len(events) == 1
        p = events[0].payload
        assert p["phase"] == "tool_execution_start"
        assert p["tool_name"] == "write"
        assert p["arguments"]["content"] == "x" * 100  # no truncation
```

Add the missing import at the top of the file:

```python
from framework.core.agent import AgentContext, AgentSessionMeta
```

- [ ] **Step 2: Run all tests**

Run: `python -m pytest tests/unit/test_hooks.py -v`
Expected: all tests PASS

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_hooks.py
git commit -m "test: add ProgressReportHook integration tests for enhanced events"
```

---

## Self-Review

**Spec coverage:**
- RunLoggingHook agent_name + iteration + two-line format → Task 1 ✓
- RunLoggingHook skip toolCall args in LLM response → Task 1 Step 2 ✓
- ProgressReportHook full content, no truncation → Task 2 ✓
- ProgressReportHook phase subdivision (turn_max_iterations, turn_error, turn_cancelled) → Task 2 Step 5 ✓
- ProgressReportHook iteration + max_iterations in all events → Task 2 Step 1 ✓
- TraceFileWriter JSON-lines + rotation → Task 3 ✓
- bot_project integration → Task 4 ✓
- File rotation (20MB, 5 backups) → Task 3 Step 1 ✓

**Placeholder scan:** No TBD/TODO found. All steps have complete code.

**Type consistency:** `_get_agent_name` uses same pattern (session_meta.agent_name → identity.agent_id → "<unknown>") in both RunLoggingHook and ProgressReportHook. `_emit` injects `agent_name`, `session_id`, `iteration`, `max_iterations` consistently.
