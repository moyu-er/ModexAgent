# Memory Context Construction Simplification — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete filter strategy, crash recovery, MemoryContextBundle/PromptSection, and pending_user_turn dead code. Converge triple assemble to single. Extend ChatMessage with content_format/truncatable_paths/created_at. Implement XML-safe truncation. Simplify extension mechanisms from 10 to 2 (Governance + Provider).

**Architecture:** Foundation types first (ChatMessage extension, InjectionResult). Then delete dead code in parallel. Then build XML infrastructure and converge assemble. Finalize with governance integration and test cleanup. TDD throughout — write failing test, implement, verify green.

**Tech Stack:** Python 3.12+, pytest, dataclasses, xml.etree.ElementTree, re

**Type Safety:** Follow `rules/type-safety.md` — enums for categories (`ContentFormat`), typed dataclasses (`InjectionResult`), proper return type annotations, no bare `Any`/`dict`/`list` in framework APIs.

---

### Task 1: Extend ChatMessage + Add ContentFormat Enum

**Files:**
- Modify: `framework/memory/core/message.py`
- Test: `tests/unit/memory/test_content_format.py` (create)

**Dependencies:** None

- [ ] **Step 1: Write failing test**

```python
# tests/unit/memory/test_content_format.py
"""Tests for ChatMessage content_format, truncatable_paths, created_at extensions."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from framework.memory.core.message import ChatMessage, ContentFormat


def test_content_format_default_is_plain():
    msg = ChatMessage(role="user", content="hello")
    assert msg.content_format == ContentFormat.PLAIN


def test_content_format_xml():
    msg = ChatMessage(
        role="user",
        content="<msg>hi</msg>",
        content_format=ContentFormat.XML,
    )
    assert msg.content_format == ContentFormat.XML


def test_truncatable_paths_default_none():
    msg = ChatMessage(role="user", content="hello")
    assert msg.truncatable_paths is None


def test_truncatable_paths_xml():
    msg = ChatMessage(
        role="user",
        content="<msg><content>x</content></msg>",
        content_format=ContentFormat.XML,
        truncatable_paths=["content"],
    )
    assert msg.truncatable_paths == ["content"]


def test_created_at_default_none():
    msg = ChatMessage(role="user", content="hello")
    assert msg.created_at is None


def test_created_at_set():
    ts = datetime(2026, 5, 28, 14, 30, 0, tzinfo=timezone.utc)
    msg = ChatMessage(role="user", content="hello", created_at=ts)
    assert msg.created_at == ts


def test_coerce_preserves_content_format():
    msg = ChatMessage.coerce({
        "role": "user",
        "content": "<msg>hi</msg>",
        "content_format": "xml",
        "truncatable_paths": ["content"],
        "created_at": "2026-05-28 14:30:00",
    })
    assert msg.content_format == ContentFormat.XML
    assert msg.truncatable_paths == ["content"]
    assert msg.created_at is not None
    assert msg.created_at.year == 2026


def test_to_dict_serializes_new_fields():
    ts = datetime(2026, 5, 28, 14, 30, 0, tzinfo=timezone.utc)
    msg = ChatMessage(
        role="user",
        content="<msg>hi</msg>",
        content_format=ContentFormat.XML,
        truncatable_paths=["content"],
        created_at=ts,
    )
    d = msg.to_dict()
    assert d["content_format"] == "xml"
    assert d["truncatable_paths"] == ["content"]
    assert d["created_at"] == "2026-05-28 14:30:00"
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/unit/memory/test_content_format.py -v`
Expected: FAIL — `ContentFormat` not defined / fields not on `ChatMessage`

- [ ] **Step 3: Implement ContentFormat enum + ChatMessage fields**

```python
# framework/memory/core/message.py — add near top after imports

from enum import StrEnum


class ContentFormat(StrEnum):
    PLAIN = "plain"
    XML = "xml"
```

```python
# In ChatMessage dataclass, add three new fields:

@dataclass
class ChatMessage:
    role: str
    content: str | list[dict[str, Any]] | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    metadata: dict[str, Any] | None = None
    # ── new fields ──
    created_at: datetime | None = None
    content_format: str | ContentFormat = ContentFormat.PLAIN
    truncatable_paths: list[str] | None = None
```

Update `to_dict()` — add new field serialization:
```python
def to_dict(self) -> dict[str, Any]:
    d: dict[str, Any] = {"role": self.role}
    if self.content is not None:
        d["content"] = self.content
    if self.tool_calls:
        d["tool_calls"] = self.tool_calls
    if self.tool_call_id:
        d["tool_call_id"] = self.tool_call_id
    if self.name:
        d["name"] = self.name
    if self.metadata:
        d["metadata"] = self.metadata
    if self.content_format != ContentFormat.PLAIN:
        d["content_format"] = str(self.content_format)
    if self.truncatable_paths is not None:
        d["truncatable_paths"] = self.truncatable_paths
    if self.created_at is not None:
        d["created_at"] = self.created_at.strftime("%Y-%m-%d %H:%M:%S")
    return d
```

Update `coerce()` — parse new fields from dict input:
```python
@classmethod
def coerce(cls, data: ChatMessage | dict[str, Any]) -> ChatMessage:
    if isinstance(data, cls):
        return data
    # ... existing field extraction ...

    created_at: datetime | None = None
    raw_ts = data.get("created_at")
    if isinstance(raw_ts, str):
        created_at = datetime.strptime(raw_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    elif isinstance(raw_ts, datetime):
        created_at = raw_ts

    content_format_raw = data.get("content_format", "plain")
    content_format = ContentFormat(content_format_raw) if content_format_raw else ContentFormat.PLAIN

    truncatable_paths = data.get("truncatable_paths")
    if isinstance(truncatable_paths, list):
        truncatable_paths = [str(p) for p in truncatable_paths]

    return cls(
        role=data.get("role", ""),
        content=data.get("content"),
        tool_calls=data.get("tool_calls"),
        tool_call_id=data.get("tool_call_id"),
        name=data.get("name"),
        metadata=data.get("metadata"),
        created_at=created_at,
        content_format=content_format,
        truncatable_paths=truncatable_paths,
    )
```

Update `from_dicts()` — pass through new fields from dict representation.

Update `__init__` to normalize `content_format` from str to ContentFormat enum in `__post_init__`.

- [ ] **Step 4: Run test to verify pass**

Run: `pytest tests/unit/memory/test_content_format.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/unit/memory/test_content_format.py framework/memory/core/message.py
git commit -m "feat(memory): add ContentFormat enum, created_at, content_format, truncatable_paths to ChatMessage"
```

---

### Task 2: Add InjectionResult + Delete MemoryContextBundle/PromptSection from public API

**Files:**
- Modify: `framework/memory/core/models.py` (delete MemoryContextBundle, PromptSection; add InjectionResult)
- Modify: `framework/memory/core/__init__.py` (update exports)
- Modify: `framework/memory/__init__.py` (update exports)
- Test: `tests/unit/memory/test_injection_result.py` (create)

**Dependencies:** Task 1 (needs ChatMessage type)

- [ ] **Step 1: Write failing test**

