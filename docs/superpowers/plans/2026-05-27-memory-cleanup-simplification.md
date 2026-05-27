# Memory Cleanup & Archival Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Simplify session cleanup and archival flow — cleanup always executes, archival is optional and can fail, no more callback injection or interceptor pattern for memory operations.

**Architecture:** Replace `MemoryCompressionCoordinator` + `MemoryLifecyclePolicy` callback with a single `cleanup_session()` function called directly from `ScopedMessageHistory`. Convert `ArchiveGenerationStrategy` from Protocol to ABC, add message field filtering, add sliding window segmentation internally.

**Tech Stack:** Python 3.12+, asyncio, pytest

**Prerequisites:** Read `docs/superpowers/specs/2026-05-27-memory-cleanup-simplification-design.md` first.

---

## File Structure Map

```
framework/memory/
  cleanup.py              ← NEW: cleanup_session() + internal keep-planner
  sanitizer.py            ← MOVED: from compression/tool_chain_sanitizer.py
  archive_generation.py   ← MODIFY: Protocol→ABC, ArchiveInputMessage, sliding window
  default_system.py       ← MODIFY: remove on_messages_added callback, call cleanup_session directly
  system.py               ← MODIFY: remove lifecycle_policy param
  lifecycle.py            ← MODIFY: keep only MemoryMaintenancePolicy
  core/models.py          ← MODIFY: keep only models used by remaining code
  core/system.py          ← MODIFY: remove get_auto_compact_summary

  DELETE:
  compression/            ← policies.py, planner.py, tool_chain.py, semantic_filter.py, __init__.py
  compaction/             ← policy.py, boundary.py, __init__.py
  retention/              ← policy.py, default.py, config.py, types.py, __init__.py

framework/
  interceptor/abc.py      ← MODIFY: remove MEMORY_OPERATION
  ioc/configs/memory.py   ← MODIFY: remove auto_compact
  ioc/factories/memory.py ← MODIFY: remove auto_compact gating, always create DualLLMArchiveGenerationStrategy
  ioc/factories/compression.py ← SIMPLIFY or DELETE

examples/bot_project/
  bot/service/core.py     ← MODIFY: _auto_compact_task → _maintenance_task
  bot/service/builders.py ← MODIFY: remove DefaultMemoryLifecyclePolicy
  config/pools/main.yml   ← MODIFY: remove auto_compact lines
```

---

### Task 1: ArchiveGenerationStrategy Protocol → ABC + ArchiveInputMessage

**Files:**
- Modify: `framework/memory/archive_generation.py`
- Modify: `framework/memory/archive_models.py`
- Create: `tests/unit/memory/test_archive_generation.py`

