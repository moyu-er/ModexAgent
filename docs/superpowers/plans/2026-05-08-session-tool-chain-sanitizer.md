# Session Tool-Chain Sanitizer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a pluggable sanitizer that removes stale invalid assistant/tool chains from persisted session memory while preserving the active last assistant tool-call tail.

**Architecture:** Implement a pure in-memory sanitizer in `framework/memory/compression/tool_chain_sanitizer.py`, then wire it into governance, compression, and lifecycle. Compression will run sanitizer before keep planning and will distinguish invalid physical drops from legal pruned archive candidates.

**Tech Stack:** Python 3.11, pytest, dataclasses, StrEnum, existing `framework.memory` session/compression/governance abstractions.

---

## File Structure

- Create: `framework/memory/compression/tool_chain_sanitizer.py`
  - Owns sanitizer enums, typed result dataclasses, protocol, and default implementation.
  - Performs no storage I/O.
- Modify: `framework/memory/context_governance.py`
  - Reuse sanitizer in `ToolChainRepairGovernance` and `FinalContextLegalityGovernance` for model-visible cleanup.
- Modify: `framework/memory/core/models.py`
  - Extend `CompressionPlan` with sanitizer cleanup fields.
- Modify: `framework/memory/compression/policies.py`
  - Inject sanitizer into `DefaultMemoryCompressionCoordinator`.
  - Run sanitizer before compaction/retention/keep planning.
  - Build cleanup-only plans for invalid physical drops when an active tail exists.
- Modify: `framework/memory/lifecycle.py`
  - Use sanitizer persistent-session analysis for active tail detection.
- Modify: `framework/memory/__init__.py`
  - Export sanitizer public interfaces.
- Create: `tests/unit/memory/test_tool_chain_sanitizer.py`
  - Focused sanitizer tests.
- Create: `tests/unit/memory/test_context_governance.py`
  - Governance model-visible tests if the file does not already exist.
- Modify: `tests/unit/memory/test_compression_policies.py`
  - Compression integration tests.
- Modify: `tests/unit/memory/test_lifecycle.py`
  - Lifecycle open-tail behavior tests.

## Task 1: Sanitizer Contract And Failing Unit Tests

**Files:**
- Create: `tests/unit/memory/test_tool_chain_sanitizer.py`
- Create later in Task 2: `framework/memory/compression/tool_chain_sanitizer.py`

- [ ] **Step 1: Write failing sanitizer tests**

Create `tests/unit/memory/test_tool_chain_sanitizer.py` with:

```python
from __future__ import annotations

from framework.core.types import MessageRole
from framework.memory.compression.tool_chain_sanitizer import (
    DefaultSessionToolChainSanitizer,
    ToolChainSanitizationMode,
    ToolChainSanitizationReason,
)


def _assistant_tool_call(*call_ids: str) -> dict:
    return {
        "role": str(MessageRole.ASSISTANT),
        "content": "",
        "tool_calls": [
            {"id": call_id, "function": {"name": f"tool_{call_id}"}}
            for call_id in call_ids
        ],
    }


def _tool(call_id: str, content: str = "result") -> dict:
    return {
        "role": str(MessageRole.TOOL),
        "tool_call_id": call_id,
        "content": content,
    }


def test_persistent_mode_removes_orphan_tool_result() -> None:
    messages = [
        {"role": str(MessageRole.USER), "content": "hi"},
        _tool("missing"),
        {"role": str(MessageRole.ASSISTANT), "content": "done"},
    ]

    result = DefaultSessionToolChainSanitizer().sanitize(
        messages,
        mode=ToolChainSanitizationMode.PERSISTENT_SESSION,
    )

    assert [msg["role"] for msg in result.messages] == [
        str(MessageRole.USER),
        str(MessageRole.ASSISTANT),
    ]
    assert result.removed_messages == [_tool("missing")]
    assert result.removed_indices == {1}
    assert [issue.reason for issue in result.issues] == [
        ToolChainSanitizationReason.ORPHAN_TOOL_RESULT,
    ]
    assert result.has_open_tail is False


def test_persistent_mode_removes_stale_incomplete_non_tail_assistant_and_partial_tool() -> None:
    messages = [
        {"role": str(MessageRole.USER), "content": "start"},
        _assistant_tool_call("a", "b"),
        _tool("a", "partial"),
        {"role": str(MessageRole.ASSISTANT), "content": "continued without b"},
        _assistant_tool_call("c"),
        _tool("c", "complete"),
    ]

    result = DefaultSessionToolChainSanitizer().sanitize(
        messages,
        mode=ToolChainSanitizationMode.PERSISTENT_SESSION,
    )

    assert result.messages == [
        {"role": str(MessageRole.USER), "content": "start"},
        {"role": str(MessageRole.ASSISTANT), "content": "continued without b"},
        _assistant_tool_call("c"),
        _tool("c", "complete"),
    ]
    assert result.removed_indices == {1, 2}
    assert [issue.reason for issue in result.issues] == [
        ToolChainSanitizationReason.STALE_INCOMPLETE_ASSISTANT_TOOL_CALLS,
        ToolChainSanitizationReason.PARTIAL_TOOL_RESULTS_REMOVED,
    ]
    assert result.has_open_tail is False


def test_persistent_mode_preserves_last_incomplete_assistant_as_open_tail() -> None:
    messages = [
        {"role": str(MessageRole.USER), "content": "start"},
        _assistant_tool_call("a", "b"),
        _tool("a", "partial"),
        {"role": str(MessageRole.USER), "content": "new user while tool b is missing"},
    ]

    result = DefaultSessionToolChainSanitizer().sanitize(
        messages,
        mode=ToolChainSanitizationMode.PERSISTENT_SESSION,
    )

    assert result.messages == messages
    assert result.removed_messages == []
    assert result.has_open_tail is True
    assert result.open_tail_assistant_index == 1


def test_model_visible_mode_removes_last_incomplete_assistant_and_partial_tool() -> None:
    messages = [
        {"role": str(MessageRole.USER), "content": "start"},
        _assistant_tool_call("a", "b"),
        _tool("a", "partial"),
        {"role": str(MessageRole.USER), "content": "new user"},
    ]

    result = DefaultSessionToolChainSanitizer().sanitize(
        messages,
        mode=ToolChainSanitizationMode.MODEL_VISIBLE_CONTEXT,
    )

    assert result.messages == [
        {"role": str(MessageRole.USER), "content": "start"},
        {"role": str(MessageRole.USER), "content": "new user"},
    ]
    assert result.removed_indices == {1, 2}
    assert result.has_open_tail is False


def test_duplicate_tool_result_keeps_first_and_removes_later_duplicate() -> None:
    messages = [
        _assistant_tool_call("a"),
        _tool("a", "first"),
        _tool("a", "duplicate"),
    ]

    result = DefaultSessionToolChainSanitizer().sanitize(
        messages,
        mode=ToolChainSanitizationMode.PERSISTENT_SESSION,
    )

    assert result.messages == [_assistant_tool_call("a"), _tool("a", "first")]
    assert result.removed_messages == [_tool("a", "duplicate")]
    assert result.removed_indices == {2}
    assert [issue.reason for issue in result.issues] == [
        ToolChainSanitizationReason.DUPLICATE_TOOL_RESULT,
    ]
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_tool_chain_sanitizer.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'framework.memory.compression.tool_chain_sanitizer'`.

- [ ] **Step 3: Commit failing tests**

```bash
git add tests/unit/memory/test_tool_chain_sanitizer.py
git commit -m "test(memory): specify session tool-chain sanitizer"
```

## Task 2: Default Sanitizer Implementation And Exports

**Files:**
- Create: `framework/memory/compression/tool_chain_sanitizer.py`
- Modify: `framework/memory/__init__.py`
- Test: `tests/unit/memory/test_tool_chain_sanitizer.py`

- [ ] **Step 1: Implement sanitizer module**

Create `framework/memory/compression/tool_chain_sanitizer.py` with:

```python
"""Session and model-visible tool-chain sanitization."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from framework.core.types import MessageRole


class ToolChainSanitizationMode(StrEnum):
    """Controls whether an incomplete final tool-call assistant is preserved."""

    PERSISTENT_SESSION = "persistent_session"
    MODEL_VISIBLE_CONTEXT = "model_visible_context"


class ToolChainSanitizationReason(StrEnum):
    """Reasons a message was removed by tool-chain sanitization."""

    ORPHAN_TOOL_RESULT = "orphan_tool_result"
    STALE_INCOMPLETE_ASSISTANT_TOOL_CALLS = "stale_incomplete_assistant_tool_calls"
    PARTIAL_TOOL_RESULTS_REMOVED = "partial_tool_results_removed"
    DUPLICATE_TOOL_RESULT = "duplicate_tool_result"


@dataclass(frozen=True)
class ToolChainSanitizationIssue:
    """One structural issue found while sanitizing a message sequence."""

    index: int
    role: MessageRole
    reason: ToolChainSanitizationReason
    tool_call_id: str | None = None
    assistant_index: int | None = None


@dataclass(frozen=True)
class ToolChainSanitizationResult:
    """Sanitized messages plus the invalid messages removed from storage/input."""

    messages: list[dict[str, Any]]
    removed_messages: list[dict[str, Any]]
    removed_indices: set[int]
    issues: list[ToolChainSanitizationIssue]
    has_open_tail: bool = False
    open_tail_assistant_index: int | None = None


class SessionToolChainSanitizer(Protocol):
    """Analyze and sanitize assistant/tool structural relationships."""

    def sanitize(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        mode: ToolChainSanitizationMode,
    ) -> ToolChainSanitizationResult:
        """Return a sanitized copy of ``messages`` and issue metadata."""
        ...


@dataclass
class _AssistantGroup:
    assistant_index: int
    call_ids: list[str]
    tool_indices_by_call_id: dict[str, list[int]] = field(default_factory=dict)

    @property
    def matched_tool_indices(self) -> set[int]:
        indices: set[int] = set()
        for values in self.tool_indices_by_call_id.values():
            indices.update(values)
        return indices

    @property
    def is_complete(self) -> bool:
        return bool(self.call_ids) and all(self.tool_indices_by_call_id.get(call_id) for call_id in self.call_ids)


class DefaultSessionToolChainSanitizer(SessionToolChainSanitizer):
    """Default full-sequence sanitizer for session storage and LLM input."""

    def sanitize(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        mode: ToolChainSanitizationMode,
    ) -> ToolChainSanitizationResult:
        copied = [dict(message) for message in messages]
        last_tool_assistant = self._last_tool_call_assistant_index(copied)
        groups = self._collect_groups(copied)
        removed_indices: set[int] = set()
        issues: list[ToolChainSanitizationIssue] = []
        consumed_tools: set[int] = set()
        has_open_tail = False
        open_tail_assistant_index: int | None = None

        for group in groups:
            is_last_tool_assistant = group.assistant_index == last_tool_assistant
            preserve_incomplete_tail = (
                mode == ToolChainSanitizationMode.PERSISTENT_SESSION
                and is_last_tool_assistant
                and not group.is_complete
            )

            if group.is_complete or preserve_incomplete_tail:
                if preserve_incomplete_tail:
                    has_open_tail = True
                    open_tail_assistant_index = group.assistant_index
                for call_id in group.call_ids:
                    tool_indices = group.tool_indices_by_call_id.get(call_id, [])
                    if not tool_indices:
                        continue
                    consumed_tools.add(tool_indices[0])
                    for duplicate_index in tool_indices[1:]:
                        removed_indices.add(duplicate_index)
                        issues.append(self._issue(
                            copied,
                            duplicate_index,
                            ToolChainSanitizationReason.DUPLICATE_TOOL_RESULT,
                            tool_call_id=call_id,
                            assistant_index=group.assistant_index,
                        ))
                continue

            removed_indices.add(group.assistant_index)
            issues.append(self._issue(
                copied,
                group.assistant_index,
                ToolChainSanitizationReason.STALE_INCOMPLETE_ASSISTANT_TOOL_CALLS,
                assistant_index=group.assistant_index,
            ))
            for tool_index in sorted(group.matched_tool_indices):
                removed_indices.add(tool_index)
                issues.append(self._issue(
                    copied,
                    tool_index,
                    ToolChainSanitizationReason.PARTIAL_TOOL_RESULTS_REMOVED,
                    tool_call_id=str(copied[tool_index].get("tool_call_id", "")),
                    assistant_index=group.assistant_index,
                ))

        for index, message in enumerate(copied):
            if message.get("role") != str(MessageRole.TOOL):
                continue
            if index in removed_indices or index in consumed_tools:
                continue
            removed_indices.add(index)
            issues.append(self._issue(
                copied,
                index,
                ToolChainSanitizationReason.ORPHAN_TOOL_RESULT,
                tool_call_id=str(message.get("tool_call_id", "")),
            ))

        sanitized = [dict(message) for index, message in enumerate(copied) if index not in removed_indices]
        removed = [dict(copied[index]) for index in sorted(removed_indices)]
        return ToolChainSanitizationResult(
            messages=sanitized,
            removed_messages=removed,
            removed_indices=set(removed_indices),
            issues=issues,
            has_open_tail=has_open_tail,
            open_tail_assistant_index=open_tail_assistant_index,
        )

    @staticmethod
    def _last_tool_call_assistant_index(messages: Sequence[dict[str, Any]]) -> int | None:
        result: int | None = None
        for index, message in enumerate(messages):
            if message.get("role") == str(MessageRole.ASSISTANT) and message.get("tool_calls"):
                result = index
        return result

    def _collect_groups(self, messages: Sequence[dict[str, Any]]) -> list[_AssistantGroup]:
        groups: list[_AssistantGroup] = []
        active_by_call_id: dict[str, _AssistantGroup] = {}
        for index, message in enumerate(messages):
            role = message.get("role")
            if role == str(MessageRole.ASSISTANT) and message.get("tool_calls"):
                call_ids = self._call_ids(message)
                group = _AssistantGroup(assistant_index=index, call_ids=call_ids)
                groups.append(group)
                for call_id in call_ids:
                    active_by_call_id[call_id] = group
                continue
            if role == str(MessageRole.TOOL):
                call_id = message.get("tool_call_id")
                if call_id is None:
                    continue
                group = active_by_call_id.get(str(call_id))
                if group is None:
                    continue
                group.tool_indices_by_call_id.setdefault(str(call_id), []).append(index)
        return groups

    @staticmethod
    def _call_ids(message: dict[str, Any]) -> list[str]:
        call_ids: list[str] = []
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict) and call.get("id") is not None:
                call_ids.append(str(call["id"]))
        return call_ids

    @staticmethod
    def _issue(
        messages: Sequence[dict[str, Any]],
        index: int,
        reason: ToolChainSanitizationReason,
        *,
        tool_call_id: str | None = None,
        assistant_index: int | None = None,
    ) -> ToolChainSanitizationIssue:
        role_value = str(messages[index].get("role", ""))
        try:
            role = MessageRole(role_value)
        except ValueError:
            role = MessageRole.USER
        return ToolChainSanitizationIssue(
            index=index,
            role=role,
            reason=reason,
            tool_call_id=tool_call_id,
            assistant_index=assistant_index,
        )
```