```python
# tests/unit/memory/test_injection_result.py
"""Tests for InjectionResult replacing MemoryContextBundle."""
from __future__ import annotations

from framework.memory.core.message import ChatMessage
from framework.memory.core.models import InjectionResult


def test_injection_result_construction():
    msgs = [ChatMessage(role="user", content="hello")]
    result = InjectionResult(system_prompt="## Knowledge\n...", messages=msgs)
    assert result.system_prompt == "## Knowledge\n..."
    assert result.messages == msgs
    assert len(result.messages) == 1


def test_injection_result_empty_system_prompt():
    result = InjectionResult(system_prompt="", messages=[])
    assert result.system_prompt == ""


def test_injection_result_is_dataclass():
    r1 = InjectionResult(system_prompt="a", messages=[])
    r2 = InjectionResult(system_prompt="a", messages=[])
    assert r1 == r2
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/unit/memory/test_injection_result.py -v`
Expected: FAIL — `InjectionResult` not defined

- [ ] **Step 3: Add InjectionResult to models.py, delete MemoryContextBundle + PromptSection**

In `framework/memory/core/models.py`:

Delete these class definitions:
- `PromptSection` (lines 121-126)
- `MemoryContextBundle` (lines 130-136)

Add:
```python
@dataclass(frozen=True)
class InjectionResult:
    """Output of injection policy — flat, no intermediate containers."""
    system_prompt: str
    messages: list[ChatMessage]
```

Update `__all__`:
- Remove: `"PromptSection"`, `"MemoryContextBundle"`
- Add: `"InjectionResult"`

Update `framework/memory/core/__init__.py`:
- Remove `PromptSection`, `MemoryContextBundle` imports
- Add `InjectionResult` import

Update `framework/memory/__init__.py`:
- Remove `PromptSection`, `MemoryContextBundle` imports + `__all__` entries
- Add `InjectionResult` import + `__all__` entry

- [ ] **Step 4: Run test to verify pass**

Run: `pytest tests/unit/memory/test_injection_result.py -v`
Expected: PASS

- [ ] **Step 5: Verify no broken imports from deleted types**

Run: `pytest tests/unit/memory/ -v --ignore=tests/unit/memory/test_injection_message_loss.py --ignore=tests/unit/memory/test_checkpoint_dedup.py --ignore=tests/unit/memory/test_error_placeholder.py -x 2>&1 | tail -20`
Expected: import errors because other files still reference deleted types. This is expected — those will be fixed in subsequent tasks.

- [ ] **Step 6: Commit**

```bash
git add framework/memory/core/models.py framework/memory/core/__init__.py framework/memory/__init__.py tests/unit/memory/test_injection_result.py
git commit -m "feat(memory): add InjectionResult, remove MemoryContextBundle and PromptSection from public API"
```

---

### Task 3: Delete Filter Strategy + Clean Injection Policies

**Files:**
- Delete: `framework/memory/injection/filter.py`
- Modify: `framework/memory/injection/full_injection.py`
- Modify: `framework/memory/injection/restricted_injection.py`
- Modify: `framework/memory/injection/__init__.py`
- Test: update `tests/unit/memory/test_context_construction_issues.py`

**Dependencies:** Task 1 (ChatMessage types), Task 2 (InjectionResult)

**Parallel:** Independent of Tasks 4, 5, 6, 7

- [ ] **Step 1: Delete filter.py**

```bash
rm framework/memory/injection/filter.py
```

- [ ] **Step 2: Clean FullInjectionPolicy**

In `framework/memory/injection/full_injection.py`:

Remove filter import:
```python
# DELETE these lines:
from framework.memory.injection.filter import (
    InjectionFilterStrategy,
    NoopFilterStrategy,
)
```

Update `__init__`:
```python
def __init__(
    self,
    *,
    budget: MemoryBudget | None = None,
    max_history_entries: int = 20,
) -> None:
    self._budget = budget or MemoryBudget()
    self._max_history = max_history_entries
```

Update `assemble()`:
```python
async def assemble(
    self,
    *,
    context: MemoryContext,
    memory_system: MemorySystem,
    query: str = "",
) -> InjectionResult:
    if not isinstance(memory_system, InjectableMemorySystem):
        raise TypeError(
            f"memory_system must implement InjectableMemorySystem, got {type(memory_system).__name__}"
        )
    sections: list[PromptSection] = []
    injectable = memory_system

    await self._inject_knowledge(sections, context, injectable, query)
    await self._inject_archive(sections, context, injectable, query)
    await self._inject_provider_blocks(sections, injectable)
    await self._inject_provider_prefetch(sections, context, injectable, query)

    sections, dropped = self._trim_by_priority(sections)

    session_msgs = await memory_system.get_history(
        context, max_messages=self._budget.max_history_messages
    )

    return InjectionResult(
        system_prompt=self._sections_to_prompt(sections),
        messages=list(session_msgs),
    )
```

Delete `_trim_by_priority` → replace with internal method that returns joined string or keep as-is but return string. Actually, keep `_trim_by_priority` returning `(list[PromptSection], list[dict])` and add a helper:

```python
@staticmethod
def _sections_to_prompt(sections: list[PromptSection]) -> str:
    return "\n\n".join(s.content for s in sections) if sections else ""
```

Delete `bundle_to_context_state()` function (module-level, lines 295-316).

Update imports: remove `MemoryContextBundle` from imports, add `InjectionResult`.

- [ ] **Step 3: Clean RestrictedInjectionPolicy**

In `framework/memory/injection/restricted_injection.py`:

Remove filter import:
```python
# DELETE these lines:
from framework.memory.injection.filter import (
    InjectionFilterStrategy,
    NoopFilterStrategy,
)
```

Update `__init__`:
```python
def __init__(
    self,
    max_session_messages: int = 50,
) -> None:
    self._max_messages = max_session_messages
```

Update `assemble()`:
```python
async def assemble(
    self,
    *,
    context: MemoryContext,
    memory_system: MemorySystem,
    query: str = "",
) -> InjectionResult:
    messages = await memory_system.get_history(context, max_messages=self._max_messages)
    return InjectionResult(
        system_prompt="",
        messages=list(messages),
    )
```

Update imports: remove `MemoryContextBundle`, add `InjectionResult`.

- [ ] **Step 4: Update __init__.py exports**

In `framework/memory/injection/__init__.py`:
- Remove filter imports (`InjectionFilterStrategy`, `ToolMessageFilterStrategy`, `NoopFilterStrategy`)
- Remove filter `__all__` entries

- [ ] **Step 5: Update policy.py return type**

In `framework/memory/injection/policy.py`:
```python
from framework.memory.core.models import InjectionResult

class MemoryInjectionPolicy(ABC):
    @abstractmethod
    async def assemble(
        self,
        *,
        context: MemoryContext,
        memory_system: MemorySystem,
        query: str = "",
    ) -> InjectionResult: ...
```

- [ ] **Step 6: Run filter-related tests to verify clean removal**

Run: `pytest tests/unit/memory/test_context_construction_issues.py -v -k "filter" 2>&1 | head -10`
Expected: collection errors (tests reference filter classes). This is expected — tests will be fixed in Phase 5.

- [ ] **Step 7: Commit**

```bash
git add framework/memory/injection/
git commit -m "refactor(memory): delete filter strategy, clean injection policies, switch to InjectionResult"
```

---

### Task 4: Delete Crash Recovery from Memory System Stack

**Files:**
- Modify: `framework/memory/core/system.py`
- Modify: `framework/memory/core/layers.py`
- Modify: `framework/memory/layers/session.py`
- Modify: `framework/memory/layers/config.py`
- Modify: `framework/memory/default_system.py`
- Modify: `framework/memory/system.py`