This task converts the Protocol to ABC, adds the `ArchiveInputMessage` dataclass for message field filtering, and adds sliding window segmentation to `DualLLMArchiveGenerationStrategy`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/memory/test_archive_generation.py`:

```python
"""Tests for ArchiveGenerationStrategy ABC and DualLLMArchiveGenerationStrategy."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from framework.memory.archive_generation import (
    ArchiveGenerationStrategy,
    ArchiveInputMessage,
    DualLLMArchiveGenerationStrategy,
    SummarizerLike,
)
from framework.memory.archive_models import (
    ArchiveChannel,
    ArchiveGenerationInputs,
    ArchiveGenerationResult,
    ArchiveInputStats,
    ArchiveWrite,
)
from framework.memory.core.models import CompressionReason
from framework.memory.core.scope import MemoryContext


# ── ABC registration test ──────────────────────────────────────────────

def test_archive_generation_strategy_is_abc():
    """ArchiveGenerationStrategy must be an ABC, not a Protocol."""
    from abc import ABC
    assert issubclass(ArchiveGenerationStrategy, ABC), (
        "ArchiveGenerationStrategy should be an ABC subclass"
    )
    # Cannot instantiate directly
    with pytest.raises(TypeError):
        ArchiveGenerationStrategy()  # type: ignore[abstract]


def test_archive_generation_strategy_subclass_must_implement_generate():
    """Subclass without generate() must fail instantiation."""
    class Incomplete(ArchiveGenerationStrategy):
        pass

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


def test_archive_generation_strategy_subclass_works():
    """Subclass with generate() must instantiate."""
    class Complete(ArchiveGenerationStrategy):
        async def generate(
            self,
            messages: Sequence[ArchiveInputMessage],
            context: MemoryContext,
            reason: CompressionReason,
        ) -> ArchiveGenerationResult:
            return ArchiveGenerationResult(
                writes=(),
                inputs=ArchiveGenerationInputs(
                    context_transcript="",
                    knowledge_transcript="",
                    stats=ArchiveInputStats(0, 0, 0, 0, 0),
                ),
            )

    instance = Complete()
    assert isinstance(instance, ArchiveGenerationStrategy)


# ── ArchiveInputMessage ────────────────────────────────────────────────

def test_archive_input_message_from_chat_message_user():
    """User message: role + content, no metadata."""
    msg = ArchiveInputMessage.from_dict({"role": "user", "content": "hello", "metadata": {"k": "v"}})
    assert msg.role == "user"
    assert msg.content == "hello"
    assert msg.tool_call_id is None


def test_archive_input_message_from_chat_message_assistant_strips_tool_calls():
    """Assistant message: tool_calls discarded."""
    msg = ArchiveInputMessage.from_dict({
        "role": "assistant",
        "content": "done",
        "tool_calls": [{"id": "t1", "function": {"name": "read_file"}}],
    })
    assert msg.role == "assistant"
    assert msg.content == "done"
    assert msg.tool_call_id is None


def test_archive_input_message_from_chat_message_tool():
    """Tool message: role + content + tool_call_id."""
    msg = ArchiveInputMessage.from_dict({
        "role": "tool",
        "content": "file content",
        "tool_call_id": "t1",
        "name": "read_file",
    })
    assert msg.role == "tool"
    assert msg.content == "file content"
    assert msg.tool_call_id == "t1"


# ── Sliding window segmentation ────────────────────────────────────────

class MockSummarizer(SummarizerLike):
    """Deterministic summarizer that echoes input back."""
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def summarize(
        self, text: str, *, prompt: str | None = None,
        max_tokens: int = 500, temperature: float = 0.3,
    ) -> str:
        self.calls.append(text)
        return f"summary of: {text[:50]}"


@pytest.mark.asyncio
async def test_sliding_window_single_segment():
    """Messages within token budget → single LLM call."""
    summarizer = MockSummarizer()
    strategy = DualLLMArchiveGenerationStrategy(
        summarizer=summarizer,
        max_segment_tokens=12000,
    )
    messages = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
    ]
    result = await strategy.generate(
        [ArchiveInputMessage.from_dict(m) for m in messages],
        MemoryContext(session_id="s1"),
        CompressionReason.MESSAGE_COUNT,
    )
    # 2 LLM calls: one for CONTEXT, one for KNOWLEDGE (single segment)
    assert len(summarizer.calls) == 2


@pytest.mark.asyncio
async def test_sliding_window_multiple_segments():
    """Messages exceeding max_segment_tokens → multiple segments per channel."""
    summarizer = MockSummarizer()
    # Low segment limit forces many segments
    strategy = DualLLMArchiveGenerationStrategy(
        summarizer=summarizer,
        max_segment_tokens=50,
    )
    messages = [
        {"role": "user", "content": "A" * 100},
        {"role": "assistant", "content": "B" * 100},
        {"role": "user", "content": "C" * 100},
        {"role": "assistant", "content": "D" * 100},
    ]
    result = await strategy.generate(
        [ArchiveInputMessage.from_dict(m) for m in messages],
        MemoryContext(session_id="s2"),
        CompressionReason.MESSAGE_COUNT,
    )
    # 4 user-turn segments × 2 channels = 8 LLM calls
    assert len(summarizer.calls) == 8
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/unit/memory/test_archive_generation.py -v
```

Expected: multiple FAIL — `ArchiveGenerationStrategy` is still a Protocol, `ArchiveInputMessage` doesn't exist, `from_dict` doesn't exist, sliding window not yet implemented.

- [ ] **Step 3: Add ArchiveInputMessage to archive_generation.py**

Modify `framework/memory/archive_generation.py`. Add at top, before existing code:

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from framework.memory.archive_models import (
    ArchiveChannel,
    ArchiveGenerationResult,
    ArchiveWrite,
)
from framework.memory.core.models import CompressionReason
from framework.memory.core.scope import MemoryContext
from framework.memory.utils import normalize_memory_summary


@dataclass(frozen=True)
class ArchiveInputMessage:
    """A message prepared for archive generation with only essential fields.

    assistant.tool_calls is intentionally discarded — it adds noise
    without value for the summarizer. Each role keeps only the fields
    the summarizer actually needs.
    """
    role: str
    content: str
    tool_call_id: str | None = None

    @classmethod
    def from_dict(cls, message: dict[str, Any]) -> ArchiveInputMessage:
        role = str(message.get("role", ""))
        content = str(message.get("content", ""))
        tool_call_id: str | None = None
        if role == "tool":
            tc_id = message.get("tool_call_id")
            tool_call_id = str(tc_id) if tc_id is not None else None
        return cls(role=role, content=content, tool_call_id=tool_call_id)
```

- [ ] **Step 4: Convert ArchiveGenerationStrategy from Protocol to ABC**

In the same file, replace the Protocol:

```python
class ArchiveGenerationStrategy(ABC):
    """Strategy for generating archive entries from pruned session messages.

    Subclasses must implement ``generate()``. The default implementation
    is ``DualLLMArchiveGenerationStrategy`` which uses a SummarizerAgent
    to produce dual-channel (CONTEXT + KNOWLEDGE) archive entries.
    """

    @abstractmethod
    async def generate(
        self,
        messages: Sequence[ArchiveInputMessage],
        context: MemoryContext,
        reason: CompressionReason,
    ) -> ArchiveGenerationResult:
        """Generate archive entries from ``messages``."""
        raise NotImplementedError
```

- [ ] **Step 5: Run subset of tests to verify ABC conversion passes**

```bash
python -m pytest tests/unit/memory/test_archive_generation.py::test_archive_generation_strategy_is_abc tests/unit/memory/test_archive_generation.py::test_archive_generation_strategy_subclass_must_implement_generate tests/unit/memory/test_archive_generation.py::test_archive_generation_strategy_subclass_works tests/unit/memory/test_archive_generation.py::test_archive_input_message_from_chat_message_user tests/unit/memory/test_archive_generation.py::test_archive_input_message_from_chat_message_assistant_strips_tool_calls tests/unit/memory/test_archive_generation.py::test_archive_input_message_from_chat_message_tool -v
```

Expected: 6 passed

- [ ] **Step 6: Add summarizer protocol update and old-data adapter**

In `archive_generation.py`, update `SummarizerLike` (replace old Protocol import):

```python
class SummarizerLike(ABC):
    """Protocol-like ABC for summarizer agents."""

    @abstractmethod
    async def summarize(
        self,
        text: str,
        *,
        prompt: str | None = None,
        max_tokens: int = 500,
        temperature: float = 0.3,
    ) -> str:
        raise NotImplementedError
```

- [ ] **Step 7: Add sliding window to DualLLMArchiveGenerationStrategy**

Modify `DualLLMArchiveGenerationStrategy.__init__` to add `max_segment_tokens`:

```python
class DualLLMArchiveGenerationStrategy(ArchiveGenerationStrategy):
    def __init__(
        self,
        *,
        summarizer: SummarizerLike,
        max_segment_tokens: int = 12000,     # NEW
        context_max_tokens: int = 800,
        knowledge_max_tokens: int = 600,
    ) -> None:
        self._summarizer = summarizer
        self._max_segment_tokens = max_segment_tokens
        self._context_max_tokens = context_max_tokens
        self._knowledge_max_tokens = knowledge_max_tokens
```

Replace the `generate` method with one that does sliding window:

```python
    async def generate(
        self,
        messages: Sequence[ArchiveInputMessage],
        context: MemoryContext,
        reason: CompressionReason,
    ) -> ArchiveGenerationResult:
        from framework.agents.summarizer.agent import SummarizerAgent

        segments = self._segment(messages)
        merged = self._sliding_window_merge(segments)

        context_parts: list[str] = []
        knowledge_parts: list[str] = []
        total_input = 0

        for segment in merged:
            transcript = "\n\n".join(
                f"[{msg.role.upper()}]: {msg.content}"
                for msg in segment
            )
            context_text = self._prompt_input(transcript, reason)
            knowledge_text = self._prompt_input(transcript, reason)

            ctx_summary = await self._summarizer.summarize(
                context_text,
                prompt=SummarizerAgent.PROMPT_CONTEXT_ARCHIVE,
                max_tokens=self._context_max_tokens,
            )
            ctx_norm = normalize_memory_summary(ctx_summary)
            if ctx_norm:
                context_parts.append(ctx_norm)

            k_summary = await self._summarizer.summarize(
                knowledge_text,
                prompt=SummarizerAgent.PROMPT_KNOWLEDGE_ARCHIVE,
                max_tokens=self._knowledge_max_tokens,
                temperature=0.2,
            )
            k_norm = normalize_memory_summary(k_summary)
            if k_norm:
                knowledge_parts.append(k_norm)

            total_input += len(segment)

        context_summary = "\n---\n".join(context_parts) if context_parts else ""
        knowledge_summary = "\n---\n".join(knowledge_parts) if knowledge_parts else ""

        if not context_summary or not knowledge_summary:
            return ArchiveGenerationResult(
                writes=(),
                inputs=ArchiveGenerationInputs(
                    context_transcript=context_summary,
                    knowledge_transcript=knowledge_summary,
                    stats=ArchiveInputStats(
                        input_messages=total_input,
                        context_messages=len(context_parts),
                        knowledge_messages=len(knowledge_parts),
                        tool_chains=0,
                        dropped_messages=0,
                    ),
                ),
            )

        return ArchiveGenerationResult(
            writes=(
                ArchiveWrite(
                    channel=ArchiveChannel.CONTEXT,
                    summary=context_summary,
                    metadata={
                        "reason": reason.value,
                        "source": "compression",
                        "generation_strategy": "dual_llm",
                        "prompt": "context_archive",
                    },
                ),
                ArchiveWrite(
                    channel=ArchiveChannel.KNOWLEDGE,
                    summary=knowledge_summary,
                    metadata={
                        "reason": reason.value,
                        "source": "compression",
                        "generation_strategy": "dual_llm",
                        "prompt": "knowledge_archive",
                    },
                ),
            ),
            inputs=ArchiveGenerationInputs(
                context_transcript=context_summary,
                knowledge_transcript=knowledge_summary,
                stats=ArchiveInputStats(
                    input_messages=total_input,
                    context_messages=len(context_parts),
                    knowledge_messages=len(knowledge_parts),
                    tool_chains=0,
                    dropped_messages=0,
                ),
            ),
        )
```

Add helper methods to `DualLLMArchiveGenerationStrategy`:

```python
    def _segment(self, messages: Sequence[ArchiveInputMessage]) -> list[list[ArchiveInputMessage]]:
        """Split messages by user turn boundaries."""
        segments: list[list[ArchiveInputMessage]] = []
        current: list[ArchiveInputMessage] = []
        for msg in messages:
            if msg.role == "user" and current:
                segments.append(current)
                current = []
            current.append(msg)
        if current:
            segments.append(current)
        return segments

    def _sliding_window_merge(
        self, segments: list[list[ArchiveInputMessage]],
    ) -> list[list[ArchiveInputMessage]]:
        """Merge adjacent segments so each window ≤ max_segment_tokens."""
        if not segments:
            return []
        merged: list[list[ArchiveInputMessage]] = []
        window: list[ArchiveInputMessage] = []
        window_tokens = 0
        for seg in segments:
            seg_tokens = sum(len(m.content) // 4 + 10 for m in seg)
            if window and window_tokens + seg_tokens > self._max_segment_tokens:
                merged.append(window)
                window = []
                window_tokens = 0
            # A single segment over the limit still gets its own window
            if not window and seg_tokens > self._max_segment_tokens:
                merged.append(list(seg))
                continue
            window.extend(seg)
            window_tokens += seg_tokens
        if window:
            merged.append(window)
        return merged

    @staticmethod
    def _prompt_input(transcript: str, reason: CompressionReason) -> str:
        return f"## Compression Reason\n{reason.value}\n\n## Transcript\n{transcript.strip()}"
```

- [ ] **Step 8: Run all archive generation tests**

```bash
python -m pytest tests/unit/memory/test_archive_generation.py -v
```

Expected: 8 passed

- [ ] **Step 9: Commit**

```bash
git add tests/unit/memory/test_archive_generation.py framework/memory/archive_generation.py
git commit -m "refactor(memory): ArchiveGenerationStrategy Protocol→ABC, add ArchiveInputMessage and sliding window"
```

---

### Task 2: Move tool-chain sanitizer

**Files:**
- Create: `framework/memory/sanitizer.py`
- Delete: `framework/memory/compression/tool_chain_sanitizer.py`
- Create: `tests/unit/memory/test_sanitizer.py`

Move the sanitizer out of the compression directory. The sanitizer logic is unchanged, only the file location and Protocol removal.

- [ ] **Step 1: Write the test (moved from old location)**

Create `tests/unit/memory/test_sanitizer.py`:

```python
"""Tests for tool-chain sanitizer."""
from __future__ import annotations

import pytest

from framework.core.types import MessageRole
from framework.memory.sanitizer import (
    DefaultSessionToolChainSanitizer,
    ToolChainSanitizationMode,
)


def test_sanitizer_preserves_complete_tool_chain():
    sanitizer = DefaultSessionToolChainSanitizer()
    messages = [
        {"role": str(MessageRole.USER), "content": "q"},
        {
            "role": str(MessageRole.ASSISTANT),
            "content": "",
            "tool_calls": [{"id": "t1", "function": {"name": "read_file"}}],
        },
        {"role": str(MessageRole.TOOL), "tool_call_id": "t1", "content": "result"},
        {"role": str(MessageRole.ASSISTANT), "content": "answer"},
    ]
    result = sanitizer.sanitize(messages, mode=ToolChainSanitizationMode.PERSISTENT_SESSION)
    assert len(result.messages) == 4
    assert len(result.removed_messages) == 0


def test_sanitizer_removes_orphan_tool_result():
    sanitizer = DefaultSessionToolChainSanitizer()
    messages = [
        {"role": str(MessageRole.USER), "content": "q"},
        {"role": str(MessageRole.TOOL), "tool_call_id": "orphan", "content": "orphan result"},
        {"role": str(MessageRole.ASSISTANT), "content": "answer"},
    ]
    result = sanitizer.sanitize(messages, mode=ToolChainSanitizationMode.PERSISTENT_SESSION)
    assert len(result.messages) == 2
    removed_roles = [m["role"] for m in result.removed_messages]
    assert str(MessageRole.TOOL) in removed_roles


def test_sanitizer_preserves_active_open_tail():
    """Last assistant with tool_calls that is incomplete → preserved."""
    sanitizer = DefaultSessionToolChainSanitizer()
    messages = [
        {"role": str(MessageRole.USER), "content": "q"},
        {
            "role": str(MessageRole.ASSISTANT),
            "content": "",
            "tool_calls": [{"id": "t1", "function": {"name": "shell"}}],
        },
        {"role": str(MessageRole.TOOL), "tool_call_id": "t1", "content": "partial"},
    ]
    result = sanitizer.sanitize(messages, mode=ToolChainSanitizationMode.PERSISTENT_SESSION)
    # Active open tail preserved
    assert result.has_open_tail
    assert len(result.messages) == 3
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/unit/memory/test_sanitizer.py -v
```

Expected: FAIL — `framework.memory.sanitizer` module doesn't exist yet.

- [ ] **Step 3: Create framework/memory/sanitizer.py**

Copy the content of `framework/memory/compression/tool_chain_sanitizer.py`, changing:

1. Remove `Protocol` from `SessionToolChainSanitizer` (make it a class with docstring noting it's for duck-typing compatibility):

```python
"""Session tool-chain sanitization.

