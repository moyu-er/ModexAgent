# Subagent Enhancement v2 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enhance subagent mechanism with crash-safe lifecycle hooks, unified trace system, output.md protocol, rewritten auto-send hook, and LRU instance pool.

**Architecture:** Add `FINALLY_TURN` hook point in `framework/hook/abc.py` + `runner.py` dispatching from `ReActAgent.run()` finally block. New `framework/trace/` module with `TraceStore` ABC, `JsonFileTraceStore`, and `TraceCollectorHook`. Rewrite `SubagentAutoSendHook` as `FinallyTurnHook` with deterministic trace/output path derivation. Add `SubagentPool` (LRU) in `framework/multi_agent/pool_reuse.py`. Integrate `_ensure_invocation()` in `AgentCommunicationService` to auto-create trace/output paths for subagent invocations.

**Tech Stack:** Python 3.12+, asyncio, dataclasses, JSON Lines, pytest-asyncio, existing `framework/hook/` and `framework/multi_agent/` patterns.

---

### Task 1: Add `FinallyTurnHook` ABC and `FINALLY_TURN` HookPoint

**Files:**
- Modify: `framework/hook/abc.py`
- Modify: `framework/hook/runner.py`
- Modify: `framework/hook/__init__.py`
- Create: `tests/unit/hook/test_finally_turn.py`

- [ ] **Step 1: Add `FINALLY_TURN` to `HookPoint` enum**

In `framework/hook/abc.py`, add to the `HookPoint` enum (after `FINALIZE_CONTENT`):

```python
FINALLY_TURN = "finally_turn"
```

- [ ] **Step 2: Add `FinallyTurnHook` ABC**

In `framework/hook/abc.py`, add after `FinalizeContentHook` (before its `finalize_content` method):

```python
class FinallyTurnHook(Hook[R]):
    _hook_point = HookPoint.FINALLY_TURN

    @abstractmethod
    async def finally_turn(self, ctx: AgentContext[R], result: AgentResult | None) -> None: ...
```

- [ ] **Step 3: Add `_FinallyTurnPayload` TypedDict to `runner.py`**

In `framework/hook/runner.py`, add after `_FinalizeContentPayload`:

```python
class _FinallyTurnPayload(TypedDict, total=False):
    result: "AgentResult | None"
```

- [ ] **Step 4: Add `_call_finally_turn` dispatch helper to `runner.py`**

In `framework/hook/runner.py`, add after the `_call_finalize_content` function:

```python
async def _call_finally_turn(
    hook: "FinallyTurnHook", ctx: "AgentContext[R]", **kw: Unpack[_FinallyTurnPayload]
) -> None:
    await hook.finally_turn(ctx, kw.get("result"))
```

- [ ] **Step 5: Add temporary import for `FinallyTurnHook` in `runner.py`**

In `framework/hook/runner.py`, add near other hook ABC imports (at bottom of existing imports block):

```python
from framework.hook.abc import (
    # ... existing imports ...
    FinallyTurnHook,
)
```

Update the full import to include `FinallyTurnHook` alongside the others.

- [ ] **Step 6: Register `FINALLY_TURN` in `_HOOK_DISPATCH` dict**

In `framework/hook/runner.py`, add to `_HOOK_DISPATCH`:

```python
_HOOK_DISPATCH: dict[HookPoint, tuple[type, Callable[..., Any]]] = {
    # ... existing entries ...
    HookPoint.FINALLY_TURN: (FinallyTurnHook, _call_finally_turn),
}
```

- [ ] **Step 7: Export `FinallyTurnHook` from `framework/hook/__init__.py`**

Add `FinallyTurnHook` to the import block and `__all__` list:

```python
from framework.hook.abc import (
    # ... existing imports ...
    FinallyTurnHook,
)
```

```python
__all__ = [
    # ... existing exports ...
    "FinallyTurnHook",
]
```

- [ ] **Step 8: Write unit test for `HookRunner` dispatching `finally_turn`**

Create `tests/unit/hook/test_finally_turn.py`:

```python
from __future__ import annotations

import asyncio

from framework.core.agent import AgentContext
from framework.core.emitter import AgentResult
from framework.hook import (
    FinallyTurnHook,
    HookErrorPolicy,
    HookPayload,
    HookPoint,
    HookResult,
    HookSpec,
    HookRunner,
)


class _TestFinallyHook(FinallyTurnHook):
    def __init__(self):
        self._calls: list[AgentResult | None] = []

    @property
    def name(self) -> str:
        return "test_finally"

    async def finally_turn(self, ctx, result):
        self._calls.append(result)

    @property
    def calls(self) -> list[AgentResult | None]:
        return self._calls


class TestFinallyTurnDispatch:
    async def test_finally_turn_dispatches_with_result(self):
        hook = _TestFinallyHook()
        runner = HookRunner([HookSpec(hook=hook)])
        result = AgentResult(content="done", stop_reason="completed")
        # Use a minimal agent context; HookRunner only uses context for pass-through
        ctx = _make_minimal_context()

        await runner.dispatch(
            HookPoint.FINALLY_TURN, ctx, HookPayload(data={"result": result})
        )
        assert len(hook.calls) == 1
        assert hook.calls[0] is result

    async def test_finally_turn_dispatches_with_none_result(self):
        hook = _TestFinallyHook()
        runner = HookRunner([HookSpec(hook=hook)])
        ctx = _make_minimal_context()

        await runner.dispatch(
            HookPoint.FINALLY_TURN, ctx, HookPayload(data={"result": None})
        )
        assert len(hook.calls) == 1
        assert hook.calls[0] is None

    async def test_finally_turn_does_not_dispatch_other_hook_types(self):
        """Non-FinallyTurnHook instances are skipped for FINALLY_TURN dispatch."""
        hook = _TestFinallyHook()
        runner = HookRunner([HookSpec(hook=hook)])
        ctx = _make_minimal_context()

        # Dispatch BEFORE_TURN — should not trigger finally_turn
        await runner.dispatch(HookPoint.BEFORE_TURN, ctx)
        assert len(hook.calls) == 0

    async def test_finally_turn_error_ignored(self):
        class _ErrorHook(FinallyTurnHook):
            @property
            def name(self) -> str:
                return "error_hook"

            async def finally_turn(self, ctx, result):
                raise RuntimeError("test error")

        runner = HookRunner([
            HookSpec(hook=_ErrorHook(), on_error=HookErrorPolicy.IGNORE),
        ])
        ctx = _make_minimal_context()
        # Should not raise
        await runner.dispatch(
            HookPoint.FINALLY_TURN, ctx,
            HookPayload(data={"result": AgentResult(content="x")}),
        )

    async def test_finally_turn_error_abort(self):
        class _ErrorHook(FinallyTurnHook):
            @property
            def name(self) -> str:
                return "error_hook"

            async def finally_turn(self, ctx, result):
                raise RuntimeError("test error")

        import pytest
        from framework.control.exceptions import PolicyViolation

        runner = HookRunner([
            HookSpec(hook=_ErrorHook(), on_error=HookErrorPolicy.ABORT),
        ])
        ctx = _make_minimal_context()
        with pytest.raises(PolicyViolation):
            await runner.dispatch(
                HookPoint.FINALLY_TURN, ctx,
                HookPayload(data={"result": AgentResult(content="x")}),
            )


def _make_minimal_context() -> AgentContext:
    from framework.core.tool_manager import InMemoryToolManager, ToolManagerConfig
    from framework.memory.history import MessageHistory

    return AgentContext(
        system_prompt="test",
        history=MessageHistory(),
        tool_manager=InMemoryToolManager(config=ToolManagerConfig()),
        session_id="test:agent",
    )
```

- [ ] **Step 9: Run test to verify it fails**

```bash
pytest tests/unit/hook/test_finally_turn.py -v
```

Expected: import errors or dispatch failures until Step 10 completes runner changes.

- [ ] **Step 10: Run full test to verify it passes**

```bash
pytest tests/unit/hook/test_finally_turn.py -v
```

Expected: 5 passed

- [ ] **Step 11: Run existing hook tests to verify no regression**

```bash
pytest tests/unit/hook/ -v
```

Expected: all existing tests pass

- [ ] **Step 12: Commit**

```bash
git add framework/hook/abc.py framework/hook/runner.py framework/hook/__init__.py tests/unit/hook/test_finally_turn.py
git commit -m "feat(hook): add FinallyTurnHook ABC and FINALLY_TURN hook point"
```

---

### Task 2: Wire `FINALLY_TURN` dispatch in `ReActAgent.run()`

**Files:**
- Modify: `framework/agents/react/agent.py`

- [ ] **Step 1: Replace the TODO comments in `finally` block with actual dispatch**