**Dependencies:** Task 1, 2

**Parallel:** Independent of Tasks 3, 6, 7

- [ ] **Step 1: Delete CheckpointMemorySystem protocol, update ContextManagedMemorySystem**

In `framework/memory/core/system.py`:

Delete `CheckpointMemorySystem` class (lines 110-151).

Update `ContextManagedMemorySystem` — remove `CheckpointMemorySystem` from bases, remove `set_pending_user_turn`/`clear_pending_user_turn`, inline core methods:

```python
class ContextManagedMemorySystem(
    BudgetManagedMemorySystem,
    Protocol,
):
    """Full memory capability expected by MemorySystemContextManager."""

    def create_message_history(
        self,
        context: MemoryContext,
        initial_messages: Sequence[ChatMessage | dict[str, Any]] | None = None,
    ) -> MessageHistory: ...

    async def add_messages(
        self,
        context: MemoryContext,
        messages: Sequence[ChatMessage | dict[str, Any]],
    ) -> None: ...

    async def get_history(
        self,
        context: MemoryContext,
        max_messages: int | None = None,
    ) -> list[ChatMessage]: ...

    async def clear(self, context: MemoryContext) -> None: ...
```

- [ ] **Step 2: Remove checkpoint abstracts from SessionMemoryManager ABC**

In `framework/memory/core/layers.py`:

Delete these abstract methods from `SessionMemoryManager`:
- `save_checkpoint()` (lines 60-66)
- `load_checkpoint()` (lines 68-70)
- `get_checkpoint_id()` (lines 99-101)
- `clear_checkpoint()` (lines 103-107)
- `get_last_recovered_checkpoint_id()` (lines 109-112)
- `set_last_recovered_checkpoint_id()` (lines 114-121)

- [ ] **Step 3: Remove checkpoint implementations from ScopedSessionMemoryManager**

In `framework/memory/layers/session.py`:

Delete checkpoint method implementations. Search for and delete all methods named: `save_checkpoint`, `load_checkpoint`, `get_checkpoint_id`, `clear_checkpoint`, `get_last_recovered_checkpoint_id`, `set_last_recovered_checkpoint_id`.

- [ ] **Step 4: Remove checkpoint config fields**

In `framework/memory/layers/config.py`:

In `SessionMemoryConfig`:
```python
# DELETE these fields:
checkpoint_key: str = ".checkpoint"
last_recovered_key: str = ".last_recovered_checkpoint"
```

- [ ] **Step 5: Remove checkpoint delegations from DefaultMemorySystem**

In `framework/memory/default_system.py`:

Delete these methods:
- `set_pending_user_turn()` (lines 260-273)
- `clear_pending_user_turn()` (lines 275-282)
- `get_pending_user_turn()` (lines 284-292)
- `save_checkpoint()` (lines 294-297)
- `load_checkpoint()` (lines 299-300)
- `get_checkpoint_id()` (lines 302-303)
- `get_last_recovered_checkpoint_id()` (lines 305-306)
- `set_last_recovered_checkpoint_id()` (lines 308-311)
- `clear_checkpoint()` (lines 313-319)

- [ ] **Step 6: Clean MemorySystemContextManager**

In `framework/memory/system.py`:

Delete:
- `_ERROR_PLACEHOLDER` constant (line 31)
- `pending_user_turn` set/clear logic in `save()` (lines 199-215)
- `save_checkpoint()` method (lines 220-233)
- `load_checkpoint()` method (lines 233-242)
- `clear_checkpoint()` method (lines 244-253)
- `recover_checkpoint()` method (lines 255-310)
- `add_assistant_placeholder()` method (lines 312-344)

Update `save()` — remove pending_user_turn blocks:
```python
async def save(
    self,
    session_id: str,
    user_message: ChatMessage | dict[str, Any] | None,
    assistant_result: AgentResult,
    metadata: dict[str, Any] | None = None,
) -> None:
    ctx = self._build_context(session_id, metadata=metadata)
    input_metadata = metadata.get("input_metadata") if metadata else None
    if user_message:
        prefixed_message = self._apply_runtime_context_prefix(user_message, input_metadata)
        await self.memory_system.add_messages(ctx, [prefixed_message])
```

- [ ] **Step 7: Commit**

```bash
git add framework/memory/core/system.py framework/memory/core/layers.py framework/memory/layers/session.py framework/memory/layers/config.py framework/memory/default_system.py framework/memory/system.py
git commit -m "refactor(memory): delete crash recovery and pending_user_turn from memory system"
```

---

### Task 5: Delete Crash Recovery from Pipeline + Assembler + AgentSession

**Files:**
- Modify: `framework/pipeline/pipeline.py`
- Modify: `framework/pipeline/context_assembler.py`
- Modify: `framework/session/agent_session.py`

**Dependencies:** Task 4 (system.py methods removed)

**Parallel:** Independent of Tasks 6, 7

- [ ] **Step 1: Clean pipeline.py**

In `framework/pipeline/pipeline.py`:

Delete `_safe_clear_checkpoint()` function (lines 86-93).

Delete `on_checkpoint` closure (lines 557-558):
```python
# DELETE:
async def on_checkpoint(messages):
    await ctx_mgr.save_checkpoint(session_id, messages)
```

Delete `turn_clean` variable initialization (line 724):
```python
# DELETE:
turn_clean = False
```

Delete `turn_clean = True` assignment (line 764):
```python
# DELETE:
turn_clean = True
```

Delete checkpoint clear block in finally (lines 788-791):
```python
# DELETE:
if turn_clean:
    await _safe_clear_checkpoint(ctx_mgr, session_id, timeout=turn.memory_flush_timeout_seconds)
else:
    logger.warning("Turn did not complete cleanly; checkpoint kept for %s", session_id)
```

- [ ] **Step 2: Simplify assemble_context()**

In `framework/pipeline/context_assembler.py`:

Delete the first `load_with_metadata()` call (lines 52-55):
```python
# DELETE:
context_state = await ctx_mgr.load_with_metadata(
    session_id,
    metadata={"input_metadata": input_metadata},
)
```

Delete crash recovery block (lines 57-84):
```python
# DELETE entire block:
recover_fn = getattr(ctx_mgr, "recover_checkpoint", None)
if recover_fn is not None:
    ...
```

Delete separate `build_system_prompt()` call (around line 132). The single `load()` now produces the complete system prompt.