Removes invalid tool-chain records (orphan tool results, stale incomplete
tool_calls) from message sequences before session cleanup and archival.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from framework.core.types import MessageRole


class ToolChainSanitizationMode(StrEnum):
    PERSISTENT_SESSION = "persistent_session"
    MODEL_VISIBLE_CONTEXT = "model_visible_context"


class ToolChainSanitizationReason(StrEnum):
    ORPHAN_TOOL_RESULT = "orphan_tool_result"
    STALE_INCOMPLETE_ASSISTANT_TOOL_CALLS = "stale_incomplete_assistant_tool_calls"
    PARTIAL_TOOL_RESULTS_REMOVED = "partial_tool_results_removed"
    DUPLICATE_TOOL_RESULT = "duplicate_tool_result"


@dataclass(frozen=True)
class ToolChainSanitizationIssue:
    index: int
    role: MessageRole
    reason: ToolChainSanitizationReason
    tool_call_id: str | None = None
    assistant_index: int | None = None


@dataclass(frozen=True)
class ToolChainSanitizationResult:
    messages: list[dict[str, Any]]
    removed_messages: list[dict[str, Any]]
    removed_indices: set[int]
    issues: list[ToolChainSanitizationIssue]
    has_open_tail: bool = False
    open_tail_assistant_index: int | None = None