- [ ] **Step 2: Export sanitizer types**

Modify `framework/memory/__init__.py` imports:

```python
from framework.memory.compression.tool_chain_sanitizer import (
    DefaultSessionToolChainSanitizer,
    SessionToolChainSanitizer,
    ToolChainSanitizationIssue,
    ToolChainSanitizationMode,
    ToolChainSanitizationReason,
    ToolChainSanitizationResult,
)
```

Add these names to `__all__` near the compression section:

```python
    "DefaultSessionToolChainSanitizer",
    "SessionToolChainSanitizer",
    "ToolChainSanitizationIssue",
    "ToolChainSanitizationMode",
    "ToolChainSanitizationReason",
    "ToolChainSanitizationResult",
```

- [ ] **Step 3: Run sanitizer tests**

Run:

```bash
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_tool_chain_sanitizer.py -q
```

Expected: all tests pass.

- [ ] **Step 4: Run focused lint**

Run:

```bash
ruff check --no-cache F:\tool\pythonProject\ModexAgent\framework\memory\compression\tool_chain_sanitizer.py F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_tool_chain_sanitizer.py --select I,F
```

Expected: all checks pass.

- [ ] **Step 5: Commit sanitizer implementation**

```bash
git add framework/memory/compression/tool_chain_sanitizer.py framework/memory/__init__.py tests/unit/memory/test_tool_chain_sanitizer.py
git commit -m "feat(memory): add session tool-chain sanitizer"
```

## Task 3: Governance Uses Model-Visible Sanitization

**Files:**
- Create or modify: `tests/unit/memory/test_context_governance.py`
- Modify: `framework/memory/context_governance.py`

- [ ] **Step 1: Write failing governance tests**

Create `tests/unit/memory/test_context_governance.py` if it does not exist. Add:

```python
from __future__ import annotations

import pytest

from framework.core.types import MessageRole
from framework.memory.context_governance import (
    FinalContextLegalityGovernance,
    ToolChainRepairGovernance,
)


def _assistant_tool_call(*call_ids: str) -> dict:
    return {
        "role": str(MessageRole.ASSISTANT),
        "content": "",
        "tool_calls": [
            {"id": call_id, "function": {"name": f"tool_{call_id}"}}
            for call_id in call_ids
        ],
    }


def _tool(call_id: str) -> dict:
    return {"role": str(MessageRole.TOOL), "tool_call_id": call_id, "content": "result"}


@pytest.mark.asyncio
async def test_tool_chain_repair_removes_last_incomplete_assistant_for_model_visible_context() -> None:
    messages = [
        {"role": str(MessageRole.USER), "content": "start"},
        _assistant_tool_call("a", "b"),
        _tool("a"),
        {"role": str(MessageRole.USER), "content": "next"},
    ]

    result = await ToolChainRepairGovernance().apply(messages)

    assert result == [
        {"role": str(MessageRole.USER), "content": "start"},
        {"role": str(MessageRole.USER), "content": "next"},
    ]


@pytest.mark.asyncio
async def test_final_legality_removes_orphan_tool_and_incomplete_assistant() -> None:
    messages = [
        _tool("orphan"),
        _assistant_tool_call("a", "b"),
        _tool("a"),
        {"role": str(MessageRole.ASSISTANT), "content": "plain"},
    ]

    result = await FinalContextLegalityGovernance().apply(messages)

    assert result == [{"role": str(MessageRole.ASSISTANT), "content": "plain"}]
```

- [ ] **Step 2: Run governance tests to verify failure**

Run:

```bash
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_context_governance.py -q
```

Expected: FAIL because current governance backfills missing tool results instead of removing incomplete assistant groups.

- [ ] **Step 3: Update governance classes**