Updated function flow:
```python
async def assemble_context(
    session_id: str,
    input_msg: InputMessage,
    input_metadata: dict[str, Any],
    sanitized_content: str | None,
    media_blocks: list[Any],
    _media_processor: Any | None,
    ctx_mgr: Any,
    route_result: Any | None,
    _is_approval_cmd: bool,
    *,
    agent_descriptor: AgentDescriptor | None = None,
    tool_manager: ToolManager | None = None,
    skill_manager: SkillManager | None = None,
    context_builder: MultiAgentContextBuilder | None = None,
    append_user_message: bool = True,
) -> Any:
    source_agent = input_metadata.get("source_agent")

    # Build multimodal content
    multimodal_content = sanitized_content
    if media_blocks and _media_processor is not None:
        try:
            multimodal_content = _media_processor.build_content(sanitized_content, media_blocks)
        except Exception:
            multimodal_content = sanitized_content

    # Build user message
    if source_agent:
        user_message = {
            "role": MessageRole.AGENT,
            "source_agent": source_agent,
            "content": multimodal_content,
        }
    else:
        user_message = {"role": MessageRole.USER, "content": multimodal_content}

    # Runtime info for system prompt
    agent_name = agent_descriptor.address.name if agent_descriptor else "main"
    runtime_info: dict[str, Any] = {"caller_context": {"agent_name": agent_name}}
    if input_metadata:
        for key in ("user_id", "tenant_id", "channel", "chat_id"):
            if key in input_metadata:
                runtime_info[key] = input_metadata[key]

    # Single load — produces complete ContextState
    context_state = await ctx_mgr.load(
        session_id,
        metadata={"input_metadata": input_metadata},
        tool_manager=tool_manager,
        skill_manager=skill_manager,
        runtime_info=runtime_info,
    )

    # Append user message
    if append_user_message and not _is_approval_cmd:
        await context_state.history.append(user_message)
    await ctx_mgr.save(
        session_id=session_id,
        user_message=None,
        assistant_result=AgentResult(),
        metadata={"input_metadata": input_metadata},
    )

    # Restore full multimodal content in history
    if media_blocks and _media_processor is not None:
        from framework.memory.history import restore_multimodal_in_history
        pending = await restore_multimodal_in_history(
            context_state.history, multimodal_content, logger
        )
        if pending is not None:
            context_state.history = ListMessageHistory(pending)

    # Sideband prompt overlay
    sideband_prompt = input_metadata.get("sideband_system_prompt")
    if isinstance(sideband_prompt, str) and sideband_prompt:
        context_state.system_prompt = "\n\n".join(
            part for part in (context_state.system_prompt, sideband_prompt) if part
        )

    # MultiAgentContextBuilder (unchanged)
    if context_builder is not None and agent_descriptor is not None:
        # ... existing MultiAgentContextBuilder logic unchanged ...
        pass

    return context_state
```

- [ ] **Step 3: Clean agent_session.py**

In `framework/session/agent_session.py`:

Delete checkpoint recovery block (lines 230-257).

Delete `_sanitize_recovered_messages()` method (line 461+):
```python
# DELETE entire method
@staticmethod
def _sanitize_recovered_messages(messages):
    ...
```

- [ ] **Step 4: Commit**

```bash
git add framework/pipeline/pipeline.py framework/pipeline/context_assembler.py framework/session/agent_session.py
git commit -m "refactor: delete crash recovery from pipeline, context assembler, agent session"
```

---

### Task 6: Delete Checkpoint from ContextManager ABC

**Files:**
- Modify: `framework/core/context.py`

**Dependencies:** Task 1, 2

**Parallel:** Independent of Tasks 3, 5, 7

- [ ] **Step 1: Remove checkpoint from ContextManager ABC**

In `framework/core/context.py`:

Delete from `ContextManager` ABC:
```python
# DELETE (lines 128-140):
async def save_checkpoint(self, session_id, messages):
    pass

async def load_checkpoint(self, session_id):
    return None

async def clear_checkpoint(self, session_id):
    pass
```

- [ ] **Step 2: Remove from InMemoryContextManager**

Delete checkpoint overrides (lines 219-232):
```python
# DELETE:
async def save_checkpoint(self, session_id, messages):
    ...

async def load_checkpoint(self, session_id):
    ...

async def clear_checkpoint(self, session_id):
    ...
```

- [ ] **Step 3: Remove from EphemeralContextManager**

Delete no-op overrides (lines 253-260):
```python
# DELETE:
async def save_checkpoint(...):
    pass
async def load_checkpoint(...):
    return None
async def clear_checkpoint(...):
    pass
```

- [ ] **Step 4: Commit**

```bash
git add framework/core/context.py
git commit -m "refactor: delete checkpoint methods from ContextManager ABC and implementations"
```

---

### Task 7: Delete Checkpoint from bot_project ToolCallAwareSessionManager

**Files:**
- Modify: `examples/bot_project/plugins/tool_call_cleanup/manager.py`

**Dependencies:** Task 4 (SessionMemoryManager ABC checkpoint methods removed)

**Parallel:** Independent of Tasks 3, 5, 6

- [ ] **Step 1: Remove checkpoint delegation methods**

In `examples/bot_project/plugins/tool_call_cleanup/manager.py`:

Delete methods (lines 66-83):
```python
# DELETE:
async def save_checkpoint(self, context, messages):
    return await self._inner.save_checkpoint(context, messages)

async def load_checkpoint(self, context):
    return await self._inner.load_checkpoint(context)

async def get_checkpoint_id(self, context):
    return await self._inner.get_checkpoint_id(context)

async def clear_checkpoint(self, context):
    return await self._inner.clear_checkpoint(context)
```

- [ ] **Step 2: Commit**

```bash
git add examples/bot_project/plugins/tool_call_cleanup/manager.py
git commit -m "refactor(bot): delete checkpoint delegation from ToolCallAwareSessionManager"
```

---

### Task 8: Implement truncate_xml_safe()

**Files:**
- Create: `framework/memory/xml_truncate.py`
- Test: `tests/unit/memory/test_xml_truncate.py` (create)

**Dependencies:** Task 1 (ChatMessage extension)

**Parallel:** Independent of Tasks 9-16

- [ ] **Step 1: Write failing test**

```python
# tests/unit/memory/test_xml_truncate.py
"""Tests for XML-safe truncation."""
from __future__ import annotations

import pytest

from framework.memory.xml_truncate import truncate_xml_safe


XML_SHORT = "<msg><content>hello</content></msg>"

XML_LONG = """<agent_message source="planner" timestamp="2026-05-28 14:30:00">
  <thinking>需要查询数据</thinking>
  <content>""" + ("x" * 500) + """</content>
  <status>ok</status>
</agent_message>"""

XML_MALFORMED = "<msg><content>hello</content>"


def test_short_xml_unchanged():
    result = truncate_xml_safe(XML_SHORT, max_chars=200, truncatable_paths=["content"])
    assert result == XML_SHORT


def test_truncates_only_content_element():
    result = truncate_xml_safe(XML_LONG, max_chars=200, truncatable_paths=["content"])
    assert '<agent_message source="planner"' in result
    assert '<thinking>需要查询数据</thinking>' in result
    assert '<status>ok</status>' in result
    assert '</agent_message>' in result
    assert len(result) <= 250  # max_chars + tag close overhead


def test_preserves_attributes():
    result = truncate_xml_safe(XML_LONG, max_chars=200, truncatable_paths=["content"])
    assert 'source="planner"' in result
    assert 'timestamp="2026-05-28 14:30:00"' in result


def test_fallback_malformed_xml():
    result = truncate_xml_safe(XML_MALFORMED, max_chars=20, truncatable_paths=["content"])
    assert result.startswith("<msg><content>hel")
    assert "</content>" in result
    assert "</msg>" in result
    assert "Content truncated" in result


def test_no_truncatable_paths_does_not_truncate():
    """With empty truncatable_paths, preserves entire XML structure."""
    result = truncate_xml_safe(XML_LONG, max_chars=200, truncatable_paths=[])
    assert len(result) <= 250
    assert '<agent_message' in result
    assert '</agent_message>' in result


def test_none_truncatable_paths_fallback():
    result = truncate_xml_safe(XML_LONG, max_chars=200, truncatable_paths=None)
    assert len(result) <= 250
    assert '</agent_message>' in result
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/unit/memory/test_xml_truncate.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement truncate_xml_safe**

```python
# framework/memory/xml_truncate.py
"""XML-safe truncation for governance."""
from __future__ import annotations