# SessionToolChainSanitizer removed — just use DefaultSessionToolChainSanitizer directly


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
        return bool(self.call_ids) and all(
            self.tool_indices_by_call_id.get(call_id) for call_id in self.call_ids
        )


class DefaultSessionToolChainSanitizer:
    """Tool-chain sanitizer. Removes invalid records from message sequences."""

    def sanitize(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        mode: ToolChainSanitizationMode,
    ) -> ToolChainSanitizationResult:
        # ... copy entire implementation from compression/tool_chain_sanitizer.py ...
        # (identical implementation, no logic changes)
```

2. Copy all the `sanitize`, `_last_tool_call_assistant_index`, `_has_plain_assistant_after`, `_collect_groups`, `_call_ids`, `_issue` methods from the old file unchanged.

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/unit/memory/test_sanitizer.py -v
```

Expected: 3 passed

- [ ] **Step 5: Update imports in existing files that reference the old location**

Don't do this yet — we'll update all imports in Task 8 after removing the old directories. For now, the old files still exist.

- [ ] **Step 6: Commit**

```bash
git add tests/unit/memory/test_sanitizer.py framework/memory/sanitizer.py
git commit -m "refactor(memory): move tool-chain sanitizer to framework/memory/sanitizer.py"
```

---

### Task 3: Write cleanup_session() — the core

**Files:**
- Create: `framework/memory/cleanup.py`
- Create: `tests/unit/memory/test_cleanup.py`

This is the main entry point. It replaces `MemoryCompressionCoordinator.maybe_compress()`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/memory/test_cleanup.py`:

```python
"""Tests for cleanup_session()."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from framework.core.types import MessageRole
from framework.memory.archive_generation import ArchiveGenerationStrategy, ArchiveInputMessage
from framework.memory.archive_models import (
    ArchiveChannel,
    ArchiveGenerationInputs,
    ArchiveGenerationResult,
    ArchiveInputStats,
    ArchiveWrite,
)
from framework.memory.cleanup import cleanup_session
from framework.memory.core.models import CompressionReason
from framework.memory.core.scope import MemoryContext
from framework.memory.layers.config import SessionMemoryConfig
from framework.memory.layers.factory import MemoryLayerFactory
from framework.memory.registry.in_memory import InMemoryStoreRegistry


@pytest.fixture
def registry():
    return InMemoryStoreRegistry()


def _make_session(registry, max_messages: int | None = None):
    from framework.memory.core.scope import MemoryLayerName
    from framework.memory.layers.session import ScopedSessionMemoryManager
    config = SessionMemoryConfig(max_messages=max_messages)
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.SESSION)
    return ScopedSessionMemoryManager(storage_factory=factory, config=config)


def _make_archive(registry):
    return MemoryLayerFactory.single_user(registry=registry).archive


class _CountingArchiveStrategy(ArchiveGenerationStrategy):
    """Records calls and succeeds/fails as configured."""
    def __init__(self, *, should_fail: bool = False) -> None:
        self.calls: list[list[ArchiveInputMessage]] = []
        self.should_fail = should_fail

    async def generate(
        self,
        messages: Sequence[ArchiveInputMessage],
        context: MemoryContext,
        reason: CompressionReason,
    ) -> ArchiveGenerationResult:
        self.calls.append(list(messages))
        if self.should_fail:
            raise RuntimeError("archive generation failed")
        return ArchiveGenerationResult(
            writes=(
                ArchiveWrite(channel=ArchiveChannel.CONTEXT, summary="ctx"),
                ArchiveWrite(channel=ArchiveChannel.KNOWLEDGE, summary="k"),
            ),
            inputs=ArchiveGenerationInputs(
                context_transcript="", knowledge_transcript="",
                stats=ArchiveInputStats(len(messages), 0, 0, 0, 0),
            ),
        )


# ── Trigger tests ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_trigger_when_under_limit(registry):
    session = _make_session(registry)
    ctx = MemoryContext(session_id="s1")
    await session.add_messages(ctx, [{"role": "user", "content": "hi"}])
    result = await cleanup_session(
        session=session, archive=None, context=ctx, max_messages=100,
    )
    assert not result.triggered


@pytest.mark.asyncio
async def test_trigger_when_over_message_limit(registry):
    session = _make_session(registry)
    ctx = MemoryContext(session_id="s2")
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(201)]
    await session.add_messages(ctx, msgs)
    result = await cleanup_session(
        session=session, archive=None, context=ctx, max_messages=200,
    )
    assert result.triggered
    assert result.cleaned
    remaining = len(await session.get_all_messages(ctx))
    assert remaining < 201, f"Session should be cleaned, still has {remaining}"


# ── Cleanup tests ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cleanup_always_executes(registry):
    """Cleanup MUST happen regardless of archive configuration."""
    session = _make_session(registry)
    ctx = MemoryContext(session_id="s3")
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(201)]
    await session.add_messages(ctx, msgs)

    # archive=None → no archival, but cleanup still happens
    result = await cleanup_session(
        session=session, archive=None, context=ctx, max_messages=200,
    )
    assert result.cleaned
    remaining = len(await session.get_all_messages(ctx))
    assert remaining < 201


@pytest.mark.asyncio
async def test_cleanup_removes_invalid_tool_chains(registry):
    session = _make_session(registry)
    ctx = MemoryContext(session_id="s4")
    await session.add_messages(ctx, [
        {"role": str(MessageRole.USER), "content": "start"},
        {"role": str(MessageRole.TOOL), "tool_call_id": "orphan", "content": "orphan"},
        {"role": str(MessageRole.USER), "content": "new"},
    ])
    # Trigger with low max_messages
    result = await cleanup_session(
        session=session, archive=None, context=ctx, max_messages=2,
    )
    assert result.cleaned
    remaining = [m.content for m in await session.get_all_messages(ctx)]
    assert "orphan" not in remaining


# ── Archive tests ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_archive_called_when_archive_present(registry):
    session = _make_session(registry)
    archive = _make_archive(registry)
    strategy = _CountingArchiveStrategy()
    ctx = MemoryContext(session_id="s5")
    await session.add_messages(ctx, [
        {"role": "user", "content": f"m{i}"} for i in range(201)
    ])

    result = await cleanup_session(
        session=session, archive=archive, context=ctx,
        archive_strategy=strategy, max_messages=200,
    )
    assert result.cleaned
    assert len(strategy.calls) == 1


@pytest.mark.asyncio
async def test_archive_skipped_when_archive_is_none(registry):
    session = _make_session(registry)
    strategy = _CountingArchiveStrategy()
    ctx = MemoryContext(session_id="s6")
    await session.add_messages(ctx, [
        {"role": "user", "content": f"m{i}"} for i in range(201)
    ])

    result = await cleanup_session(
        session=session, archive=None, context=ctx,
        archive_strategy=strategy, max_messages=200,
    )
    assert result.cleaned
    assert len(strategy.calls) == 0


# ── Failure counter tests ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_archive_failure_increments_counter(registry):
    session = _make_session(registry)
    archive = _make_archive(registry)
    strategy = _CountingArchiveStrategy(should_fail=True)
    ctx = MemoryContext(session_id="s7")
    await session.add_messages(ctx, [
        {"role": "user", "content": f"m{i}"} for i in range(201)
    ])

    result = await cleanup_session(
        session=session, archive=archive, context=ctx,
        archive_strategy=strategy, max_messages=200,
        archive_fail_threshold=3,
    )
    # Cleanup succeeds despite archive failure
    assert result.cleaned
    assert result.archive_fail_count == 1


@pytest.mark.asyncio
async def test_archive_skipped_after_consecutive_failures(registry):
    session = _make_session(registry)
    archive = _make_archive(registry)
    strategy = _CountingArchiveStrategy(should_fail=True)
    ctx = MemoryContext(session_id="s8")
    messages = [{"role": "user", "content": f"m{i}"} for i in range(201)]

    await session.add_messages(ctx, messages)
    # Fail 3 times (reaches threshold)
    for i in range(3):
        await cleanup_session(
            session=session, archive=archive, context=ctx,
            archive_strategy=strategy, max_messages=200,
            archive_fail_threshold=3,
        )
        # Re-fill session to trigger again
        await session.replace_messages(ctx, messages)

    # 4th call: archive should be skipped
    await session.replace_messages(ctx, messages)
    result = await cleanup_session(
        session=session, archive=archive, context=ctx,
        archive_strategy=strategy, max_messages=200,
        archive_fail_threshold=3,
    )
    assert result.cleaned
    assert result.archive_skipped  # archive was skipped, not called
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/unit/memory/test_cleanup.py -v
```

Expected: FAIL — `framework.memory.cleanup` module doesn't exist.

- [ ] **Step 3: Create framework/memory/cleanup.py**

```python
"""Session cleanup and archival — direct call, no callback injection.