In `framework/agents/react/agent.py`, replace lines 230-249 (the two TODO blocks):

```python
        except Exception as e:
            logger.exception("Agent execution error")
            await emitter.emit(ReActEvent.ERROR, str(e))
            all_new = _get_turn_messages(context)
            result = AgentResult(
                error=str(e), stop_reason=StopReason.ERROR,
                messages=all_new, attachments=context.attachments,
            )
            await emitter.emit_complete(result)
            return result
        finally:
            # FINALLY_TURN: fires regardless of success/error/cancel.
            # SubagentAutoSendHook and cleanup hooks always execute.
            if runtime.hooks:
                try:
                    await runtime.hooks.dispatch(
                        HookPoint.FINALLY_TURN, context,
                        HookPayload(data={"result": result}),
                    )
                except Exception:
                    logger.exception("FINALLY_TURN hook dispatch failed")
            # Clean up typed state
            state = get_react_state(context)
```

That is, remove the two TODO comment blocks. Keep the existing `finally` block's cleanup (phase marking, emitter reset, context token reset). Insert the `FINALLY_TURN` dispatch **before** the cleanup calls.

- [ ] **Step 2: Verify the file has no syntax/LSP errors**

```bash
python -c "import framework.agents.react.agent"
```

Expected: clean import, no errors

- [ ] **Step 3: Run existing react agent tests**

```bash
pytest tests/unit/ -k "react" -v --timeout=60
```

Expected: all existing tests pass

- [ ] **Step 4: Commit**

```bash
git add framework/agents/react/agent.py
git commit -m "feat(react): wire FINALLY_TURN hook dispatch in ReActAgent.run() finally block"
```

---

### Task 3: Trace System — Types and Store

**Files:**
- Create: `framework/trace/__init__.py`
- Create: `framework/trace/types.py`
- Create: `framework/trace/store.py`
- Create: `tests/unit/trace/__init__.py`
- Create: `tests/unit/trace/test_store.py`

- [ ] **Step 1: Create `framework/trace/__init__.py`**

```python
"""framework.trace — Unified operation-level trace system for all agents."""

from framework.trace.store import JsonFileTraceStore, TraceStore
from framework.trace.types import OperationKind, OperationRecord, OperationStatus

__all__ = [
    "JsonFileTraceStore",
    "OperationKind",
    "OperationRecord",
    "OperationStatus",
    "TraceStore",
]
```

- [ ] **Step 2: Create `framework/trace/types.py`**

```python
"""Trace types: OperationRecord and related enums."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from framework.runtime.enums import OperationKind, OperationStatus


@dataclass
class OperationRecord:
    """A single operation traced during agent execution.

    One JSON line in operations.jsonl.  ``trace_id`` groups all operations
    in one turn; ``session_id`` groups all turns in one agent session.
    """

    trace_id: str                # Globally unique per turn
    session_id: str              # {conv}:{agent}[:{invocation}]
    agent_name: str
    invocation_id: str | None = None
    kind: OperationKind = OperationKind.LLM_CALL
    status: OperationStatus = OperationStatus.COMPLETED
    timestamp: float = 0.0
    duration_ms: int | None = None   # null for start/end markers
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-serialisable dict (one line in .jsonl)."""
        d: dict[str, Any] = {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "agent_name": self.agent_name,
            "kind": str(self.kind),
            "status": str(self.status),
            "timestamp": self.timestamp,
        }
        if self.invocation_id is not None:
            d["invocation_id"] = self.invocation_id
        if self.duration_ms is not None:
            d["duration_ms"] = self.duration_ms
        if self.metadata:
            d["metadata"] = self.metadata
        if self.error:
            d["error"] = self.error
        return d
```

Note: `OperationKind` and `OperationStatus` already exist in `framework/runtime/enums.py` as `StrEnum` values. We reuse them. Verify they include the needed values:

Required `OperationKind`: `LLM_CALL`, `TOOL_BATCH`, `TOOL_CALL`, `APPROVAL`, `CONTROL_COMMAND`
Required `OperationStatus`: `CREATED`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED`

These all exist in the current enums. If missing, add to the enum.

- [ ] **Step 3: Add `TURN_START` and `TURN_END` to `OperationKind` if not present**

In `framework/runtime/enums.py`, check the `OperationKind` enum. If `TURN_START` and `TURN_END` are missing, add them:

```python
class OperationKind(StrEnum):
    LLM_CALL = "llm_call"
    TOOL_BATCH = "tool_batch"
    TOOL_CALL = "tool_call"
    APPROVAL = "approval"
    CONTROL_COMMAND = "control_command"
    TURN_START = "turn_start"    # NEW
    TURN_END = "turn_end"        # NEW
    ERROR = "error"              # NEW