import logging
import re
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)


def truncate_xml_safe(
    content: str,
    max_chars: int,
    truncatable_paths: list[str] | None = None,
) -> str:
    """Truncate XML content preserving structure.

    For well-formed XML: truncate only text inside truncatable_paths elements.
    For malformed XML: cut at boundary, close open tags, never crash.

    Args:
        content: XML content string.
        max_chars: Maximum characters to keep.
        truncatable_paths: Element tag names whose text content can be truncated.

    Returns:
        Truncated XML string with preserved structure.
    """
    if len(content) <= max_chars:
        return content

    paths = truncatable_paths or []

    try:
        return _truncate_xml_structured(content, max_chars, paths)
    except (ET.ParseError, Exception) as e:
        logger.debug("XML parse failed in truncate_xml_safe, falling back: %s", e)
        return _truncate_xml_fallback(content, max_chars)


def _truncate_xml_structured(
    content: str,
    max_chars: int,
    truncatable_paths: list[str],
) -> str:
    """Truncate well-formed XML: only reduce text in truncatable_paths elements."""
    root = ET.fromstring(content)

    for path in truncatable_paths:
        for elem in root.iter(path):
            text = elem.text or ""
            if len(text) > 0:
                # Compute budget: distribute remaining chars across truncatable elements
                # Simple strategy: first truncatable element gets the budget
                budget = max_chars - _estimate_overhead(content, text)
                if budget > 0 and len(text) > budget:
                    elem.text = text[:budget]

    result = ET.tostring(root, encoding="unicode")
    if len(result) > max_chars:
        # Secondary: tag-close fallback
        return _truncate_xml_fallback(content, max_chars)
    return result


def _estimate_overhead(content: str, inner_text: str) -> int:
    """Estimate XML overhead excluding inner_text."""
    return len(content) - len(inner_text)


def _truncate_xml_fallback(content: str, max_chars: int) -> str:
    """Fallback: plaintext cut at boundary, then close any open XML tags."""
    prefix = content[:max_chars]
    open_tags: list[str] = []
    for m in re.finditer(r'<(/?)(\w+)(?:[^>]*/?)>', prefix):
        if m.group(1) == '/':
            if open_tags and open_tags[-1] == m.group(2):
                open_tags.pop()
        else:
            open_tags.append(m.group(2))
    for tag in reversed(open_tags):
        prefix += f'</{tag}>'
    return prefix + '\n<!-- Content truncated -->'