Entry point is ``cleanup_session()``, called directly from
``ScopedMessageHistory`` after every message append.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from framework.memory.archive_generation import (
    ArchiveGenerationStrategy,
    ArchiveInputMessage,
)
from framework.memory.core.layers import ArchiveMemoryManager, SessionMemoryManager
from framework.memory.core.models import CompressionReason, CompressionResultReason
from framework.memory.core.scope import MemoryContext
from framework.memory.sanitizer import (
    DefaultSessionToolChainSanitizer,
    ToolChainSanitizationMode,
)

logger = logging.getLogger(__name__)


@dataclass
class CleanupResult:
    triggered: bool = False
    cleaned: bool = False
    archived: bool = False
    archive_skipped: bool = False
    archive_fail_count: int = 0


# Per-session archive failure counter.  Keyed by session_id.
# In-memory only — resets on process restart.
_archive_fail_counters: dict[str, int] = {}


async def cleanup_session(
    *,
    session: SessionMemoryManager,
    archive: ArchiveMemoryManager | None,
    context: MemoryContext,
    archive_strategy: ArchiveGenerationStrategy | None = None,
    max_messages: int = 200,
    max_tokens: int = 100000,
    keep_ratio: float = 0.5,
    archive_fail_threshold: int = 3,
) -> CleanupResult:
    """Clean up session (always) and optionally archive pruned messages.

    Cleanup is unconditional once triggered.  Archive is attempted only
    when ``archive is not None`` and an ``archive_strategy`` is provided.
    After ``archive_fail_threshold`` consecutive failures, archive is
    skipped and the counter resets.
    """
    # ── Step 1: Trigger check ──────────────────────────────────────────
    all_msgs_raw = await session.get_all_messages(context)
    if not all_msgs_raw:
        return CleanupResult()

    all_msgs = [m.to_dict() if hasattr(m, "to_dict") else dict(m) for m in all_msgs_raw]

    msg_count = len(all_msgs)
    est_tokens = _estimate_tokens(all_msgs)

    if msg_count <= max_messages and est_tokens <= max_tokens:
        return CleanupResult()

    result = CleanupResult(triggered=True)

    # ── Step 2: Clean session ──────────────────────────────────────────
    sanitizer = DefaultSessionToolChainSanitizer()
    sanitization = sanitizer.sanitize(
        all_msgs, mode=ToolChainSanitizationMode.PERSISTENT_SESSION,
    )
    sanitized = sanitization.messages
    if not sanitized:
        return result

    # Compute keep boundary
    keep_boundary = _compute_keep_boundary(sanitized, keep_ratio, msg_count)
    keep = sanitized[keep_boundary:]
    pruned = sanitized[:keep_boundary]

    # Always replace session
    revision = await session.get_revision(context)
    new_revision = await session.replace_messages_if_revision(
        context, keep, revision,
    )
    if new_revision is not None:
        result.cleaned = True

    # ── Step 3: Archive (optional) ─────────────────────────────────────
    if archive is None or archive_strategy is None:
        return result

    session_id = context.session_id
    fail_count = _archive_fail_counters.get(session_id, 0)

    if fail_count >= archive_fail_threshold:
        logger.info(
            "Archive skipped: %d consecutive failures for session=%s",
            fail_count, session_id,
        )
        _archive_fail_counters[session_id] = 0
        result.archive_skipped = True
        return result

    try:
        archive_input = [ArchiveInputMessage.from_dict(m) for m in pruned]
        gen_result = await archive_strategy.generate(
            archive_input, context,
            CompressionReason.MESSAGE_COUNT if msg_count > max_messages else CompressionReason.TOKEN_PRESSURE,
        )
        if gen_result.writes:
            await archive.append_bundle(context, gen_result.writes)
            result.archived = True
        _archive_fail_counters[session_id] = 0
    except Exception:
        fail_count = _archive_fail_counters.get(session_id, 0) + 1
        _archive_fail_counters[session_id] = fail_count
        result.archive_fail_count = fail_count
        logger.warning(
            "Archive generation failed for session=%s (attempt %d/%d)",
            session_id, fail_count, archive_fail_threshold, exc_info=True,
        )

    return result