Modify `framework/memory/context_governance.py` imports:

```python
from framework.memory.compression.tool_chain_sanitizer import (
    DefaultSessionToolChainSanitizer,
    ToolChainSanitizationMode,
)
```

Replace `ToolChainRepairGovernance.apply` with:

```python
    async def apply(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = DefaultSessionToolChainSanitizer().sanitize(
            messages,
            mode=ToolChainSanitizationMode.MODEL_VISIBLE_CONTEXT,
        )
        return result.messages
```

Replace `FinalContextLegalityGovernance.apply` with:

```python
    async def apply(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = DefaultSessionToolChainSanitizer().sanitize(
            messages,
            mode=ToolChainSanitizationMode.MODEL_VISIBLE_CONTEXT,
        )
        return result.messages
```

Leave the existing classes in place so bot configuration does not change. Do not write sanitizer results back to session.

- [ ] **Step 4: Run governance and sanitizer tests**

Run:

```bash
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_context_governance.py F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_tool_chain_sanitizer.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit governance changes**

```bash
git add framework/memory/context_governance.py tests/unit/memory/test_context_governance.py
git commit -m "fix(memory): sanitize model-visible tool chains"
```

## Task 4: Compression Plan Fields And Cleanup-Only Tests

**Files:**
- Modify: `framework/memory/core/models.py`
- Modify: `tests/unit/memory/test_compression_policies.py`

- [ ] **Step 1: Extend `CompressionPlan` fields**

Modify `framework/memory/core/models.py` imports:

```python
from framework.memory.compression.tool_chain_sanitizer import ToolChainSanitizationIssue
```

Extend `CompressionPlan`:

```python
@dataclass(frozen=True)
class CompressionPlan:
    trigger: CompressionTrigger
    expected_revision: StorageRevision
    expected_cursor: int | None
    keep_messages: list[dict[str, Any]]
    summarize_messages: list[dict[str, Any]]
    archive_raw_messages: list[dict[str, Any]]
    drop_messages: list[dict[str, Any]]
    summary: str | None = None
    pending_pruned_input_entries: list[Any] = field(default_factory=list)
    drop_without_archive_messages: list[dict[str, Any]] = field(default_factory=list)
    sanitization_issues: list[ToolChainSanitizationIssue] = field(default_factory=list)
    has_open_tail: bool = False
```

- [ ] **Step 2: Add failing cleanup-only compression test**

Append to `tests/unit/memory/test_compression_policies.py`:

```python
async def test_coordinator_removes_stale_invalid_tool_chain_without_archive(registry):
    from framework.memory.core.scope import MemoryLayerName
    from framework.memory.layers.config import SessionMemoryConfig
    from framework.memory.layers.session import ScopedSessionMemoryManager

    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.SESSION)
    session = ScopedSessionMemoryManager(factory, SessionMemoryConfig(max_messages=None))
    ctx = MemoryContext(session_id="sanitize-cleanup")
    await session.add_messages(ctx, [
        {"role": str(MessageRole.USER), "content": "start"},
        {
            "role": str(MessageRole.ASSISTANT),
            "content": "",
            "tool_calls": [
                {"id": "a", "function": {"name": "tool_a"}},
                {"id": "b", "function": {"name": "tool_b"}},
            ],
        },
        {"role": str(MessageRole.TOOL), "tool_call_id": "a", "content": "partial"},
        {"role": str(MessageRole.ASSISTANT), "content": "plain"},
    ])

    coordinator = DefaultMemoryCompressionCoordinator(max_messages=3)
    result = await coordinator.maybe_compress(
        session=session,
        archive=None,
        context=ctx,
    )

    assert result.committed
    assert [msg.to_dict() for msg in await session.get_all_messages(ctx)] == [
        {"role": str(MessageRole.USER), "content": "start"},
        {"role": str(MessageRole.ASSISTANT), "content": "plain"},
    ]