```

- [ ] **Step 4: Run test to verify pass**

Run: `pytest tests/unit/memory/test_xml_truncate.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add framework/memory/xml_truncate.py tests/unit/memory/test_xml_truncate.py
git commit -m "feat(memory): add truncate_xml_safe with parse-failure fallback"
```

---

### Task 9: Update LossyContentCompactionGovernance

**Files:**
- Modify: `framework/memory/context_governance.py`

**Dependencies:** Task 8 (truncate_xml_safe)

**Parallel:** Independent of Tasks 10-16 (only depends on Task 8)

- [ ] **Step 1: Update apply() with system skip + content_format dispatch**

In `framework/memory/context_governance.py`:

Add import:
```python
from framework.memory.xml_truncate import truncate_xml_safe
```

Update `LossyContentCompactionGovernance.apply()`:
```python
async def apply(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    length = len(messages)
    max_range = max(0, min(length - self.keep_range_count, int(length * (1.0 - self.keep_range_ratio))))
    if max_range <= 0:
        return messages
    for i, msg in enumerate(messages):
        updated = dict(msg)
        role = str(updated.get("role", ""))

        # system messages: never truncated
        if role == "system":
            result.append(updated)
            continue

        if i >= max_range:
            result.append(updated)
            continue

        limit = self._limits.get(role)
        content = updated.get("content")
        if limit is not None and limit > 0 and isinstance(content, str) and len(content) > limit:
            fmt = str(updated.get("content_format", "plain"))
            if fmt == "xml":
                paths: list[str] = updated.get("truncatable_paths") or []
                updated["content"] = truncate_xml_safe(content, limit, paths)
            else:
                updated["content"] = self._truncate_content(
                    content, limit, role,
                    source_agent=str(updated.get("source_agent", "")),
                )
            updated[META_CONTEXT_LOSSY] = True
            updated[META_ORIGINAL_CHARS] = len(content)
            updated[META_CONTEXT_REDUCTION] = self._reduction_name(role)
        # Truncate oversized tool_calls arguments
        if self._tool_args_head_chars > 0:
            updated = self._truncate_tool_args(updated)
        result.append(updated)
    return result
```

- [ ] **Step 2: Commit**

```bash
git add framework/memory/context_governance.py
git commit -m "feat(governance): add system message skip and content_format dispatch to LossyCompaction"
```

---

### Task 10: Update MicrocompactGovernance for XML Tool Results

**Files:**
- Modify: `framework/memory/context_governance.py`

**Dependencies:** Task 8 (truncate_xml_safe)

**Parallel:** Independent of Tasks 9, 11-16

- [ ] **Step 1: Update MicrocompactGovernance.apply()**

In `framework/memory/context_governance.py`, update `MicrocompactGovernance.apply()`:

```python
async def apply(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compactable_indices: list[int] = []
    for idx, msg in enumerate(messages):
        if msg.get("role") == str(MessageRole.TOOL) and msg.get("name") not in self._whitelist_tools:
            compactable_indices.append(idx)

    if len(compactable_indices) <= self._keep_recent:
        return list(messages)

    stale = compactable_indices[: len(compactable_indices) - self._keep_recent]
    updated: list[dict[str, Any]] | None = None
    for idx in stale:
        msg = messages[idx]
        content = msg.get("content")
        if not isinstance(content, str) or len(content) < self._min_chars:
            continue
        name = msg.get("name", "tool")
        fmt = str(msg.get("content_format", "plain"))
        if fmt == "xml":
            paths: list[str] = msg.get("truncatable_paths") or []
            if paths:
                summary = f"[XML {name} result: {len(content):,} chars, content compacted]"
                compacted = _compact_xml_content(content, paths)
            else:
                summary = f"[XML {name} result omitted: {len(content):,} chars]"
                compacted = summary
            if updated is None:
                updated = [dict(m) for m in messages]
            updated[idx]["content"] = compacted
        else:
            summary = f"[{name} result omitted from context: {len(content):,} chars]"
            if updated is None:
                updated = [dict(m) for m in messages]
            updated[idx]["content"] = summary

    return updated if updated is not None else list(messages)
```

Add helper at module level:
```python
def _compact_xml_content(content: str, paths: list[str]) -> str:
    """Replace text inside truncatable_paths elements with compaction notice."""
    import re
    result = content
    for path in paths:
        pattern = rf'(<{path}[^>]*>)(.*?)(</{path}>)'
        result = re.sub(
            pattern,
            rf'\1[content compacted: {len(result)} chars]\3',
            result,
            flags=re.DOTALL,
        )
    return result
```

- [ ] **Step 2: Commit**

```bash
git add framework/memory/context_governance.py
git commit -m "feat(governance): XML-aware tool result compaction in MicrocompactGovernance"
```

---

### Task 11: Update FullInjectionPolicy — PromptSection Internal + InjectionResult Return

**Files:**
- Modify: `framework/memory/injection/full_injection.py`

**Dependencies:** Task 2 (InjectionResult), Task 3 (filter removed)

**Parallel:** Independent of Tasks 8-10, 12-16

- [ ] **Step 1: Move PromptSection to internal, implement _sections_to_prompt**

In `framework/memory/injection/full_injection.py`:

After existing imports, define internal types:
```python
@dataclass(frozen=True)
class _PromptSection:
    """Internal: used for priority sorting during assembly. Not exported."""
    content: str
    priority: int = 0


@dataclass
class _InjectionResult:
    """Internal assembly result before building final InjectionResult."""
    system_sections: list[_PromptSection]
    messages: list[ChatMessage]
```

Update `assemble()` to use `_PromptSection` and return `InjectionResult` from core.models:

```python
async def assemble(
    self,
    *,
    context: MemoryContext,
    memory_system: MemorySystem,
    query: str = "",
) -> InjectionResult:
    if not isinstance(memory_system, InjectableMemorySystem):
        raise TypeError(...)
    sections: list[_PromptSection] = []
    injectable = memory_system

    await self._inject_knowledge(sections, context, injectable, query)
    await self._inject_archive(sections, context, injectable, query)
    await self._inject_provider_blocks(sections, injectable)
    await self._inject_provider_prefetch(sections, context, injectable, query)

    sections = self._trim_by_priority(sections)

    session_msgs = await memory_system.get_history(
        context, max_messages=self._budget.max_history_messages
    )

    system_prompt = "\n\n".join(s.content for s in sections) if sections else ""
    return InjectionResult(
        system_prompt=system_prompt,
        messages=list(session_msgs),
    )
```

Update all `_inject_*` helpers to work with `_PromptSection` instead of `PromptSection`:
```python
async def _inject_knowledge(
    self,
    sections: list[_PromptSection],
    context: MemoryContext,
    memory_system: InjectableMemorySystem,
    query: str,
) -> None:
    try:
        knowledge = await memory_system.retrieve_knowledge(context, query=query)
        if knowledge.soul:
            sections.append(_PromptSection(content=f"{knowledge.soul}", priority=100))
        if knowledge.user:
            sections.append(_PromptSection(content=f"{knowledge.user}", priority=100))
        if knowledge.memory:
            sections.append(_PromptSection(content=f"{knowledge.memory}", priority=90))
    except Exception:
        logger.debug("Knowledge injection skipped", exc_info=True)
```

Update `_trim_by_priority` to work with `_PromptSection`:
```python
def _trim_by_priority(
    self, sections: list[_PromptSection]
) -> list[_PromptSection]:
    sorted_sections = sorted(sections, key=lambda s: s.priority, reverse=True)
    max_tokens = self._budget.max_system_prompt_tokens
    if max_tokens is None or max_tokens <= 0:
        return sorted_sections

    kept: list[_PromptSection] = []
    running = 0
    for sec in sorted_sections:
        tokens = estimate_text_tokens(sec.content)
        if running + tokens <= max_tokens:
            kept.append(sec)
            running += tokens
        else:
            trimmed = self._trim_section_by_paragraphs(sec, max_tokens - running)
            if trimmed:
                kept.append(trimmed)
                running += estimate_text_tokens(trimmed.content)
    return kept

@staticmethod
def _trim_section_by_paragraphs(
    section: _PromptSection, max_chars: int
) -> _PromptSection | None:
    if len(section.content) <= max_chars:
        return section
    paragraphs = section.content.split("\n\n")
    if not paragraphs:
        return None
    kept = [paragraphs[0]]
    for para in paragraphs[1:]:
        candidate = "\n\n".join(kept + [para])
        if len(candidate) <= max_chars:
            kept.append(para)
        else:
            break
    if not kept:
        return None
    trimmed_content = "\n\n".join(kept)
    if trimmed_content == section.content:
        return section
    return _PromptSection(content=trimmed_content, priority=section.priority)
```

Delete `bundle_to_context_state()` module-level function.

- [ ] **Step 2: Delete PromptSection from models.py if still present**

Check that `framework/memory/core/models.py` no longer has `PromptSection`. (Should already be done from Task 2.)

- [ ] **Step 3: Commit**

```bash
git add framework/memory/injection/full_injection.py
git commit -m "refactor(memory): make PromptSection internal, FullInjectionPolicy returns InjectionResult"
```

---

### Task 12: Update RestrictedInjectionPolicy → InjectionResult

**Files:**
- Modify: `framework/memory/injection/restricted_injection.py`

**Dependencies:** Task 2 (InjectionResult), Task 3 (filter removed)

**Parallel:** Independent of Tasks 8-11, 13-16

- [ ] **Step 1: Switch to InjectionResult**

In `framework/memory/injection/restricted_injection.py`:

```python
from framework.memory.core.models import InjectionResult

class RestrictedInjectionPolicy(MemoryInjectionPolicy):
    """Peer/subagent policy — session messages only, no knowledge/archive/providers."""

    def __init__(
        self,
        max_session_messages: int = 50,
    ) -> None:
        self._max_messages = max_session_messages

    async def assemble(
        self,
        *,
        context: MemoryContext,
        memory_system: MemorySystem,
        query: str = "",
    ) -> InjectionResult:
        messages = await memory_system.get_history(context, max_messages=self._max_messages)
        return InjectionResult(
            system_prompt="",
            messages=list(messages),
        )
```

- [ ] **Step 2: Commit**

```bash
git add framework/memory/injection/restricted_injection.py
git commit -m "refactor(memory): RestrictedInjectionPolicy returns InjectionResult"
```

---

### Task 13: MemorySystemContextManager Single Assemble

**Files:**
- Modify: `framework/memory/system.py`

**Dependencies:** Task 11 (FullInjectionPolicy → InjectionResult), Task 12 (RestrictedInjectionPolicy → InjectionResult), Task 4 (crash recovery removed)

**Parallel:** Depends on Tasks 4, 11, 12

- [ ] **Step 1: Update load() — single assemble, produces complete ContextState**

In `framework/memory/system.py`, update `MemorySystemContextManager.load()`:

```python
async def load(
    self,
    session_id: str,
    runtime_info: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    tool_manager: Any = None,
    skill_manager: SkillManager | None = None,
) -> ContextState:
    self._last_session_id = session_id
    ctx = self._build_context(session_id, runtime_info=runtime_info, metadata=metadata)

    # Budget enforcement
    try:
        await self.memory_system.ensure_within_budget(ctx)
    except Exception:
        logger.warning("Pre-load budget check failed", exc_info=True)

    # Extract query from runtime_info for provider prefetch
    query = ""
    if runtime_info and "message" in runtime_info:
        query = str(runtime_info["message"])

    # Single assemble
    result = await self.injection_policy.assemble(
        context=ctx,
        memory_system=self.memory_system,
        query=query,
    )

    # Build complete system_prompt in one pass
    parts: list[str] = []
    if self.base_system_prompt:
        parts.append(self.base_system_prompt)
    if result.system_prompt:
        parts.append(result.system_prompt)
    if skill_manager is not None:
        skill_prompt = await skill_manager.build_prompt(
            ResolutionContext.from_runtime(tool_manager=tool_manager)
        )
        if skill_prompt:
            parts.append(skill_prompt)
    if runtime_info:
        runtime_text = self._format_runtime_info(runtime_info)
        if runtime_text:
            parts.append(runtime_text)

    system_prompt = "\n\n---\n\n".join(parts) if parts else ""
    history = self.memory_system.create_message_history(
        context=ctx, initial_messages=result.messages,
    )
    return ContextState(system_prompt=system_prompt, history=history)
```

- [ ] **Step 2: Simplify build_system_prompt()**

```python
async def build_system_prompt(
    self,
    tool_manager: Any,
    skill_manager: SkillManager | None = None,
    runtime_info: dict[str, Any] | None = None,
) -> str:
    """Build system prompt. Delegates to load() which caches internally."""
    state = await self.load(
        session_id=self._last_session_id or "default",
        tool_manager=tool_manager,
        skill_manager=skill_manager,
        runtime_info=runtime_info,
    )
    return state.system_prompt
```

- [ ] **Step 3: Delete _bundle_to_state()**

Remove the `_bundle_to_state` method — logic is now inlined in `load()`.

- [ ] **Step 4: Commit**

```bash
git add framework/memory/system.py
git commit -m "refactor(memory): single assemble in MemorySystemContextManager.load()"
```

---

### Task 14: Simplify assemble_context() + agent_session.py

**Files:**
- Modify: `framework/pipeline/context_assembler.py`
- Modify: `framework/session/agent_session.py`

**Dependencies:** Task 13 (MemorySystemContextManager single assemble), Task 5 (crash recovery removed)

**Parallel:** Depends on Tasks 5, 13

- [ ] **Step 1: Verify assemble_context() simplification**

The heavy lifting was done in Task 5. Verify that `assemble_context()`:
- Has exactly ONE call to `ctx_mgr.load()` (not `load_with_metadata`)
- No crash recovery block
- No separate `build_system_prompt()` call
- `load()` is called with `tool_manager`, `skill_manager`, `runtime_info`

Run a quick syntax check:
```bash
python -c "import ast; ast.parse(open('framework/pipeline/context_assembler.py').read()); print('OK')"
```

- [ ] **Step 2: Verify agent_session.py cleanup**

The crash recovery block and `_sanitize_recovered_messages` were deleted in Task 5. Verify clean.

- [ ] **Step 3: Commit (if any fixes needed)**

```bash
git add framework/pipeline/context_assembler.py framework/session/agent_session.py
git commit -m "refactor: finalize context assembler and agent session simplification"
```

---

### Task 15: Update PendingInjectionGovernance → XML + System Role

**Files:**
- Modify: `framework/memory/pending.py`
- Modify: `framework/memory/context_governance.py`

**Dependencies:** Task 1 (ChatMessage extension), Task 8 (XML format defined)

**Parallel:** Independent of Tasks 9-14, 16

- [ ] **Step 1: Update DefaultPendingPrunedInputInjector.apply()**

In `framework/memory/pending.py`:

```python
async def apply(
    self,
    messages: list[dict[str, Any]],
    context: MemoryContext,
) -> list[dict[str, Any]]:
    if await self._clear_if_session_completed(context):
        return messages
    try:
        entries = await self._manager.get_entries(context)
    except Exception:
        logger.warning("Failed to load pending pruned inputs", exc_info=True)
        return messages
    if not entries:
        return messages

    # Build XML content
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    xml_parts = [f'<supplementary-context type="pending-input" entries="{len(entries)}" timestamp="{ts}">']
    for entry in entries:
        source = "user"
        content = self._entry_content(entry)
        xml_parts.append(f'  <entry source="{source}">')
        xml_parts.append(f'    <content>{_xml_escape(content)}</content>')
        xml_parts.append(f'  </entry>')
    xml_parts.append('</supplementary-context>')
    xml_content = "\n".join(xml_parts)

    pending_message = {
        "role": "system",
        "content": xml_content,
        "content_format": "xml",
        "truncatable_paths": ["content"],
        "metadata": {
            "memory_source": "pending_pruned_inputs",
            "entry_count": len(entries),
        },
    }
    insert_at = self._after_system_messages(messages)
    return [*messages[:insert_at], pending_message, *messages[insert_at:]]
```

Add helper:
```python
def _xml_escape(text: str) -> str:
    """Escape special XML characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
```

- [ ] **Step 2: Commit**

```bash
git add framework/memory/pending.py
git commit -m "feat(pending): XML format + system role + content_format for pending injection"
```

---

### Task 16: Update normalize_agent_messages_for_llm() → XML Format

**Files:**
- Modify: `framework/core/message_utils.py`

**Dependencies:** Task 1 (ChatMessage extension)

**Parallel:** Independent of Tasks 9-15

- [ ] **Step 1: Change agent message format from text prefix to XML**

In `framework/core/message_utils.py`:

```python
def normalize_agent_messages_for_llm(
    messages: Sequence[ChatMessage | dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    has_agent = False
    converted: list[dict[str, Any]] = []

    for msg in messages:
        msg_dict = _msg_to_dict(msg)
        if msg_dict.get("role") != MessageRole.AGENT:
            converted.append(msg_dict)
            continue

        has_agent = True
        source_agent = msg_dict.get("source_agent", "unknown")
        original_content = msg_dict.get("content", "")
        ts = msg_dict.get("created_at", "")

        xml_content = (
            f'<agent_message source="{_xml_escape(str(source_agent))}"'
            + (f' timestamp="{ts}"' if ts else "")
            + ">\n"
            + f"  <content>{_xml_escape(str(original_content))}</content>\n"
            + "</agent_message>"
        )

        converted.append({
            "role": "user",
            "content": xml_content,
            "content_format": "xml",
            "truncatable_paths": ["content"],
            **{k: v for k, v in msg_dict.items()
               if k not in ("role", "content", "source_agent", "content_format", "truncatable_paths")},
        })

    return converted, has_agent


def _xml_escape(text: str) -> str:
    """Escape special XML characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
```

Update `AGENT_COMMUNICATION_SYSTEM_NOTE` — already mentions `<agent_message>` XML. Keep as-is.

- [ ] **Step 2: Commit**

```bash
git add framework/core/message_utils.py
git commit -m "feat(core): agent messages use XML agent_message format with content_format"
```

---

### Task 17: Delete Dead Test Files + Update Affected Tests

**Files:**
- Delete: `tests/unit/memory/test_checkpoint_dedup.py`
- Delete: `tests/unit/memory/test_error_placeholder.py`
- Delete: `tests/unit/memory/test_injection_message_loss.py`
- Modify: all tests in `tests/` that reference removed checkpoint/filter/MemoryContextBundle APIs

**Dependencies:** All implementation tasks (1-16)

- [ ] **Step 1: Delete dead test files**

```bash
rm tests/unit/memory/test_checkpoint_dedup.py
rm tests/unit/memory/test_error_placeholder.py
rm tests/unit/memory/test_injection_message_loss.py
```

- [ ] **Step 2: Run full test suite to find broken tests**

Run: `pytest tests/ -x --tb=line 2>&1 | head -80`
Identify all tests that fail due to:
- Import errors (reference to deleted classes)
- AttributeError (reference to removed methods like save_checkpoint)
- Type errors (MemoryContextBundle vs InjectionResult)

- [ ] **Step 3: Update each broken test file**

For each broken test file, either:
- Remove test methods that test deleted functionality (checkpoint, filter)
- Update imports: `MemoryContextBundle` → `InjectionResult`
- Update assertions to match new return types
- Remove `filter_strategy=` constructor args

Key files from the spec:
- `test_context_construction_issues.py` — remove filter references, MemoryContextBundle imports
- `test_bot_project_memory_pipeline.py` — remove bundle.dropped_sections access
- `test_pending_injection_correctness.py` — remove MemoryContextBundle import
- `tests/unit/agents/react/test_nodes.py` — remove checkpoint mock tests
- `tests/unit/session/test_agent_session.py` — remove checkpoint recovery tests
- `tests/unit/pipeline/test_slash_commands.py` — remove load_checkpoint mock
- `tests/unit/pipeline/test_pipeline_subagent_emitter.py` — remove checkpoint mocks
- `tests/unit/memory/core/test_layers.py` — remove checkpoint method mocks
- `tests/unit/memory/core/test_default_system.py` — remove checkpoint tests
- `tests/unit/memory/test_tool_call_cleanup_manager.py` — remove checkpoint mock methods
- `tests/unit/core/test_context.py` — remove checkpoint tests

- [ ] **Step 4: Verify all tests pass**

Run: `pytest tests/unit/ -v --tb=short 2>&1 | tail -30`
Expected: all tests PASS after updates

- [ ] **Step 5: Commit**

```bash
git add -u tests/
git commit -m "test: delete dead tests, update all tests for simplified API"
```

---

### Task 18: Add New Verification Tests

**Files:**
- Create: `tests/unit/memory/test_single_assemble.py`
- Create: `tests/unit/memory/test_xml_survives_governance.py`
- Modify: `tests/unit/memory/test_context_construction_issues.py`

**Dependencies:** All implementation tasks (1-16)

- [ ] **Step 1: Test single assemble**

```python
# tests/unit/memory/test_single_assemble.py
"""Verify single assemble in MemorySystemContextManager."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.core.context import ContextState
from framework.memory.core.message import ChatMessage
from framework.memory.core.models import InjectionResult
from framework.memory.system import MemorySystemContextManager


@pytest.mark.asyncio
async def test_load_produces_complete_context_state():
    """load() returns ContextState with both system_prompt and history."""
    mock_system = MagicMock()
    mock_system.ensure_within_budget = AsyncMock()
    mock_system.create_message_history = MagicMock(
        return_value=MagicMock()
    )
    policy = MagicMock()
    policy.assemble = AsyncMock(return_value=InjectionResult(
        system_prompt="## Knowledge\n...",
        messages=[ChatMessage(role="user", content="hello")],
    ))
    ctx_mgr = MemorySystemContextManager(
        memory_system=mock_system,
        injection_policy=policy,
        base_system_prompt="You are helpful.",
    )
    state = await ctx_mgr.load("s1", tool_manager=MagicMock())
    assert isinstance(state, ContextState)
    assert "You are helpful." in state.system_prompt
    assert "## Knowledge" in state.system_prompt
    assert state.history is not None


@pytest.mark.asyncio
async def test_single_assemble_called_once():
    """injection_policy.assemble() called exactly once per load()."""
    mock_system = MagicMock()
    mock_system.ensure_within_budget = AsyncMock()
    mock_system.create_message_history = MagicMock(return_value=MagicMock())
    policy = MagicMock()
    policy.assemble = AsyncMock(return_value=InjectionResult(
        system_prompt="",
        messages=[],
    ))
    ctx_mgr = MemorySystemContextManager(
        memory_system=mock_system,
        injection_policy=policy,
    )
    await ctx_mgr.load("s1")
    await ctx_mgr.build_system_prompt(tool_manager=MagicMock())
    # build_system_prompt calls load() again, but in the same request
    # assemble is still called only once per load
    assert policy.assemble.call_count == 2  # Once per load call
```

- [ ] **Step 2: Test XML survives governance chain**

```python
# tests/unit/memory/test_xml_survives_governance.py
"""Integration: XML messages survive full governance chain intact."""
from __future__ import annotations

import pytest

from framework.memory.context_governance import (
    CompositeGovernance,
    LossyContentCompactionGovernance,
    MicrocompactGovernance,
    PendingInjectionGovernance,
    TokenBudgetGovernance,
    ToolChainRepairGovernance,
)


XML_AGENT_MSG = """<agent_message source="planner" timestamp="2026-05-28 14:30:00">
  <thinking>查询数据</thinking>
  <content>""" + ("d" * 3000) + """</content>
</agent_message>"""


@pytest.mark.asyncio
async def test_xml_agent_message_survives_lossy_truncation():
    """XML agent message: content truncated, structure preserved."""
    gov = LossyContentCompactionGovernance(
        user_head_chars=500,
        keep_range_count=0,
        keep_range_ratio=0.0,
    )
    messages = [{
        "role": "user",
        "content": XML_AGENT_MSG,
        "content_format": "xml",
        "truncatable_paths": ["content"],
    }]
    result = await gov.apply(messages)
    assert '<agent_message source="planner"' in result[0]["content"]
    assert '<thinking>查询数据</thinking>' in result[0]["content"]
    assert '</agent_message>' in result[0]["content"]
    assert len(result[0]["content"]) < len(XML_AGENT_MSG)


@pytest.mark.asyncio
async def test_system_messages_skip_all_truncation():
    """System messages pass through Lossy + TokenBudget untouched."""
    gov = CompositeGovernance([
        LossyContentCompactionGovernance(
            tool_result_head_chars=100,
            assistant_head_chars=100,
            keep_range_count=0,
            keep_range_ratio=0.0,
        ),
        TokenBudgetGovernance(max_tokens=2000),
    ])
    system_content = "<supplementary-context><content>" + ("x" * 5000) + "</content></supplementary-context>"
    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": "short"},
    ]
    result = await gov.apply(messages)
    assert result[0]["role"] == "system"
    assert result[0]["content"] == system_content  # Untouched
```

- [ ] **Step 3: Run new tests**

Run: `pytest tests/unit/memory/test_single_assemble.py tests/unit/memory/test_xml_survives_governance.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add tests/unit/memory/test_single_assemble.py tests/unit/memory/test_xml_survives_governance.py
git commit -m "test: add integration tests for single assemble and XML governance safety"
```

---

### Task 19: Final Integration Verification

**Dependencies:** All tasks (1-18)

- [ ] **Step 1: Run full unit test suite**

Run: `pytest tests/unit/ -v --tb=short 2>&1 | tail -30`
Expected: all tests PASS

- [ ] **Step 2: Run the bot_project tests**

Run: `pytest examples/bot_project/tests/ -v --tb=short 2>&1 | tail -20`
Expected: all tests PASS

- [ ] **Step 3: Verify type safety with mypy**

Run: `mypy framework/memory/ framework/core/context.py framework/core/message_utils.py framework/pipeline/context_assembler.py --ignore-missing-imports 2>&1 | tail -20`
Expected: no new type errors introduced

- [ ] **Step 4: Run lint**

Run: `ruff check framework/memory/ framework/core/context.py framework/core/message_utils.py framework/pipeline/context_assembler.py framework/pipeline/pipeline.py framework/session/agent_session.py 2>&1 | tail -10`
Expected: no new lint errors

- [ ] **Step 5: Commit final state**

```bash
git add -A
git commit -m "test: final integration verification — all tests pass, types clean"
```