def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate token count for a list of messages."""
    try:
        from framework.memory.utils import estimate_token_count
        return estimate_token_count(messages)
    except Exception:
        return sum(len(str(m)) // 4 for m in messages)


def _compute_keep_boundary(
    messages: list[dict[str, Any]],
    keep_ratio: float,
    total_count: int,
) -> int:
    """Compute the index where messages[:boundary] are pruned, messages[boundary:] are kept.

    Strategy:
    1. Walk backward from the end, counting messages.
    2. Stop when we've reached keep_target messages.
    3. Never split a tool-call chain (walk forward past tool results).
    4. Always keep the most recent user message as anchor.
    """
    keep_target = max(1, int(total_count * keep_ratio))
    accumulated = 0
    boundary = len(messages)

    for i in range(len(messages) - 1, -1, -1):
        accumulated += 1
        if accumulated >= keep_target:
            boundary = i
            break

    # Never split a tool chain: if boundary is inside a tool group, pull back
    boundary = _align_boundary_backward(messages, boundary)

    # Ensure last user message is in the keep set
    boundary = _anchor_last_user(messages, boundary)

    return max(0, boundary)


def _align_boundary_backward(
    messages: list[dict[str, Any]], boundary: int,
) -> int:
    """If boundary splits a tool_result from its assistant, move boundary before the assistant."""
    if boundary <= 0 or boundary >= len(messages):
        return boundary
    check = boundary - 1
    while check >= 0 and messages[check].get("role") == "tool":
        check -= 1
    if check >= 0 and messages[check].get("role") == "assistant" and messages[check].get("tool_calls"):
        return check
    return boundary


def _anchor_last_user(
    messages: list[dict[str, Any]], boundary: int,
) -> int:
    """Ensure the most recent user message stays in the keep set."""
    for i in range(len(messages) - 1, boundary - 1, -1):
        if messages[i].get("role") == "user":
            return boundary  # Already in keep set
    # Last user is in prune set — move boundary to include it
    for i in range(boundary - 1, -1, -1):
        if messages[i].get("role") == "user":
            return i
    return boundary
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/unit/memory/test_cleanup.py -v
```

Expected: 8 passed (or fewer if some need debugging — iterate until all pass)

- [ ] **Step 5: Commit**

```bash
git add tests/unit/memory/test_cleanup.py framework/memory/cleanup.py
git commit -m "feat(memory): add cleanup_session() — direct call, cleanup always executes"
```

---

### Task 4: Wire cleanup_session into ScopedMessageHistory

**Files:**
- Modify: `framework/memory/default_system.py`

Replace the callback injection with a direct call to `cleanup_session`.

- [ ] **Step 1: Modify ScopedMessageHistory**

In `framework/memory/default_system.py`, replace `ScopedMessageHistory`:

```python
class ScopedMessageHistory(MessageHistory):
    """MessageHistory backed by a registry-scoped SessionMemoryManager.

    Calls ``cleanup_session()`` after every ``append`` / ``extend``
    so session memory is pruned on the ReAct-turn hot path.
    """

    def __init__(
        self,
        manager: Any,  # SessionMemoryManager
        context: MemoryContext,
        initial_messages: Sequence[ChatMessage | dict[str, Any]] | None = None,
        recorder: MemoryAppendRecorder | None = None,
        archive_manager: Any = None,               # NEW
        archive_strategy: Any = None,              # NEW
        cleanup_config: dict[str, Any] | None = None,  # NEW
    ) -> None:
        self._manager = manager
        self._context = context
        self._recorder = recorder
        self._archive_manager = archive_manager
        self._archive_strategy = archive_strategy
        self._cleanup_config = cleanup_config or {}
        self._cache: list[ChatMessage] | None = (
            [ChatMessage.coerce(m) for m in initial_messages]
            if initial_messages is not None
            else None
        )
        self._cache_lock = asyncio.Lock()

    async def _run_cleanup(self) -> None:
        from framework.memory.cleanup import cleanup_session
        await cleanup_session(
            session=self._manager,
            archive=self._archive_manager,
            context=self._context,
            archive_strategy=self._archive_strategy,
            **self._cleanup_config,
        )

    async def append(self, message: ChatMessage | dict[str, Any]) -> None:
        revision = await self._manager.add_messages(self._context, [message])
        if self._recorder is not None:
            await self._recorder.record([message], self._context)
        await self._run_cleanup()
        async with self._cache_lock:
            self._cache = None

    async def extend(self, messages: Sequence[ChatMessage | dict[str, Any]]) -> None:
        if not messages:
            return
        revision = await self._manager.add_messages(self._context, list(messages))
        if self._recorder is not None:
            await self._recorder.record(list(messages), self._context)
        await self._run_cleanup()
        async with self._cache_lock:
            self._cache = None

    # ... to_list, clear, replace_all unchanged ...
```

- [ ] **Step 2: Modify DefaultMemorySystem.create_message_history**

Replace the callback-creation logic with direct archive/strategy passing:

```python
    def create_message_history(
        self,
        context: MemoryContext,
        initial_messages: Sequence[ChatMessage | dict[str, Any]] | None = None,
    ) -> MessageHistory:
        archive_mgr = self._layers.archive
        strategy = getattr(self, "_archive_strategy", None)

        return ScopedMessageHistory(
            manager=self._layers.session,
            context=context,
            initial_messages=initial_messages,
            recorder=self._recorder,
            archive_manager=archive_mgr,
            archive_strategy=strategy,
            cleanup_config=self._cleanup_config or {},
        )
```

- [ ] **Step 3: Modify DefaultMemorySystem.__init__**

Replace `lifecycle_policy` with `archive_strategy` and `cleanup_config`:

```python
    def __init__(
        self,
        *,
        layer_set: MemoryLayerSet,
        store_registry: MemoryStoreRegistry,
        providers: Any | None = None,
        archive_strategy: ArchiveGenerationStrategy | None = None,
        cleanup_config: dict[str, Any] | None = None,
        maintenance_policy: Any | None = None,  # MemoryMaintenancePolicy
    ) -> None:
        self._layers = layer_set
        self._registry = store_registry
        self._providers = providers
        self._archive_strategy = archive_strategy
        self._cleanup_config = cleanup_config or {}
        self._maintenance_policy = maintenance_policy
        self._recorder = MemoryAppendRecorder()
        if providers is not None:
            for provider in providers.all():
                self._recorder.add_provider(provider)
```

- [ ] **Step 4: Remove old helper methods and the compression_coordinator property**

Delete from `DefaultMemorySystem`:
- `compression_coordinator` property
- `get_auto_compact_summary` method
- `get_compression_summary` method (if only used by old compression code)

- [ ] **Step 5: Run all memory tests to find breakage**

```bash
python -m pytest tests/unit/memory/ -q --tb=short 2>&1 | tail -30
```

Fix any test that breaks due to the signature change. Most breakage will be in tests that create `DefaultMemorySystem` with `lifecycle_policy=` — we'll update those in Task 11.

- [ ] **Step 6: Commit**

```bash
git add framework/memory/default_system.py
git commit -m "refactor(memory): wire cleanup_session directly into ScopedMessageHistory"
```

---

### Task 5: Clean up memory/system.py and memory/core/system.py

**Files:**
- Modify: `framework/memory/system.py`
- Modify: `framework/memory/core/system.py`

- [ ] **Step 1: Update create_memory_system()**

In `framework/memory/system.py`, change signature:

```python
def create_memory_system(
    workspace: Path,
    config: MemoryLayerConfigSet | None = None,
    llm_provider: Any | None = None,
    session_only: bool = False,
    archive_strategy: Any | None = None,       # NEW, replaces lifecycle_policy
    cleanup_config: dict[str, Any] | None = None,  # NEW
    maintenance_policy: Any | None = None,     # NEW
) -> DefaultMemorySystem:
    registry = DefaultMemoryStoreRegistry(workspace)
    if session_only:
        session_config = config.session if config else None
        pending_config = config.pending if config else None
        layer_set = MemoryLayerFactory.session_only(
            registry=registry,
            config=session_config,
            pending_config=pending_config,
        )
    else:
        layer_set = MemoryLayerFactory.single_user(
            registry=registry,
            config=config,
            llm_provider=llm_provider,
        )
    return DefaultMemorySystem(
        layer_set=layer_set,
        store_registry=registry,
        archive_strategy=archive_strategy,
        cleanup_config=cleanup_config,
        maintenance_policy=maintenance_policy,
    )
```

- [ ] **Step 2: Remove get_auto_compact_summary from core/system.py**

In `framework/memory/core/system.py`:
- Remove `get_auto_compact_summary` from `InjectableMemorySystem` Protocol
- Remove `get_compression_summary` from `InjectableMemorySystem` Protocol (if present)

- [ ] **Step 3: Commit**

```bash
git add framework/memory/system.py framework/memory/core/system.py
git commit -m "refactor(memory): remove lifecycle_policy from create_memory_system, remove legacy system interfaces"
```

---

### Task 6: Clean up lifecycle.py — keep only MemoryMaintenancePolicy

**Files:**
- Modify: `framework/memory/lifecycle.py`

- [ ] **Step 1: Remove MemoryLifecyclePolicy and DefaultMemoryLifecyclePolicy**

Delete these classes from `framework/memory/lifecycle.py`:
- `MemoryLifecyclePolicy` (ABC and class)
- `DefaultMemoryLifecyclePolicy` (class)
- `_normalize_role` helper (used only by `DefaultMemoryLifecyclePolicy.on_session_end`)
- `MemoryAgentRole` import (if only used by lifecycle policy)
- `MessageRole` import (if only used by lifecycle policy)

Keep:
- `MaintenanceResult` dataclass
- `MemoryMaintenancePolicy` ABC
- `DefaultMemoryMaintenancePolicy`
- `SessionRetentionPolicy` ABC + `DefaultSessionRetentionPolicy`
- `ArchiveRetentionPolicy` ABC + `DefaultArchiveRetentionPolicy`
- `KnowledgeRetentionPolicy` ABC + `DefaultKnowledgeRetentionPolicy`

- [ ] **Step 2: Remove lifecycle import from framework/memory/__init__.py**

Remove `MemoryLifecyclePolicy` and `DefaultMemoryLifecyclePolicy` from `framework/memory/__init__.py` exports.

- [ ] **Step 3: Commit**

```bash
git add framework/memory/lifecycle.py framework/memory/__init__.py
git commit -m "refactor(memory): remove MemoryLifecyclePolicy, keep only MemoryMaintenancePolicy"
```

---

### Task 7: Delete old compression, compaction, retention directories

**Files:**
- Delete: `framework/memory/compression/`
- Delete: `framework/memory/compaction/`
- Delete: `framework/memory/retention/`

But DON'T delete `tool_chain_sanitizer.py` if it's already been moved to `sanitizer.py`. Actually the whole `compression/` directory goes away.

- [ ] **Step 1: Delete the directories**

```bash
rm -rf framework/memory/compression/
rm -rf framework/memory/compaction/
rm -rf framework/memory/retention/
```

- [ ] **Step 2: Clean up framework/memory/__init__.py**

Remove all imports from the deleted directories. Remove exports for:
- `MemoryCompressionCoordinator`, `DefaultMemoryCompressionCoordinator`
- `CompressionTriggerPolicy`, `DefaultCompressionTriggerPolicy`
- `CommitPolicy`, `DefaultCommitPolicy`
- `CompressionErrorPolicy`, `DefaultCompressionErrorPolicy`
- `SummaryStrategy`
- `BoundaryPolicy`, `ToolChainBoundaryPolicy`, `BoundaryPolicyName`
- `MessageCompactionPolicy`, `ConservativeCompactionPolicy`
- `MessageRetentionPolicy`, `DefaultMessageRetentionPolicy`
- `CompressionKeepPlanner`, `PriorityCompressionKeepPlanner`
- `MemoryLifecyclePolicy`, `DefaultMemoryLifecyclePolicy`
- All `Compression*`, all `Compaction*`, all `Retention*` types

- [ ] **Step 3: Commit**

```bash
git add -A framework/memory/
git commit -m "refactor(memory): delete compression/, compaction/, retention/ directories"
```

---

### Task 8: Clean up framework imports and references

**Files to check and fix:**
- `framework/memory/__init__.py`
- `framework/memory/core/models.py`
- `framework/memory/context_governance.py`
- `framework/memory/injection/filter.py`
- `framework/memory/layers/config.py`
- `framework/agents/summarizer/strategy.py`
- `framework/interceptor/abc.py`
- `framework/ioc/configs/memory.py`
- `framework/ioc/factories/memory.py`
- `framework/ioc/factories/compression.py`
- `framework/ioc/factories/descriptors.py`

- [ ] **Step 1: Fix framework/interceptor/abc.py**

Remove `MEMORY_OPERATION = "memory_operation"` from `InterceptorScope` enum.

- [ ] **Step 2: Fix framework/memory/core/models.py**

Remove data classes only used by deleted code (check each with grep):
- `CompressionPlan`, `CompressionTrigger`, `CompressionResult`, `CompressionResultReason` — these may still be needed by `archive_generation.py` (uses `CompressionReason`). Keep types still referenced.
- `ArchiveEntry`, `UnprocessedResult`, `LongTermMemory`, `StorageRevision` — keep (still used by layers).
- `MemoryContextBundle` — remove `auto_compact_summary` field, keep `compression_summary` if still used.

- [ ] **Step 3: Fix framework/memory/context_governance.py**

Remove `from framework.memory.retention...` imports. Check which retention types `TokenBudgetGovernance` uses — if it references `MessageRetentionDecision` or `RetentionPriority`, inline the needed types into governance.py or a shared types file.

- [ ] **Step 4: Fix framework/memory/injection/filter.py**

Remove `from framework.memory.compaction.policy import ...` imports. Replace with equivalent logic inline.

- [ ] **Step 5: Fix framework/agents/summarizer/strategy.py**

Remove `from framework.memory.compaction.policy import ...` imports and inline needed types.

- [ ] **Step 6: Fix framework/memory/layers/config.py**

Remove lifecycle-related comments that reference `DefaultMemoryLifecyclePolicy.on_messages_added`.

- [ ] **Step 7: Run all framework tests to verify no import errors**

```bash
python -m pytest tests/unit/memory/ -q --tb=short 2>&1 | tail -10
```

Fix any import errors found.

- [ ] **Step 8: Commit**

```bash
git add framework/
git commit -m "refactor(memory): clean up imports and references to deleted modules"
```

---

### Task 9: Update IOC configs and factories

**Files:**
- Modify: `framework/ioc/configs/memory.py`
- Modify: `framework/ioc/factories/memory.py`
- Simplify/Delete: `framework/ioc/factories/compression.py`
- Modify: `framework/ioc/factories/descriptors.py`

- [ ] **Step 1: Remove auto_compact from ShortTermConfig**

In `framework/ioc/configs/memory.py`, remove the `auto_compact` field:

```python
class ShortTermConfig(BaseModel):
    max_messages: int = 100
    max_tokens: int = 100000
    keep_ratio_for_messages: float = 0.4
    keep_ratio_for_token: float = 0.4
    # auto_compact: bool = False  ← REMOVED
```

- [ ] **Step 2: Update IOC memory factory**

In `framework/ioc/factories/memory.py`, replace the `create_memory` function:

```python
def create_memory(
    cfg: MemoryConfig,
    llm_provider: LLMProvider,
    workspace: Path,
) -> object:
    from framework.memory.system import create_memory_system

    layer_config = _build_memory_layer_config(cfg)

    # Always create archive generation strategy (archive existence gates usage)
    archive_generation = None
    if llm_provider is not None:
        from framework.agents.summarizer import SummarizerAgent
        from framework.memory.archive_generation import DualLLMArchiveGenerationStrategy
        summarizer = SummarizerAgent(llm_provider)
        archive_generation = DualLLMArchiveGenerationStrategy(summarizer=summarizer)

    st = cfg.short_term
    cleanup_config = {
        "max_messages": st.max_messages,
        "max_tokens": st.max_tokens,
        "keep_ratio": st.keep_ratio_for_messages,
    }

    return create_memory_system(
        workspace=workspace,
        config=layer_config,
        llm_provider=llm_provider,
        archive_strategy=archive_generation,
        cleanup_config=cleanup_config,
    )
```

- [ ] **Step 3: Delete or simplify compression factory**

File `framework/ioc/factories/compression.py` — the `create_subagent_compression_coordinator` function is no longer needed. If it's still imported anywhere, replace callers with simplified logic. Then delete the file.

- [ ] **Step 4: Update descriptors factory**

In `framework/ioc/factories/descriptors.py`, remove references to `compression_coordinator` and `MemoryLifecyclePolicy`. Any code that builds agent descriptors should use `archive_strategy` and `cleanup_config` instead.

- [ ] **Step 5: Run IOC tests**

```bash
python -m pytest tests/unit/ioc/ -q --tb=short 2>&1 | tail -10
```

Fix any failures.

- [ ] **Step 6: Commit**

```bash
git add framework/ioc/
git commit -m "refactor(ioc): remove auto_compact, always create archive strategy, simplify factories"
```

---

### Task 10: Update bot project

**Files:**
- Modify: `examples/bot_project/bot/service/core.py`
- Modify: `examples/bot_project/bot/service/builders.py`
- Modify: `examples/bot_project/config/pools/main.yml`
- Modify: `examples/bot_project/config/pools/coding.yml`
- Modify: `examples/bot_project/plugins/mem0_memory/provider.py`

- [ ] **Step 1: Update config YAML — remove auto_compact**

In `examples/bot_project/config/pools/main.yml`:
- Remove `auto_compact: false` from pool-level `memory.short_term` (line ~24)
- Remove `auto_compact: false` from agent-level `memory.short_term` (line ~47)

In `examples/bot_project/config/pools/coding.yml`:
- Add `auto_compact` to search, remove if present

In any template `.yml` files: same.

- [ ] **Step 2: Update core.py — rename _auto_compact_task**

In `examples/bot_project/bot/service/core.py`:
- Rename field `self._auto_compact_task` → `self._maintenance_task`
- Rename method `_auto_compact_loop` → `_maintenance_loop`
- Rename method `_init_auto_compact` → `_init_maintenance_task`
- Update `stop()` to cancel `self._maintenance_task`
- Update print messages: "AutoCompactService" → "MaintenanceService"
- Remove any usage of `DefaultMemoryLifecyclePolicy` when creating the main agent memory — replace with `archive_strategy` + `cleanup_config` creation

- [ ] **Step 3: Update builders.py**

In `examples/bot_project/bot/service/builders.py`:
- In `_create_subagent_memory`: remove `DefaultMemoryLifecyclePolicy` import and usage
- Remove `from framework.memory.lifecycle import DefaultMemoryLifecyclePolicy`
- Replace `lifecycle_policy=DefaultMemoryLifecyclePolicy(compression_coordinator=coordinator)` with `cleanup_config={...}` and remove coordinator creation

- [ ] **Step 4: Update mem0 provider**

In `examples/bot_project/plugins/mem0_memory/provider.py`:
- Remove `from framework.memory.compaction.policy import ...` import if present

- [ ] **Step 5: Run bot tests**

```bash
python -m pytest tests/unit/bot/ -q --tb=short 2>&1 | tail -10
```

Fix any failures.

- [ ] **Step 6: Commit**

```bash
git add examples/bot_project/
git commit -m "refactor(bot): remove auto_compact config, rename _auto_compact_task to _maintenance_task"
```

---

### Task 11: Update all remaining tests

**Files:**
- Delete: `tests/unit/memory/retention/`
- Delete: `tests/unit/memory/compression/`
- Rewrite: `tests/unit/memory/test_compression_policies.py` (rename to `test_cleanup_integration.py`)
- Rewrite: `tests/unit/memory/test_lifecycle.py`
- Update: `tests/unit/memory/test_bot_project_memory_pipeline.py`
- Update: `tests/unit/memory/test_summarizer_integration.py`
- Update: `tests/unit/ioc/test_memory_factory.py`
- Update: `tests/unit/memory/core/test_default_system.py`

- [ ] **Step 1: Delete retention and compression test directories**

```bash
rm -rf tests/unit/memory/retention/
rm -rf tests/unit/memory/compression/
```

- [ ] **Step 2: Rename test_compression_policies.py → test_cleanup_integration.py**

Keep only the tests that test `cleanup_session` behavior. The tests we wrote in Task 3 (`test_cleanup.py`) are the main unit tests. Move any useful integration tests from the old file to the new file.

At minimum, keep these tests (adapting to new API):
- Session cleaning always happens even when archive generation is empty (was `test_coordinator_cleans_session_when_archive_generation_is_empty`)
- Session cleaning when no archive generation strategy (was `test_coordinator_cleans_session_without_archive_generation_strategy`)
- Tool-chain splitting protection
- Archive entries written when everything works
- Test from the bug report: session MUST be cleaned when over limit

Remove tests that test deleted ABCs directly.

- [ ] **Step 3: Rewrite test_lifecycle.py**

Remove all `MemoryLifecyclePolicy` tests. Keep only `MemoryMaintenancePolicy` and retention policy tests. Add tests for `scan_once` with proper mocks.

- [ ] **Step 4: Update test_bot_project_memory_pipeline.py**

Replace references to `MemoryCompressionCoordinator` with archive strategy setup via `cleanup_config`. Update `_bot_project_coordinator` helper function to create archive strategy instead.

- [ ] **Step 5: Update test_summarizer_integration.py**

Replace `MemoryCompressionCoordinator` creation with `cleanup_session` calls. Update `MockSummarizerAgent` usage.

- [ ] **Step 6: Update test_memory_factory.py**

Remove all `auto_compact` tests (`test_auto_compact_false_*`, `test_auto_compact_true_*`, `test_auto_compact_false_default`). Add tests that verify `archive_strategy` is always created when `llm_provider` is provided.

- [ ] **Step 7: Update test_default_system.py**

Replace `lifecycle_policy=` with `archive_strategy=` and `cleanup_config=` in system creation.

- [ ] **Step 8: Run all memory tests**

```bash
python -m pytest tests/unit/memory/ -q --tb=short 2>&1 | tail -15
```

Expected: all passing.

- [ ] **Step 9: Commit**

```bash
git add tests/
git commit -m "test(memory): update tests for cleanup_session, remove deleted module tests"
```

---

### Task 12: Final integration verification

- [ ] **Step 1: Run full test suite**

```bash
python -m pytest tests/unit/ -q --tb=short 2>&1 | tail -10
```

Expected: all passing (except any pre-existing failures).

- [ ] **Step 2: Verify no stale imports**

```bash
python -c "import framework.memory; print('OK')"
python -c "from framework.memory.cleanup import cleanup_session; print('OK')"
python -c "from framework.memory.archive_generation import ArchiveGenerationStrategy, DualLLMArchiveGenerationStrategy, ArchiveInputMessage; print('OK')"
python -c "from framework.memory.sanitizer import DefaultSessionToolChainSanitizer; print('OK')"
```

Expected: all "OK", no import errors.

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "chore: final cleanup, remove stale imports"
```