```

- [ ] **Step 3: Add failing open-tail cleanup-only test**

Append to `tests/unit/memory/test_compression_policies.py`:

```python
async def test_coordinator_preserves_active_open_tail_but_removes_older_invalid_chain(registry):
    from framework.memory.core.scope import MemoryLayerName
    from framework.memory.layers.config import SessionMemoryConfig
    from framework.memory.layers.session import ScopedSessionMemoryManager

    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.SESSION)
    session = ScopedSessionMemoryManager(factory, SessionMemoryConfig(max_messages=None))
    ctx = MemoryContext(session_id="sanitize-open-tail")
    open_tail = {
        "role": str(MessageRole.ASSISTANT),
        "content": "",
        "tool_calls": [
            {"id": "tail-a", "function": {"name": "tool_tail_a"}},
            {"id": "tail-b", "function": {"name": "tool_tail_b"}},
        ],
    }
    await session.add_messages(ctx, [
        {"role": str(MessageRole.USER), "content": "start"},
        {
            "role": str(MessageRole.ASSISTANT),
            "content": "",
            "tool_calls": [{"id": "old-a", "function": {"name": "tool_old_a"}}],
        },
        {"role": str(MessageRole.USER), "content": "continued"},
        open_tail,
        {"role": str(MessageRole.TOOL), "tool_call_id": "tail-a", "content": "partial"},
    ])

    coordinator = DefaultMemoryCompressionCoordinator(max_messages=3)
    result = await coordinator.maybe_compress(
        session=session,
        archive=None,
        context=ctx,
    )

    assert result.committed
    assert [msg.to_dict() for msg in await session.get_all_messages(ctx)] == [
        {"role": str(MessageRole.USER), "content": "start"},
        {"role": str(MessageRole.USER), "content": "continued"},
        open_tail,
        {"role": str(MessageRole.TOOL), "tool_call_id": "tail-a", "content": "partial"},
    ]
```

- [ ] **Step 4: Run tests to verify failure**

Run:

```bash
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_compression_policies.py::test_coordinator_removes_stale_invalid_tool_chain_without_archive F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_compression_policies.py::test_coordinator_preserves_active_open_tail_but_removes_older_invalid_chain -q
```

Expected: FAIL because the coordinator has not run sanitizer yet.

- [ ] **Step 5: Commit failing integration tests and model field**

```bash
git add framework/memory/core/models.py tests/unit/memory/test_compression_policies.py
git commit -m "test(memory): cover sanitizer compression cleanup"
```

## Task 5: Wire Sanitizer Into Compression Coordinator

**Files:**
- Modify: `framework/memory/compression/policies.py`
- Test: `tests/unit/memory/test_compression_policies.py`

- [ ] **Step 1: Add sanitizer imports and constructor injection**

Modify imports in `framework/memory/compression/policies.py`:

```python
from collections import Counter

from framework.memory.compression.tool_chain_sanitizer import (
    DefaultSessionToolChainSanitizer,
    SessionToolChainSanitizer,
    ToolChainSanitizationMode,
)
```

Add constructor parameter to `DefaultMemoryCompressionCoordinator.__init__`:

```python
        tool_chain_sanitizer: SessionToolChainSanitizer | None = None,
```

Assign it:

```python
        self._tool_chain_sanitizer = tool_chain_sanitizer or DefaultSessionToolChainSanitizer()
```

- [ ] **Step 2: Sanitize before compaction planning**

In `maybe_compress`, immediately after reading `all_msgs`, insert:

```python
        try:
            sanitization = self._tool_chain_sanitizer.sanitize(
                all_msgs,
                mode=ToolChainSanitizationMode.PERSISTENT_SESSION,
            )
        except Exception:
            logger.warning("Session tool-chain sanitization failed", exc_info=True)
            return CompressionResult(committed=True, reason=CompressionResultReason.NO_SAFE_BOUNDARY)

        if sanitization.removed_messages:
            counts = Counter(issue.reason for issue in sanitization.issues)
            logger.info(
                "Session tool-chain sanitizer removed invalid messages: session=%s removed=%d reasons=%s open_tail=%s",
                context.session_id,
                len(sanitization.removed_messages),
                {str(reason): count for reason, count in counts.items()},
                sanitization.has_open_tail,
            )

        sanitized_msgs = sanitization.messages
        if not sanitized_msgs:
            revision = await session.get_revision(context)
            plan = CompressionPlan(
                trigger=trigger,
                expected_revision=revision,
                expected_cursor=None,
                keep_messages=[],
                summarize_messages=[],
                archive_raw_messages=[],
                drop_messages=[],
                summary="",
                drop_without_archive_messages=sanitization.removed_messages,
                sanitization_issues=sanitization.issues,
                has_open_tail=sanitization.has_open_tail,
            )
            return await self._commit.commit(
                plan=plan,
                session=session,
                archive=None,
                pending=pending,
                context=context,
                error_policy=self._error,
            )

        if sanitization.has_open_tail:
            if not sanitization.removed_messages:
                return CompressionResult(committed=True, reason=CompressionResultReason.NO_SAFE_BOUNDARY)
            revision = await session.get_revision(context)
            plan = CompressionPlan(
                trigger=trigger,
                expected_revision=revision,
                expected_cursor=None,
                keep_messages=sanitized_msgs,
                summarize_messages=[],
                archive_raw_messages=[],
                drop_messages=[],
                summary="",
                drop_without_archive_messages=sanitization.removed_messages,
                sanitization_issues=sanitization.issues,
                has_open_tail=True,
            )
            return await self._commit.commit(
                plan=plan,
                session=session,
                archive=None,
                pending=pending,
                context=context,
                error_policy=self._error,
            )

        all_msgs = sanitized_msgs