```

- [ ] **Step 4: Create `framework/trace/store.py`**

```python
"""Trace storage: ABC and JSON-lines file implementation."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from framework.trace.types import OperationRecord

logger = logging.getLogger(__name__)


class TraceStore(ABC):
    """Abstract interface for persisting trace operation records."""

    @abstractmethod
    async def save(self, record: "OperationRecord") -> None:
        """Persist a single operation record."""
        ...

    @abstractmethod
    async def list_by_session(self, session_id: str) -> list["OperationRecord"]:
        """Return all records for a session, ordered by timestamp."""
        ...

    @abstractmethod
    async def list_by_trace_id(self, trace_id: str) -> list["OperationRecord"]:
        """Return all records with the given trace_id."""
        ...


class JsonFileTraceStore(TraceStore):
    """Append-only JSON Lines trace store, one file per session."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = Path(base_dir)

    def _path(self, session_id: str) -> Path:
        return self._base_dir / session_id / "operations.jsonl"

    async def save(self, record: "OperationRecord") -> None:
        path = self._path(record.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record.to_json_dict(), ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    async def list_by_session(self, session_id: str) -> list["OperationRecord"]:
        from framework.trace.types import OperationRecord as OR

        path = self._path(session_id)
        if not path.exists():
            return []
        records: list[OR] = []
        for line in path.read_text(encoding="utf-8").strip().split("\n"):
            if not line.strip():
                continue
            data = json.loads(line)
            records.append(OR(
                trace_id=data["trace_id"],
                session_id=data["session_id"],
                agent_name=data["agent_name"],
                invocation_id=data.get("invocation_id"),
                kind=data["kind"],
                status=data["status"],
                timestamp=data["timestamp"],
                duration_ms=data.get("duration_ms"),
                metadata=data.get("metadata", {}),
                error=data.get("error"),
            ))
        return records

    async def list_by_trace_id(self, trace_id: str) -> list["OperationRecord"]:
        # Naive scan — acceptable for trace files that are per-session bounded
        results: list["OperationRecord"] = []
        if not self._base_dir.exists():
            return results
        for session_dir in self._base_dir.iterdir():
            if not session_dir.is_dir():
                continue
            jsonl = session_dir / "operations.jsonl"
            if not jsonl.exists():
                continue
            for line in jsonl.read_text(encoding="utf-8").strip().split("\n"):
                if not line.strip():
                    continue
                data = json.loads(line)
                if data.get("trace_id") == trace_id:
                    from framework.trace.types import OperationRecord as OR
                    results.append(OR(
                        trace_id=data["trace_id"],
                        session_id=data["session_id"],
                        agent_name=data["agent_name"],
                        invocation_id=data.get("invocation_id"),
                        kind=data["kind"],
                        status=data["status"],
                        timestamp=data["timestamp"],
                        duration_ms=data.get("duration_ms"),
                        metadata=data.get("metadata", {}),
                        error=data.get("error"),
                    ))
        return results
```

- [ ] **Step 5: Create `tests/unit/trace/test_store.py`**

```python
from __future__ import annotations

import tempfile
from pathlib import Path

from framework.trace import JsonFileTraceStore, OperationKind, OperationRecord, OperationStatus


class TestJsonFileTraceStore:
    async def test_save_and_list_by_session(self):
        with tempfile.TemporaryDirectory() as td:
            store = JsonFileTraceStore(Path(td))
            rec = OperationRecord(
                trace_id="trace_1",
                session_id="conv:worker:a1b2",
                agent_name="worker",
                invocation_id="a1b2",
                kind=OperationKind.TURN_START,
                status=OperationStatus.COMPLETED,
                timestamp=1718123456.0,
            )
            await store.save(rec)

            records = await store.list_by_session("conv:worker:a1b2")
            assert len(records) == 1
            assert records[0].trace_id == "trace_1"
            assert records[0].kind == OperationKind.TURN_START

    async def test_list_by_session_empty_when_no_file(self):
        with tempfile.TemporaryDirectory() as td:
            store = JsonFileTraceStore(Path(td))
            records = await store.list_by_session("nonexistent")
            assert records == []

    async def test_list_by_trace_id(self):
        with tempfile.TemporaryDirectory() as td:
            store = JsonFileTraceStore(Path(td))
            rec1 = OperationRecord(
                trace_id="trace_1", session_id="conv:worker:a1b2",
                agent_name="worker", kind=OperationKind.TURN_START,
                status=OperationStatus.COMPLETED, timestamp=1.0,
            )
            rec2 = OperationRecord(
                trace_id="trace_2", session_id="conv:worker:a1b2",
                agent_name="worker", kind=OperationKind.TOOL_CALL,
                status=OperationStatus.COMPLETED, timestamp=2.0,
            )
            await store.save(rec1)
            await store.save(rec2)

            by_trace = await store.list_by_trace_id("trace_1")
            assert len(by_trace) == 1
            assert by_trace[0].trace_id == "trace_1"

    async def test_save_creates_parent_dir(self):
        with tempfile.TemporaryDirectory() as td:
            store = JsonFileTraceStore(Path(td))
            rec = OperationRecord(
                trace_id="t1", session_id="deep/nested/session",
                agent_name="x", kind=OperationKind.TURN_START,
                status=OperationStatus.COMPLETED, timestamp=1.0,
            )
            await store.save(rec)
            jsonl = Path(td) / "deep" / "nested" / "session" / "operations.jsonl"
            assert jsonl.exists()
```

- [ ] **Step 6: Run trace store tests**

```bash
pytest tests/unit/trace/test_store.py -v
```

Expected: 4 passed

- [ ] **Step 7: Commit**

```bash
git add framework/trace/ tests/unit/trace/ framework/runtime/enums.py
git commit -m "feat(trace): add TraceStore ABC, JsonFileTraceStore, OperationRecord types"
```

---

### Task 4: Trace System — TraceCollectorHook

**Files:**
- Create: `framework/trace/hooks.py`
- Modify: `framework/trace/__init__.py`
- Modify: `framework/runtime/enums.py` (TurnCustomKey)
- Create: `tests/unit/trace/test_hooks.py`

- [ ] **Step 1: Add `TRACE_ID` to `TurnCustomKey` enum**

In `framework/runtime/enums.py`, add to `TurnCustomKey`:

```python
TRACE_ID = "_trace_id"
```

- [ ] **Step 2: Create `framework/trace/hooks.py`**

```python
"""TraceCollectorHook — records every operation via lifecycle hooks."""

from __future__ import annotations

import time
import uuid
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from framework.hook.abc import (
    AfterLLMResponseHook,
    AfterToolExecutionHook,
    BeforeToolExecutionHook,
    BeforeTurnHook,
    FinallyTurnHook,
)
from framework.runtime.enums import TurnCustomKey
from framework.trace.types import OperationKind, OperationRecord, OperationStatus

if TYPE_CHECKING:
    from framework.core.agent import AgentContext
    from framework.core.emitter import AgentResult
    from framework.core.tool_manager import ToolResult
    from framework.core.types import LLMResponse, ToolCall
    from framework.trace.store import TraceStore

logger = logging.getLogger(__name__)


class TraceCollectorHook(
    BeforeTurnHook,
    AfterLLMResponseHook,
    BeforeToolExecutionHook,
    AfterToolExecutionHook,
    FinallyTurnHook,
):
    """Collects an OperationRecord for every lifecycle event.

    Injected via RuntimeServicesConfig when tracing is enabled (default on).
    Session-level ``trace_id`` is generated on BEFORE_TURN and consumed
    by all downstream hooks for the same turn.
    """

    def __init__(self, store: "TraceStore", *, enabled: bool = True) -> None:
        self._store = store
        self._enabled = enabled

    @property
    def name(self) -> str:
        return "trace_collector"

    # -- helpers --------------------------------------------------------------

    def _now(self) -> float:
        return time.time()

    def _trace_id(self, ctx: "AgentContext") -> str:
        tid = ctx.runtime.state.custom.get(TurnCustomKey.TRACE_ID) if ctx.runtime else None
        if tid is None:
            tid = uuid.uuid4().hex[:12]
        return str(tid)

    async def _save(self, rec: "OperationRecord") -> None:
        if not self._enabled:
            return
        try:
            await self._store.save(rec)
        except Exception:
            logger.exception("TraceCollectorHook: failed to save operation record")

    # -- hook methods ---------------------------------------------------------

    async def before_turn(self, ctx: "AgentContext") -> None:
        if not self._enabled:
            return
        trace_id = uuid.uuid4().hex[:12]
        if ctx.runtime is not None:
            ctx.runtime.state.custom[TurnCustomKey.TRACE_ID] = trace_id
        rec = OperationRecord(
            trace_id=trace_id,
            session_id=ctx.session_id,
            agent_name=self._agent_name(ctx),
            invocation_id=self._invocation_id(ctx),
            kind=OperationKind.TURN_START,
            status=OperationStatus.CREATED,
            timestamp=self._now(),
        )
        await self._save(rec)

    async def after_llm_response(self, ctx: "AgentContext", response: "LLMResponse") -> None:
        if not self._enabled:
            return
        rec = OperationRecord(
            trace_id=self._trace_id(ctx),
            session_id=ctx.session_id,
            agent_name=self._agent_name(ctx),
            invocation_id=self._invocation_id(ctx),
            kind=OperationKind.LLM_CALL,
            status=OperationStatus.COMPLETED,
            timestamp=self._now(),
            metadata={
                "finish_reason": response.finish_reason or "",
                "tool_calls_count": len(response.tool_calls or []),
            },
        )
        await self._save(rec)

    async def before_tool_execution(self, ctx: "AgentContext", tool_calls: "list[ToolCall]") -> None:
        if not self._enabled:
            return
        rec = OperationRecord(
            trace_id=self._trace_id(ctx),
            session_id=ctx.session_id,
            agent_name=self._agent_name(ctx),
            invocation_id=self._invocation_id(ctx),
            kind=OperationKind.TOOL_BATCH,
            status=OperationStatus.RUNNING,
            timestamp=self._now(),
            metadata={
                "tool_count": len(tool_calls),
                "tool_names": [tc.tool_name for tc in tool_calls],
            },
        )
        await self._save(rec)

    async def after_tool_execution(self, ctx: "AgentContext", results: "list[ToolResult]") -> None:
        if not self._enabled:
            return
        for r in results:
            error = r.error or ""
            rec = OperationRecord(
                trace_id=self._trace_id(ctx),
                session_id=ctx.session_id,
                agent_name=self._agent_name(ctx),
                invocation_id=self._invocation_id(ctx),
                kind=OperationKind.TOOL_CALL,
                status=OperationStatus.FAILED if error else OperationStatus.COMPLETED,
                timestamp=self._now(),
                metadata={"tool_name": r.tool_name},
                error=error if error else None,
            )
            await self._save(rec)

    async def finally_turn(self, ctx: "AgentContext", result: "AgentResult | None") -> None:
        if not self._enabled:
            return
        stop_reason = getattr(result, "stop_reason", "error") if result else "error"
        error = getattr(result, "error", None) if result else "subagent crashed"
        rec = OperationRecord(
            trace_id=self._trace_id(ctx),
            session_id=ctx.session_id,
            agent_name=self._agent_name(ctx),
            invocation_id=self._invocation_id(ctx),
            kind=OperationKind.TURN_END,
            status=OperationStatus.FAILED if error else OperationStatus.COMPLETED,
            timestamp=self._now(),
            metadata={"stop_reason": str(stop_reason) if stop_reason else ""},
            error=str(error) if error else None,
        )
        await self._save(rec)

    # -- static helpers -------------------------------------------------------

    @staticmethod
    def _agent_name(ctx: "AgentContext") -> str:
        if ctx.session_meta is not None:
            return ctx.session_meta.agent_name
        return ctx.identity.agent_id if ctx.identity else "unknown"

    @staticmethod
    def _invocation_id(ctx: "AgentContext") -> str | None:
        if ctx.session_meta is not None:
            return ctx.session_meta.invocation_id
        return None
```

- [ ] **Step 3: Update `framework/trace/__init__.py`** to export new classes

Add to imports:
```python
from framework.trace.hooks import TraceCollectorHook
```

Add to `__all__`:
```python
"TraceCollectorHook",
```

- [ ] **Step 4: Create `tests/unit/trace/test_hooks.py`**

```python
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from framework.trace import (
    JsonFileTraceStore,
    OperationKind,
    OperationRecord,
    OperationStatus,
    TraceCollectorHook,
)
from framework.runtime.enums import TurnCustomKey


class TestTraceCollectorHook:
    @pytest.fixture
    async def store_and_hook(self):
        with tempfile.TemporaryDirectory() as td:
            store = JsonFileTraceStore(Path(td))
            hook = TraceCollectorHook(store)
            ctx = _make_trace_context(session_id="conv:test_agent:a1b2")
            yield store, hook, ctx, Path(td)

    async def test_before_turn_records_turn_start(self, store_and_hook):
        store, hook, ctx, _ = store_and_hook
        await hook.before_turn(ctx)

        records = await store.list_by_session("conv:test_agent:a1b2")
        assert len(records) == 1
        assert records[0].kind == OperationKind.TURN_START
        assert records[0].status == OperationStatus.CREATED
        # trace_id stored in custom state
        assert ctx.runtime.state.custom.get(TurnCustomKey.TRACE_ID) is not None

    async def test_after_llm_response_records_llm_call(self, store_and_hook):
        store, hook, ctx, _ = store_and_hook
        await hook.before_turn(ctx)  # sets trace_id

        from framework.core.types import LLMResponse
        response = LLMResponse(content="hello", finish_reason="stop")
        await hook.after_llm_response(ctx, response)

        records = [r for r in await store.list_by_session("conv:test_agent:a1b2")
                   if r.kind == OperationKind.LLM_CALL]
        assert len(records) == 1
        assert records[0].metadata["finish_reason"] == "stop"

    async def test_before_tool_execution_records_tool_batch(self, store_and_hook):
        store, hook, ctx, _ = store_and_hook
        await hook.before_turn(ctx)

        from framework.core.types import ToolCall
        tc = ToolCall(id="c1", tool_name="read", arguments={"path": "test.py"})
        await hook.before_tool_execution(ctx, [tc])

        records = [r for r in await store.list_by_session("conv:test_agent:a1b2")
                   if r.kind == OperationKind.TOOL_BATCH]
        assert len(records) == 1
        assert records[0].metadata["tool_names"] == ["read"]

    async def test_after_tool_execution_records_per_tool(self, store_and_hook):
        store, hook, ctx, _ = store_and_hook
        await hook.before_turn(ctx)

        from framework.core.tool_manager import ToolResult
        results = [
            ToolResult(tool_name="read", result="content"),
            ToolResult(tool_name="write", result="ok", error="permission denied"),
        ]
        await hook.after_tool_execution(ctx, results)

        records = [r for r in await store.list_by_session("conv:test_agent:a1b2")
                   if r.kind == OperationKind.TOOL_CALL]
        assert len(records) == 2
        assert records[0].metadata["tool_name"] == "read"
        assert records[0].error is None
        assert records[1].metadata["tool_name"] == "write"
        assert records[1].error == "permission denied"

    async def test_finally_turn_records_turn_end(self, store_and_hook):
        store, hook, ctx, _ = store_and_hook
        await hook.before_turn(ctx)

        from framework.core.emitter import AgentResult
        result = AgentResult(content="done", stop_reason="completed")
        await hook.finally_turn(ctx, result)

        records = [r for r in await store.list_by_session("conv:test_agent:a1b2")
                   if r.kind == OperationKind.TURN_END]
        assert len(records) == 1
        assert records[0].status == OperationStatus.COMPLETED
        assert records[0].metadata["stop_reason"] == "completed"

    async def test_finally_turn_records_error(self, store_and_hook):
        store, hook, ctx, _ = store_and_hook
        await hook.before_turn(ctx)

        from framework.core.emitter import AgentResult
        result = AgentResult(error="crash", stop_reason="error")
        await hook.finally_turn(ctx, result)

        records = [r for r in await store.list_by_session("conv:test_agent:a1b2")
                   if r.kind == OperationKind.TURN_END]
        assert len(records) == 1
        assert records[0].status == OperationStatus.FAILED
        assert records[0].error == "crash"

    async def test_disabled_hook_records_nothing(self, store_and_hook):
        store, hook, ctx, _ = store_and_hook
        hook._enabled = False
        await hook.before_turn(ctx)

        records = await store.list_by_session("conv:test_agent:a1b2")
        assert len(records) == 0


def _make_trace_context(session_id: str):
    from framework.core.agent import AgentContext
    from framework.core.tool_manager import InMemoryToolManager, ToolManagerConfig
    from framework.memory.history import MessageHistory
    from framework.runtime.enums import AgentKind, TurnPhase
    from framework.runtime.models import TurnIdentity
    from framework.runtime.services import AgentRuntime, AgentRuntimeServices
    from framework.agents.react.state import ReActTurnState

    state = ReActTurnState(
        identity=TurnIdentity(agent_id="test", session_id=session_id, turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    runtime = AgentRuntime(services=AgentRuntimeServices(), state=state)
    return AgentContext(
        system_prompt="test",
        history=MessageHistory(),
        tool_manager=InMemoryToolManager(config=ToolManagerConfig()),
        session_id=session_id,
        runtime=runtime,
    )
```

- [ ] **Step 5: Run trace hook tests**

```bash
pytest tests/unit/trace/test_hooks.py -v
```

Expected: 7 passed

- [ ] **Step 6: Commit**

```bash
git add framework/trace/hooks.py framework/trace/__init__.py framework/runtime/enums.py tests/unit/trace/test_hooks.py
git commit -m "feat(trace): add TraceCollectorHook — per-operation trace via lifecycle hooks"
```

---

### Task 5: `_ensure_invocation()` and output.md protocol in `AgentCommunicationService`

**Files:**
- Modify: `framework/multi_agent/communication.py`

- [ ] **Step 1: Add `_ensure_invocation()` method to `AgentCommunicationService`**

In `framework/multi_agent/communication.py`, add the method to the class (after `_resolve_target`):

```python
def _ensure_invocation(
    self,
    target_agent: str,
    conversation_id: str,
    invocation_id: str | None,
    target_kind: "AgentCommKind | None",
) -> tuple[str | None, "Path | None", "Path | None"]:
    """Ensure invocation_id and create trace/output dirs for subagent targets.

    Returns (invocation_id, trace_dir, output_path).  Returns (None, None, None)
    when target is not a subagent.
    """
    if target_kind != AgentCommKind.SUBAGENT:
        return invocation_id, None, None

    from uuid import uuid4

    # Generate or validate invocation_id
    if not invocation_id or str(invocation_id).lower() == "null":
        invocation_id = uuid4().hex[:8]
    else:
        # Check if existing trace directory exists for this invocation
        existing_session = self._session_strategy.format(
            conversation_id=conversation_id,
            agent_name=target_agent,
            invocation_id=invocation_id,
        )
        if self._runtime_dir is not None:
            trace_path = self._runtime_dir / "trace" / existing_session
            if not trace_path.exists():
                invocation_id = uuid4().hex[:8]

    session_id = self._session_strategy.format(
        conversation_id=conversation_id,
        agent_name=target_agent,
        invocation_id=invocation_id,
    )

    runtime_dir = self._runtime_dir or Path(".")
    trace_dir = runtime_dir / "trace" / session_id
    output_path = runtime_dir / "output" / session_id / "output.md"

    trace_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    return invocation_id, trace_dir, output_path
```

This method needs `self._runtime_dir` — add it to `__init__`:

```python
def __init__(
    self,
    # ... existing params ...
    runtime_dir: Path | None = None,   # NEW
) -> None:
    # ... existing assignments ...
    self._runtime_dir = runtime_dir   # NEW
```

- [ ] **Step 2: Inject output.md protocol into subagent system prompt**

In `_create_dynamic_subagent()`, after the fork context injection block, add:

```python
# ── Inject output.md protocol into system prompt ──
output_path = self._runtime_dir / "output" / session_id / "output.md" \
    if self._runtime_dir and session_id else None
if output_path is not None:
    output_protocol = (
        "\n\n---\n\n"
        "## Output Protocol\n\n"
        "Your task result MUST be written to this file:\n"
        f"  {output_path}\n\n"
        "- This file is your deliverable. What you say in conversation is transient.\n"
        "- Write your final answer, analysis, or implementation result here.\n"
        "- The system will notify your caller with this path when you finish.\n"
        "- Do NOT rely on communication tools for result delivery — write to this file."
    )
    system_prompt = system_prompt + output_protocol
```

- [ ] **Step 3: Call `_ensure_invocation()` in `_send()` method**

In the `_send()` method (where `_create_dynamic_subagent` is called), add the invocation handling before the existing logic:

Find the section where `invocation_id` is first used, and add:

```python
# Ensure invocation + trace/output paths for subagent targets
invocation_id, _trace_dir, _output_path = self._ensure_invocation(
    target_agent=target_agent,
    conversation_id=conversation_id,
    invocation_id=invocation_id,
    target_kind=resolved_kind,
)
```

- [ ] **Step 4: Verify no LSP errors**

```bash
python -c "from framework.multi_agent.communication import AgentCommunicationService"
```

Expected: clean import

- [ ] **Step 5: Run existing communication service tests**

```bash
pytest tests/unit/multi_agent/test_communication_service.py -v
```

Expected: all existing tests pass

- [ ] **Step 6: Commit**

```bash
git add framework/multi_agent/communication.py
git commit -m "feat(communication): add _ensure_invocation with trace/output path creation and output.md protocol injection"
```

---

### Task 6: Rewrite `SubagentAutoSendHook` as `FinallyTurnHook`

**Files:**
- Modify: `framework/hook/builtin/subagent_auto_send.py`
- Modify: `tests/unit/multi_agent/test_subagent_auto_send_hook.py`

- [ ] **Step 1: Rewrite `subagent_auto_send.py`**

Replace the entire file content. Key changes:
- Base class: `FinallyTurnHook` (was `AfterTurnHook`)
- No `send_to_agent` tool check
- No `_communicated` set tracking
- No `_already_sent_in_history` fallback
- Deterministic trace/output path derivation from `session_id`
- `_classify_stop()` for error/hint classification
- XML notification format with trace/output paths

```python
"""Subagent Auto-Send Hook — always-fire result notification for subagents.

Fires on FINALLY_TURN (guaranteed) — no communication tool check needed.
Subagents have no communication tools; this hook is the sole notification path.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from framework.hook.abc import FinallyTurnHook

if TYPE_CHECKING:
    from framework.core.agent import AgentContext
    from framework.core.emitter import AgentResult
    from framework.multi_agent.bus import AgentMessageBus

logger = logging.getLogger(__name__)


class SubagentAutoSendHook(FinallyTurnHook):
    """Always-fire result notification for subagents.

    Fires on FINALLY_TURN (success, error, cancel, max_iterations — always).
    Derives trace_dir and output_path deterministically from session_id.
    Sends XML notification to parent inbox.
    """

    @property
    def name(self) -> str:
        return "subagent_auto_send_hook"

    _THINK_PAIRED_RE = re.compile(
        r"<\s*(?:think|reasoning|reflection)\b[^>]*(?:>|\n)"
        r"(.*?)</\s*(?:think|reasoning|reflection)\b[^>]*(?:>|\n)",
        re.IGNORECASE | re.DOTALL,
    )
    _THINK_TAG_RE = re.compile(
        r"<\s*/?\s*(?:think|reasoning|reflection)\b[^>]*>?",
        re.IGNORECASE,
    )

    def __init__(
        self,
        agent_bus: "AgentMessageBus | None" = None,
        self_name: str = "",
        parent_name: str = "main",
        runtime_dir: "Path | None" = None,
    ) -> None:
        self._agent_bus = agent_bus
        self._self_name = self_name
        self._parent_name = parent_name
        self._runtime_dir = runtime_dir or Path(".")

    # -- FINALLY_TURN (always fires) ------------------------------------------

    async def finally_turn(self, ctx: "AgentContext", result: "AgentResult | None") -> None:
        if self._agent_bus is None:
            return

        session_id = ctx.session_id or ""

        # 1. Derive artifact paths from session_id (deterministic)
        trace_dir = self._runtime_dir / "trace" / session_id
        output_path = self._runtime_dir / "output" / session_id / "output.md"

        # 2. Check output.md status
        output_status = "written" if output_path.exists() else "missing"

        # 3. Determine stop condition
        stop_reason = getattr(result, "stop_reason", "error") if result else "error"
        error = getattr(result, "error", None) if result else "subagent crashed"

        is_normal, hint = self._classify_stop(stop_reason, output_status, error)

        # 4. Truncate last assistant output
        content = getattr(result, "content", "") if result else ""
        summary = self._truncate_content(content, max_chars=1500)

        # 5. Get invocation_id from session_meta
        invocation_id = ""
        if ctx.session_meta is not None:
            invocation_id = getattr(ctx.session_meta, "invocation_id", "") or ""

        # 6. Build XML notification
        xml = self._build_xml(
            agent_name=self._self_name,
            invocation_id=invocation_id,
            status="completed" if is_normal else "incomplete",
            stop_reason=str(stop_reason) if stop_reason else "unknown",
            is_normal=is_normal,
            error=error or "",
            hint=hint,
            summary=summary,
            trace_dir_rel=f"trace/{session_id}/operations.jsonl",
            output_path_rel=f"output/{session_id}/output.md",
            output_status=output_status,
        )

        # 7. Send to parent inbox
        await self._notify_parent(ctx, session_id, xml)

    # -- stop classification --------------------------------------------------

    @staticmethod
    def _classify_stop(
        stop_reason: Any, output_status: str, error: Any,
    ) -> tuple[bool, str]:
        reason_str = str(stop_reason) if stop_reason else ""
        if error:
            return False, (
                "Subagent crashed with an error. "
                "You may want to restart with a new invocation_id."
            )
        if reason_str == "max_iterations":
            return False, (
                "Subagent hit step limit — task may be incomplete. "
                "Continue with same invocation_id to resume."
            )
        if output_status == "missing":
            return False, (
                "Subagent finished but output.md was not written. "
                "You may want to re-run this task."
            )
        return True, ""

    # -- XML builder ----------------------------------------------------------

    @staticmethod
    def _build_xml(
        *,
        agent_name: str,
        invocation_id: str,
        status: str,
        stop_reason: str,
        is_normal: bool,
        error: str,
        hint: str,
        summary: str,
        trace_dir_rel: str,
        output_path_rel: str,
        output_status: str,
    ) -> str:
        from framework.utils.xml import xml_text

        return (
            "<subagent_notification>\n"
            f"  <agent>{xml_text(agent_name)}</agent>\n"
            f"  <invocation_id>{xml_text(invocation_id)}</invocation_id>\n"
            f"  <status>{xml_text(status)}</status>\n"
            f"  <stop_reason>{xml_text(stop_reason)}</stop_reason>\n"
            f"  <is_normal>{str(is_normal).lower()}</is_normal>\n"
            f"  <error>{xml_text(error)}</error>\n"
            f"  <hint>{xml_text(hint)}</hint>\n"
            f"  <summary>{xml_text(summary)}</summary>\n"
            f"  <artifacts>\n"
            f"    <trace>{xml_text(trace_dir_rel)}</trace>\n"
            f"    <output>{xml_text(output_path_rel)}</output>\n"
            f"    <output_status>{xml_text(output_status)}</output_status>\n"
            f"  </artifacts>\n"
            f"</subagent_notification>"
        )

    # -- notification ---------------------------------------------------------

    async def _notify_parent(
        self, ctx: "AgentContext", session_id: str, xml: str,
    ) -> None:
        """Send XML notification to parent agent's inbox."""
        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.envelope import AgentMessageEnvelope
        from framework.multi_agent.session_id import DefaultSessionIdStrategy

        strategy = DefaultSessionIdStrategy(main_agent_name=self._parent_name)
        try:
            parts = strategy.parse(session_id)
        except ValueError:
            logger.warning(
                "SubagentAutoSendHook: cannot parse session_id %s", session_id,
            )
            return

        conversation_id = parts.conversation_id
        invocation_id = parts.invocation_id or ""
        inbox_key = strategy.format(
            conversation_id=conversation_id, agent_name=self._parent_name,
        )

        # Strip think tags from the XML summary (defense in depth)
        from framework.hook.builtin.inbox_flush import InboxFlushHook
        xml = InboxFlushHook._sanitize_content(xml)

        envelope = AgentMessageEnvelope(
            payload={
                "content": xml,
                "message_type": "agent_result",
                "metadata": {"agent_type": self._self_name, "format": "xml"},
            },
            source=AgentAddress(name=self._self_name),
            target=AgentAddress(name=self._parent_name),
            message_type="agent_result",
            conversation_id=conversation_id,
            agent_session_id=inbox_key,
            invocation_id=invocation_id,
        )

        try:
            await self._agent_bus.send(inbox_key, envelope)
            logger.info(
                "SubagentAutoSendHook: notified parent %s (agent=%s, session=%s)",
                self._parent_name, self._self_name, session_id,
            )
        except Exception:
            logger.exception(
                "SubagentAutoSendHook: failed to notify parent %s",
                self._parent_name,
            )

    # -- content helpers ------------------------------------------------------

    @staticmethod
    def _truncate_content(content: str, max_chars: int = 1500) -> str:
        content = SubagentAutoSendHook._THINK_PAIRED_RE.sub("", content)
        content = SubagentAutoSendHook._THINK_TAG_RE.sub("", content)
        if len(content) <= max_chars:
            return content
        return content[:max_chars] + f"\n[...truncated, {len(content) - max_chars} more chars]"
```

- [ ] **Step 2: Update test file to match new behavior**

Modify `tests/unit/multi_agent/test_subagent_auto_send_hook.py` to use `FinallyTurnHook` interface and cover new scenarios.

Key test cases:
1. `finally_turn` with completed result → XML sent with output_status=written
2. `finally_turn` with error result → XML with is_normal=false, hint about crash
3. `finally_turn` with max_iterations → XML with hint about resuming
4. `finally_turn` with completed but no output.md → XML with hint about missing output
5. `finally_turn` with no agent_bus → no-op
6. `send_to_agent` tool check is NOT performed (verification by absence)

Rewrite the test file:

```python
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from framework.core.agent import AgentContext, AgentSessionMeta
from framework.core.emitter import AgentResult
from framework.core.tool_manager import InMemoryToolManager, ToolManagerConfig
from framework.memory.history import MessageHistory
from framework.multi_agent.bus import LocalAgentMessageBus
from framework.multi_agent.comm_kind import AgentCommKind
from framework.multi_agent.inbox.consumer import InboxConsumer
from framework.multi_agent.inbox.producer import InboxProducer
from framework.multi_agent.inbox.server_local import LocalFileInboxServer
from framework.hook.builtin.subagent_auto_send import SubagentAutoSendHook


def _make_context(session_id: str, agent_name: str = "worker") -> AgentContext:
    return AgentContext(
        system_prompt="test",
        history=MessageHistory(),
        tool_manager=InMemoryToolManager(config=ToolManagerConfig()),
        session_id=session_id,
        session_meta=AgentSessionMeta(
            conversation_id="conv123",
            agent_name=agent_name,
            comm_kind=AgentCommKind.SUBAGENT,
            invocation_id="a1b2c3d4",
        ),
    )


def _make_bus(tmpdir: Path) -> LocalAgentMessageBus:
    server = LocalFileInboxServer(workspace=tmpdir / "inbox")
    producer = InboxProducer(server=server)
    consumer = InboxConsumer(server=server)
    return LocalAgentMessageBus(producer=producer, consumer=consumer)


class TestSubagentAutoSendHookFinallyTurn:
    async def test_completed_with_output_sends_xml(self, tmp_path):
        # Create output.md to simulate subagent writing it
        output_dir = tmp_path / "output" / "conv123:worker:a1b2c3d4"
        output_dir.mkdir(parents=True)
        (output_dir / "output.md").write_text("fixed the bug")

        bus = _make_bus(tmp_path)
        hook = SubagentAutoSendHook(
            agent_bus=bus,
            self_name="worker",
            parent_name="main",
            runtime_dir=tmp_path,
        )
        ctx = _make_context("conv123:worker:a1b2c3d4")
        result = AgentResult(content="fixed the bug in auth.py", stop_reason="completed")

        await hook.finally_turn(ctx, result)

        # Verify envelope sent to parent inbox
        envelopes = await bus.poll(
            "conv123:main",  # parent inbox key
            limit=10,
        )
        assert len(envelopes) >= 1
        content = envelopes[0].payload.get("content", "")
        assert "<subagent_notification>" in content
        assert "<agent>worker</agent>" in content
        assert "output.md" in content

    async def test_error_crash_sends_hint(self, tmp_path):
        bus = _make_bus(tmp_path)
        hook = SubagentAutoSendHook(
            agent_bus=bus, self_name="worker", parent_name="main",
            runtime_dir=tmp_path,
        )
        ctx = _make_context("conv123:worker:a1b2c3d4")
        result = AgentResult(error="something broke", stop_reason="error")

        await hook.finally_turn(ctx, result)

        envelopes = await bus.poll("conv123:main", limit=10)
        content = envelopes[0].payload.get("content", "")
        assert "<is_normal>false</is_normal>" in content
        assert "crashed" in content.lower()

    async def test_max_iterations_sends_hint(self, tmp_path):
        bus = _make_bus(tmp_path)
        hook = SubagentAutoSendHook(
            agent_bus=bus, self_name="worker", parent_name="main",
            runtime_dir=tmp_path,
        )
        ctx = _make_context("conv123:worker:a1b2c3d4")
        result = AgentResult(content="partial work...", stop_reason="max_iterations")

        await hook.finally_turn(ctx, result)

        envelopes = await bus.poll("conv123:main", limit=10)
        content = envelopes[0].payload.get("content", "")
        assert "<is_normal>false</is_normal>" in content
        assert "step limit" in content.lower()

    async def test_no_agent_bus_noop(self, tmp_path):
        hook = SubagentAutoSendHook(
            agent_bus=None, self_name="worker", parent_name="main",
        )
        ctx = _make_context("conv123:worker:a1b2c3d4")
        result = AgentResult(content="done", stop_reason="completed")
        # Should not raise
        await hook.finally_turn(ctx, result)

    async def test_no_result_sends_error_notification(self, tmp_path):
        bus = _make_bus(tmp_path)
        hook = SubagentAutoSendHook(
            agent_bus=bus, self_name="worker", parent_name="main",
            runtime_dir=tmp_path,
        )
        ctx = _make_context("conv123:worker:a1b2c3d4")

        await hook.finally_turn(ctx, None)

        envelopes = await bus.poll("conv123:main", limit=10)
        content = envelopes[0].payload.get("content", "")
        assert "<is_normal>false</is_normal>" in content
        assert "crashed" in content.lower()

    async def test_output_status_missing_when_no_file(self, tmp_path):
        bus = _make_bus(tmp_path)
        hook = SubagentAutoSendHook(
            agent_bus=bus, self_name="worker", parent_name="main",
            runtime_dir=tmp_path,
        )
        ctx = _make_context("conv123:worker:a1b2c3d4")
        result = AgentResult(content="done", stop_reason="completed")
        # No output.md created

        await hook.finally_turn(ctx, result)

        envelopes = await bus.poll("conv123:main", limit=10)
        content = envelopes[0].payload.get("content", "")
        assert "<output_status>missing</output_status>" in content
        assert "<is_normal>false</is_normal>" in content
```

- [ ] **Step 3: Run rewritten tests**

```bash
pytest tests/unit/multi_agent/test_subagent_auto_send_hook.py -v
```

Expected: 6 passed

- [ ] **Step 4: Commit**

```bash
git add framework/hook/builtin/subagent_auto_send.py tests/unit/multi_agent/test_subagent_auto_send_hook.py
git commit -m "feat(hook): rewrite SubagentAutoSendHook as FinallyTurnHook with XML notification and deterministic path derivation"
```

---

### Task 7: Remove communication tools from subagent

**Files:**
- Modify: `framework/multi_agent/communication.py`

- [ ] **Step 1: In `_build_subagent_tool_manager()`, remove communication tool registration**

In `framework/multi_agent/communication.py`, find `_build_subagent_tool_manager()`. Remove the section that registers `SendToAgentTool` for subagents. Keep the method but skip communication tool registration:

```python
async def _build_subagent_tool_manager(
    self, template: "AgentTemplate", agent_name: str,
    parent_name: str = "main",
):
    """Build the subagent tool manager from template configuration.

    Subagents get standard + MCP tools only. No communication tools.
    Communication is handled automatically by SubagentAutoSendHook.
    """
    from framework.core.tool_manager import InMemoryToolManager, ToolManagerConfig
    from framework.tools.presets import get_preset_tools

    subagent_tm = InMemoryToolManager(config=ToolManagerConfig())

    # Register standard tools based on template.tool_preset
    # (no communication tools — SubagentAutoSendHook handles that)
    if hasattr(template, "tool_preset") and template.tool_preset:
        preset_tools = get_preset_tools(template.tool_preset)
        for tool in preset_tools:
            subagent_tm.register(tool)
    elif hasattr(template, "standard_tools"):
        # backward compat
        for tool_def in template.standard_tools:
            if hasattr(tool_def, "enabled") and not tool_def.enabled:
                continue
            # register standard tools here

    return subagent_tm
```

The exact code depends on the current implementation of `_build_subagent_tool_manager`. The key change is: **do not register `SendToAgentTool` or `CommunicationTargetStore`**.

- [ ] **Step 2: Verify import is clean**

```bash
python -c "from framework.multi_agent.communication import AgentCommunicationService"
```

- [ ] **Step 3: Commit**

```bash
git add framework/multi_agent/communication.py
git commit -m "feat(subagent): remove communication tool registration from subagent tool manager"
```

---

### Task 8: LRU SubagentPool

**Files:**
- Create: `framework/multi_agent/pool_reuse.py`
- Modify: `framework/multi_agent/__init__.py`
- Modify: `framework/multi_agent/communication.py`
- Create: `tests/unit/multi_agent/test_subagent_pool.py`

- [ ] **Step 1: Create `framework/multi_agent/pool_reuse.py`**

```python
"""SubagentPool — LRU instance reuse for dynamic subagents.

Framework-layer abstraction.  ``send_to_agent`` to a subagent type routes
through ``SubagentPool.acquire()`` which returns or creates an instance.
Session isolation via ``session_id`` ensures no cross-task contamination.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from framework.multi_agent.registry import AgentInstance

logger = logging.getLogger(__name__)


@dataclass
class _PoolEntry:
    instance: "AgentInstance"
    created_at: float
    last_used: float


class SubagentPool:
    """LRU pool for subagent instance reuse.

    ``acquire(agent_type, factory)`` returns an existing instance or creates
    one via ``factory()``.  Idle instances are evicted after ``ttl_seconds``.
    """

    def __init__(
        self,
        max_size: int = 8,
        ttl_seconds: float = 1800.0,
        eviction_check_interval: float = 120.0,
    ) -> None:
        self._pool: dict[str, _PoolEntry] = {}   # key = agent_type
        self._lru_order: list[str] = []
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._eviction_interval = eviction_check_interval
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._closed = False

    # -- public API -----------------------------------------------------------

    async def acquire(
        self,
        agent_type: str,
        factory: Callable[[], Awaitable["AgentInstance"]],
    ) -> "AgentInstance":
        """Get or create a subagent instance for ``agent_type``.

        ``factory`` is called only on cache miss.
        """
        async with self._lock:
            if agent_type in self._pool:
                entry = self._pool[agent_type]
                entry.last_used = time.monotonic()
                self._touch_lru(agent_type)
                logger.debug("SubagentPool: hit %s", agent_type)
                return entry.instance

            # Evict oldest if full
            while len(self._pool) >= self._max_size:
                oldest = self._lru_order[0]
                await self._evict(oldest)

            logger.info("SubagentPool: creating %s (miss)", agent_type)
            instance = await factory()
            self._pool[agent_type] = _PoolEntry(
                instance=instance,
                created_at=time.monotonic(),
                last_used=time.monotonic(),
            )
            self._lru_order.append(agent_type)
            return instance

    async def evict(self, agent_type: str) -> None:
        """Evict a specific agent type from the pool."""
        async with self._lock:
            await self._evict(agent_type)

    async def start_cleanup(self) -> None:
        """Start background TTL eviction task."""
        if self._cleanup_task is not None:
            return
        self._cleanup_task = asyncio.create_task(self._cleanup_stale_loop())

    async def close(self) -> None:
        """Shut down pool: cancel cleanup, evict all."""
        self._closed = True
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        async with self._lock:
            for agent_type in list(self._pool.keys()):
                await self._evict(agent_type)

    @property
    def size(self) -> int:
        return len(self._pool)

    @property
    def cached_types(self) -> list[str]:
        return list(self._pool.keys())

    # -- internal -------------------------------------------------------------

    def _touch_lru(self, agent_type: str) -> None:
        """Move agent_type to end of LRU order."""
        if agent_type in self._lru_order:
            self._lru_order.remove(agent_type)
        self._lru_order.append(agent_type)

    async def _evict(self, agent_type: str) -> None:
        """Internal eviction without lock (caller holds lock)."""
        entry = self._pool.pop(agent_type, None)
        if agent_type in self._lru_order:
            self._lru_order.remove(agent_type)
        if entry is not None:
            try:
                # Gracefully shut down the agent instance
                instance = entry.instance
                if hasattr(instance, "pipeline") and instance.pipeline is not None:
                    await instance.pipeline.shutdown()
            except Exception:
                logger.exception("SubagentPool: error shutting down %s", agent_type)

    async def _cleanup_stale_loop(self) -> None:
        """Periodic TTL-based eviction of idle instances."""
        while not self._closed:
            try:
                await asyncio.sleep(self._eviction_interval)
                await self._cleanup_stale()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("SubagentPool: cleanup_stale loop error")

    async def _cleanup_stale(self) -> None:
        now = time.monotonic()
        async with self._lock:
            stale = [
                t for t in self._lru_order
                if now - self._pool[t].last_used > self._ttl
            ]
        for agent_type in stale:
            logger.info("SubagentPool: evicting stale %s (idle %.0fs)", agent_type, now - self._pool[agent_type].last_used)
            async with self._lock:
                await self._evict(agent_type)
```

- [ ] **Step 2: Export `SubagentPool` from `framework/multi_agent/__init__.py`**

Add import and export:

```python
from framework.multi_agent.pool_reuse import SubagentPool
```

Add to `__all__`:
```python
"SubagentPool",
```

- [ ] **Step 3: Integrate `SubagentPool` into `AgentCommunicationService`**

In `framework/multi_agent/communication.py`:

Add `subagent_pool` parameter to `AgentCommunicationService.__init__`:

```python
def __init__(
    self,
    # ... existing params ...
    subagent_pool: "SubagentPool | None" = None,   # NEW
) -> None:
    # ... existing assignments ...
    self._subagent_pool = subagent_pool   # NEW
```

In `_create_dynamic_subagent()`, replace direct `register_resident` with pool acquire:

```python
# ── Acquire from pool (or register new) ──
from framework.multi_agent.descriptor import AgentDescriptor, AgentLLMConfig

descriptor = AgentDescriptor(
    address=AgentAddress(name=name),
    llm_config=AgentLLMConfig(
        model=self._pool_llm_model or "",
        temperature=self._pool_llm_temperature,
        max_tokens=self._pool_llm_max_tokens,
    ),
    system_prompt_template=system_prompt,
    max_iterations=template.max_steps,
    execution_strategy="react",
    context_strategy="persistent",
    safety_policy=self._safety,
    comm_kind=AgentCommKind.SUBAGENT,
)

if self._subagent_pool is not None:
    async def _factory():
        from framework.pipeline.adapters import NullOutputAdapter
        await self._pool.register_resident(
            descriptor,
            context_manager=subagent_ctx,
            tool_manager=subagent_tm,
            skill_manager=subagent_sm,
            output_adapter=NullOutputAdapter(),
        )
        self._pool._mark_dynamic(name)
        self._wire_subagent_hooks(name, parent_name=parent_name)
        return self._pool.get(name)

    await self._subagent_pool.acquire(name, _factory)
else:
    # Fallback: create without pool
    from framework.pipeline.adapters import NullOutputAdapter
    await self._pool.register_resident(
        descriptor,
        context_manager=subagent_ctx,
        tool_manager=subagent_tm,
        skill_manager=subagent_sm,
        output_adapter=NullOutputAdapter(),
    )
    self._pool._mark_dynamic(name)
    self._wire_subagent_hooks(name, parent_name=parent_name)
```

Note: when using the pool, `_factory` is only called on cache miss. On hit, `acquire` returns the existing instance directly. The hooks and registration happen inside `_factory` (only on miss).

- [ ] **Step 4: Create `tests/unit/multi_agent/test_subagent_pool.py`**

```python
from __future__ import annotations

import asyncio

import pytest

from framework.multi_agent.pool_reuse import SubagentPool


class _FakeAgentInstance:
    def __init__(self, name: str):
        self.name = name
        self.pipeline = None


class TestSubagentPool:
    async def test_acquire_creates_on_miss(self):
        pool = SubagentPool(max_size=4)
        call_count = 0

        async def factory():
            nonlocal call_count
            call_count += 1
            return _FakeAgentInstance("worker")

        inst = await pool.acquire("worker", factory)
        assert inst.name == "worker"
        assert call_count == 1
        assert pool.size == 1

    async def test_acquire_returns_cached_on_hit(self):
        pool = SubagentPool(max_size=4)
        call_count = 0

        async def factory():
            nonlocal call_count
            call_count += 1
            return _FakeAgentInstance("worker")

        inst1 = await pool.acquire("worker", factory)
        inst2 = await pool.acquire("worker", factory)
        assert inst1 is inst2
        assert call_count == 1  # factory called once
        assert pool.size == 1

    async def test_lru_eviction_on_full_pool(self):
        pool = SubagentPool(max_size=2)
        calls: list[str] = []

        async def make_factory(name: str):
            async def factory():
                calls.append(name)
                return _FakeAgentInstance(name)
            return factory

        await pool.acquire("a", await make_factory("a"))
        await pool.acquire("b", await make_factory("b"))
        # Pool is full (2/2), "a" is oldest
        await pool.acquire("c", await make_factory("c"))
        # "a" should be evicted
        assert pool.size <= 2
        assert "c" in pool.cached_types
        # "a" evicted → re-acquire triggers factory again
        calls.clear()
        await pool.acquire("a", await make_factory("a"))
        assert "a" in calls  # factory called for re-creation

    async def test_evict_removes_and_calls_shutdown(self):
        pool = SubagentPool(max_size=4)

        class _ShutdownAgent:
            def __init__(self):
                self.shutdown_called = False
                self.pipeline = None

        agent = _ShutdownAgent()
        pool._pool["test"] = pool._PoolEntry(  # type: ignore
            instance=agent, created_at=0, last_used=0,
        )
        pool._lru_order.append("test")

        await pool.evict("test")
        assert pool.size == 0

    async def test_close_evicts_all(self):
        pool = SubagentPool(max_size=4)
        async def factory():
            return _FakeAgentInstance("x")
        await pool.acquire("a", factory)
        await pool.acquire("b", factory)
        await pool.close()
        assert pool.size == 0

    async def test_multiple_types_isolated(self):
        pool = SubagentPool(max_size=4)
        async def factory_a():
            return _FakeAgentInstance("worker")
        async def factory_b():
            return _FakeAgentInstance("scout")

        w = await pool.acquire("worker", factory_a)
        s = await pool.acquire("scout", factory_b)
        assert w.name == "worker"
        assert s.name == "scout"
        assert w is not s
        assert pool.size == 2
```

- [ ] **Step 5: Run SubagentPool tests**

```bash
pytest tests/unit/multi_agent/test_subagent_pool.py -v
```

Expected: 6 passed

- [ ] **Step 6: Commit**

```bash
git add framework/multi_agent/pool_reuse.py framework/multi_agent/__init__.py framework/multi_agent/communication.py tests/unit/multi_agent/test_subagent_pool.py
git commit -m "feat(pool): add LRU SubagentPool for dynamic subagent instance reuse"
```

---

### Task 9: Integration test — end-to-end subagent lifecycle

**Files:**
- Create: `tests/unit/multi_agent/test_subagent_v2_integration.py`

- [ ] **Step 1: Write integration test**

```python
from __future__ import annotations

"""Integration test: subagent lifecycle with FINALLY_TURN hook notification."""

import tempfile
from pathlib import Path

import pytest

from framework.hook.builtin.subagent_auto_send import SubagentAutoSendHook
from framework.multi_agent.bus import LocalAgentMessageBus
from framework.multi_agent.inbox.consumer import InboxConsumer
from framework.multi_agent.inbox.producer import InboxProducer
from framework.multi_agent.inbox.server_local import LocalFileInboxServer
from framework.core.agent import AgentContext, AgentSessionMeta
from framework.core.emitter import AgentResult
from framework.core.tool_manager import InMemoryToolManager, ToolManagerConfig
from framework.memory.history import MessageHistory
from framework.multi_agent.comm_kind import AgentCommKind
from framework.trace import JsonFileTraceStore, TraceCollectorHook, OperationKind


class TestSubagentV2Integration:
    """End-to-end: subagent finishes → FINALLY_TURN → parent receives XML."""

    async def test_full_lifecycle_notification(self, tmp_path):
        # Setup
        bus = _make_bus(tmp_path)
        store = JsonFileTraceStore(tmp_path / "trace")
        trace_hook = TraceCollectorHook(store)
        auto_hook = SubagentAutoSendHook(
            agent_bus=bus, self_name="worker", parent_name="main",
            runtime_dir=tmp_path,
        )

        # Create output.md to simulate subagent writing it
        output_path = tmp_path / "output" / "conv123:worker:a1b2" / "output.md"
        output_path.parent.mkdir(parents=True)
        output_path.write_text("# Fix: SQL injection in auth.py\n\nParameterized queries added.")

        ctx = _make_context("conv123:worker:a1b2", "worker")
        result = AgentResult(content="I fixed the SQL injection bug in auth.py", stop_reason="completed")

        # Simulate before_turn + execution + finally_turn
        await trace_hook.before_turn(ctx)
        await auto_hook.finally_turn(ctx, result)

        # Verify trace recorded
        records = await store.list_by_session("conv123:worker:a1b2")
        turn_starts = [r for r in records if r.kind == OperationKind.TURN_START]
        assert len(turn_starts) >= 1

        # Verify parent received notification
        envelopes = await bus.poll("conv123:main", limit=10)
        assert len(envelopes) >= 1
        content = envelopes[0].payload.get("content", "")
        assert "<subagent_notification>" in content
        assert "<agent>worker</agent>" in content
        assert "<output_status>written</output_status>" in content
        assert "trace/conv123:worker:a1b2" in content
        assert "output/conv123:worker:a1b2" in content

    async def test_crash_sends_error_notification(self, tmp_path):
        bus = _make_bus(tmp_path)
        auto_hook = SubagentAutoSendHook(
            agent_bus=bus, self_name="worker", parent_name="main",
            runtime_dir=tmp_path,
        )
        ctx = _make_context("conv123:worker:a1b2", "worker")
        result = AgentResult(error="something broke", stop_reason="error")

        await auto_hook.finally_turn(ctx, result)

        envelopes = await bus.poll("conv123:main", limit=10)
        content = envelopes[0].payload.get("content", "")
        assert "<is_normal>false</is_normal>" in content
        assert "crashed" in content.lower()

    async def test_trace_collector_records_error_turn_end(self, tmp_path):
        store = JsonFileTraceStore(tmp_path / "trace")
        trace_hook = TraceCollectorHook(store)
        ctx = _make_context("conv123:worker:a1b2", "worker")
        result = AgentResult(error="timeout", stop_reason="error")

        await trace_hook.before_turn(ctx)
        await trace_hook.finally_turn(ctx, result)

        records = [r for r in await store.list_by_session("conv123:worker:a1b2")
                   if r.kind == OperationKind.TURN_END]
        assert len(records) == 1
        assert records[0].status == "failed"  # OperationStatus.FAILED
        assert records[0].error == "timeout"


def _make_context(session_id: str, agent_name: str) -> AgentContext:
    return AgentContext(
        system_prompt="test",
        history=MessageHistory(),
        tool_manager=InMemoryToolManager(config=ToolManagerConfig()),
        session_id=session_id,
        session_meta=AgentSessionMeta(
            conversation_id="conv123",
            agent_name=agent_name,
            comm_kind=AgentCommKind.SUBAGENT,
            invocation_id="a1b2",
        ),
    )


def _make_bus(tmpdir: Path) -> LocalAgentMessageBus:
    server = LocalFileInboxServer(workspace=tmpdir / "inbox")
    producer = InboxProducer(server=server)
    consumer = InboxConsumer(server=server)
    return LocalAgentMessageBus(producer=producer, consumer=consumer)
```

- [ ] **Step 2: Run integration tests**

```bash
pytest tests/unit/multi_agent/test_subagent_v2_integration.py -v
```

Expected: 3 passed

- [ ] **Step 3: Run full multi_agent test suite to verify no regressions**

```bash
pytest tests/unit/multi_agent/ -v --timeout=120
```

Expected: all existing tests pass

- [ ] **Step 4: Run full unit test suite**

```bash
pytest tests/unit/ -v --timeout=120
```

Expected: all tests pass

- [ ] **Step 5: Commit**

```bash
git add tests/unit/multi_agent/test_subagent_v2_integration.py
git commit -m "test: add end-to-end integration test for subagent v2 lifecycle"
```

---

## Summary of All Changes

| Task | Files Created | Files Modified |
|------|--------------|----------------|
| 1. FINALLY_TURN HookPoint | `tests/unit/hook/test_finally_turn.py` | `hook/abc.py`, `hook/runner.py`, `hook/__init__.py` |
| 2. Wire in ReActAgent | — | `agents/react/agent.py` |
| 3. Trace types + store | `trace/__init__.py`, `trace/types.py`, `trace/store.py`, `tests/unit/trace/__init__.py`, `tests/unit/trace/test_store.py` | `runtime/enums.py` (OperationKind) |
| 4. TraceCollectorHook | `trace/hooks.py`, `tests/unit/trace/test_hooks.py` | `trace/__init__.py`, `runtime/enums.py` (TurnCustomKey) |
| 5. _ensure_invocation + output.md | — | `multi_agent/communication.py` |
| 6. SubagentAutoSendHook rewrite | — | `hook/builtin/subagent_auto_send.py`, `tests/unit/multi_agent/test_subagent_auto_send_hook.py` |
| 7. Remove comm tools | — | `multi_agent/communication.py` |
| 8. SubagentPool | `multi_agent/pool_reuse.py`, `tests/unit/multi_agent/test_subagent_pool.py` | `multi_agent/__init__.py`, `multi_agent/communication.py` |
| 9. Integration test | `tests/unit/multi_agent/test_subagent_v2_integration.py` | — |

**Execution order**: Tasks 1→2→3→4→5→6→7→8→9. Tasks 1-2 are foundational. Tasks 3-5 are independent. Tasks 6-7 depend on 1-2 and 5. Task 8 is independent. Task 9 validates everything together.