```

This makes cleanup-only plans skip archive writing because invalid sanitizer removals must not be archived.

- [ ] **Step 3: Preserve sanitizer metadata in normal plans**

When building the existing `CompressionPlan`, add:

```python
            drop_without_archive_messages=sanitization.removed_messages,
            sanitization_issues=sanitization.issues,
            has_open_tail=sanitization.has_open_tail,
```

The pending extractor call must keep using sanitized `all_msgs` and `pruned_indices_set`.

- [ ] **Step 4: Run compression sanitizer tests**

Run:

```bash
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_compression_policies.py::test_coordinator_removes_stale_invalid_tool_chain_without_archive F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_compression_policies.py::test_coordinator_preserves_active_open_tail_but_removes_older_invalid_chain -q
```

Expected: both tests pass.

- [ ] **Step 5: Run full compression policy tests**

Run:

```bash
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_compression_policies.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit compression wiring**

```bash
git add framework/memory/compression/policies.py tests/unit/memory/test_compression_policies.py
git commit -m "feat(memory): sanitize session tool chains during compression"
```

## Task 6: Lifecycle Uses Active-Tail Analysis

**Files:**
- Modify: `tests/unit/memory/test_lifecycle.py`
- Modify: `framework/memory/lifecycle.py`

- [ ] **Step 1: Add failing lifecycle regression**

Append to `TestDefaultMemoryLifecyclePolicy` in `tests/unit/memory/test_lifecycle.py`:

```python
    @pytest.mark.asyncio
    async def test_on_messages_added_does_not_skip_for_old_stale_incomplete_tool_call(self):
        coordinator = AsyncMock()
        coordinator.maybe_compress = AsyncMock()
        policy = DefaultMemoryLifecyclePolicy(compression_coordinator=coordinator)
        ctx = MemoryContext(session_id="s1", user_id="u1")
        session = AsyncMock()
        session.get_all_messages = AsyncMock(
            return_value=[
                {
                    "role": str(MessageRole.ASSISTANT),
                    "content": "",
                    "tool_calls": [
                        {"id": "old-a", "function": {"name": "search_files"}},
                        {"id": "old-b", "function": {"name": "search_files"}},
                    ],
                },
                {"role": str(MessageRole.TOOL), "tool_call_id": "old-a", "content": "partial"},
                {"role": str(MessageRole.ASSISTANT), "content": "done"},
            ]
        )
        layers = MemoryLayerSet(session=session, archive=None)

        await policy.on_messages_added(ctx, layers)

        coordinator.maybe_compress.assert_called_once_with(
            session=layers.session,
            archive=None,
            pending=None,
            context=ctx,
        )
```

- [ ] **Step 2: Run lifecycle test to verify failure**

Run:

```bash
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_lifecycle.py::TestDefaultMemoryLifecyclePolicy::test_on_messages_added_does_not_skip_for_old_stale_incomplete_tool_call -q
```

Expected: FAIL because `_has_open_react_process` currently treats old incomplete data as active.

- [ ] **Step 3: Update lifecycle open-tail detection**

Modify `framework/memory/lifecycle.py` imports:

```python
from framework.memory.compression.tool_chain_sanitizer import (
    DefaultSessionToolChainSanitizer,
    ToolChainSanitizationMode,
)
```

Replace the body after messages are loaded in `_has_open_react_process` with:

```python
        result = DefaultSessionToolChainSanitizer().sanitize(
            messages,
            mode=ToolChainSanitizationMode.PERSISTENT_SESSION,
        )
        return result.has_open_tail
```

Keep the existing docstring but update it to say the check protects only the active last assistant tool-call tail.

- [ ] **Step 4: Run lifecycle tests**

Run:

```bash
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_lifecycle.py -q
```

Expected: all lifecycle tests pass.

- [ ] **Step 5: Commit lifecycle changes**

```bash
git add framework/memory/lifecycle.py tests/unit/memory/test_lifecycle.py
git commit -m "fix(memory): detect only active tool-call tail in lifecycle"
```

## Task 7: Pending And Archive Boundary Regression Tests

**Files:**
- Modify: `tests/unit/memory/test_compression_policies.py`

- [ ] **Step 1: Add pending exclusion regression**

Append:

```python
async def test_sanitizer_removed_messages_do_not_create_pending_entries(registry):
    from framework.memory.core.scope import MemoryLayerName
    from framework.memory.layers.config import SessionMemoryConfig
    from framework.memory.layers.session import ScopedSessionMemoryManager

    layer_set = MemoryLayerFactory.single_user(registry=registry)
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.SESSION)
    session = ScopedSessionMemoryManager(factory, SessionMemoryConfig(max_messages=None))
    ctx = MemoryContext(session_id="pending-boundary")
    await session.add_messages(ctx, [
        {
            "role": str(MessageRole.ASSISTANT),
            "content": "",
            "tool_calls": [{"id": "old-a", "function": {"name": "tool_old_a"}}],
        },
        {"role": str(MessageRole.USER), "content": "unfinished"},
    ])

    assert layer_set.pending is not None
    coordinator = DefaultMemoryCompressionCoordinator(max_messages=1)
    await coordinator.maybe_compress(
        session=session,
        archive=None,
        pending=layer_set.pending,
        context=ctx,
    )

    entries = await layer_set.pending.get_entries(ctx)
    assert [entry.content for entry in entries] == []
```

- [ ] **Step 2: Add archive exclusion regression**

Append:

```python
async def test_sanitizer_removed_messages_are_not_archived(registry):
    from framework.memory.core.scope import MemoryLayerName
    from framework.memory.layers.config import SessionMemoryConfig
    from framework.memory.layers.session import ScopedSessionMemoryManager

    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.SESSION)
    session = ScopedSessionMemoryManager(factory, SessionMemoryConfig(max_messages=None))
    ctx = MemoryContext(session_id="archive-boundary")
    await session.add_messages(ctx, [
        {
            "role": str(MessageRole.ASSISTANT),
            "content": "",
            "tool_calls": [{"id": "old-a", "function": {"name": "tool_old_a"}}],
        },
        {"role": str(MessageRole.ASSISTANT), "content": "plain"},
    ])

    class RecordingArchive:
        def __init__(self):
            self.entries = []

        async def append(self, context, entry):
            _ = context
            self.entries.append(entry)

    archive = RecordingArchive()
    coordinator = DefaultMemoryCompressionCoordinator(max_messages=1)
    await coordinator.maybe_compress(
        session=session,
        archive=archive,  # type: ignore[arg-type]
        context=ctx,
    )

    assert archive.entries == []
```

- [ ] **Step 3: Run boundary tests**

Run:

```bash
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_compression_policies.py::test_sanitizer_removed_messages_do_not_create_pending_entries F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_compression_policies.py::test_sanitizer_removed_messages_are_not_archived -q
```

Expected: both tests pass.

- [ ] **Step 4: Commit boundary tests**

```bash
git add tests/unit/memory/test_compression_policies.py
git commit -m "test(memory): verify sanitizer archive and pending boundaries"
```

## Task 8: Final Verification

**Files:**
- No code changes expected.

- [ ] **Step 1: Run focused memory tests**

Run:

```bash
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_tool_chain_sanitizer.py F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_context_governance.py F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_compression_policies.py F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_lifecycle.py F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_pending_pruned_inputs.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run bot project memory config tests**

Run:

```bash
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\examples\bot_project\tests\test_pending_memory_config.py F:\tool\pythonProject\ModexAgent\examples\bot_project\tests\test_compression_config_pass_through.py -q
```

Expected: all selected bot project tests pass. If `test_compression_config_pass_through.py` remains untracked from earlier work, do not add it unless the implementation changed it.

- [ ] **Step 3: Run lint and type checks**

Run:

```bash
ruff check --no-cache F:\tool\pythonProject\ModexAgent\framework\memory F:\tool\pythonProject\ModexAgent\tests\unit\memory --select I,F
mypy F:\tool\pythonProject\ModexAgent\framework\memory
```

Expected: ruff passes. Mypy passes for `framework\memory`; if unrelated existing issues appear outside touched memory files, record exact file and line in the final handoff.

- [ ] **Step 4: Check git status**

Run:

```bash
git -C F:\tool\pythonProject\ModexAgent status --short
git -C F:\tool\pythonProject\ModexAgent log --oneline -8
```

Expected: only pre-existing unrelated worktree entries remain unstaged. The sanitizer implementation commits appear at the top of the log.

## Self-Review Checklist

- Spec coverage:
  - Full-session sanitizer: Tasks 1 and 2.
  - Last assistant persistent-session exception: Tasks 1 and 2.
  - Governance removes incomplete model-visible tool chains: Task 3.
  - Compression cleanup without archive: Tasks 4 and 5.
  - Active open tail prevents normal compression while allowing stale cleanup: Tasks 4, 5, and 6.
  - Archive and pending exclusion: Task 7.
  - Main/peer/subagent shared behavior through `archive=None`: Tasks 5, 7, and 8.
- Placeholder scan:
  - No placeholder markers or undefined step references are present.
- Type consistency:
  - `ToolChainSanitizationMode`, `ToolChainSanitizationReason`, `ToolChainSanitizationIssue`, `ToolChainSanitizationResult`, `SessionToolChainSanitizer`, and `DefaultSessionToolChainSanitizer` are defined before use.
  - `CompressionPlan.drop_without_archive_messages`, `CompressionPlan.sanitization_issues`, and `CompressionPlan.has_open_tail` are defined before coordinator use.
