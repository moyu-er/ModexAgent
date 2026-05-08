# Pending Pruned Input Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a default-on pending-pruned-input memory layer that stores pruned unfinished `user`/`agent` inputs by session and injects them after governance as the first `user` message.

**Architecture:** Add `pending` as an auxiliary `MemorySystem` layer with its own manager, config, extractor, and injector. Compression records only pruned unfinished inputs into pending storage after archive write succeeds or is skipped, lifecycle clears the physical pending store when a completed assistant is present in the persisted session, and the ReAct LLM path injects pending entries after governance and before provider calls.

**Tech Stack:** Python 3.11, dataclasses, ABCs, existing `MemoryStoreRegistry`/`MemoryStorage`, pytest, mypy, ruff.

---

## File Structure

- Modify: `framework/core/types.py`
  - Add `MessageRole.PENDING` with role comments.
- Modify: `framework/memory/core/scope.py`
  - Add `MemoryLayerName.PENDING`.
- Modify: `framework/memory/core/layers.py`
  - Add `PendingPrunedInputMemoryManager` ABC, including `replace_entries()` for commit rollback, and `MemoryLayerSet.pending`.
- Modify: `framework/memory/layers/config.py`
  - Add `PendingPrunedInputMemoryConfig` and `MemoryLayerConfigSet.pending`.
- Create: `framework/memory/layers/pending.py`
  - Default scoped pending manager and typed entry model.
- Modify: `framework/memory/layers/factory.py`
  - Build pending manager for `single_user()` and `session_only()`.
- Modify: `framework/memory/layers/__init__.py`
  - Export pending config and manager.
- Create: `framework/memory/pending.py`
  - Extractor and injector. Lifecycle/default-system hooks own cleanup.
- Modify: `framework/memory/compression/policies.py`
  - Add pending manager/extractor to coordinator and persist pruned unfinished inputs.
- Modify: `framework/memory/lifecycle.py`
  - Clear pending physical storage when completed assistant messages are appended and on subagent session end.
- Modify: `framework/memory/default_system.py`
  - Clear pending in `clear()`.
- Modify: `framework/memory/system.py`
  - Ensure `create_memory_system()` builds layer sets with pending by default.
- Modify: `framework/agents/react/runtime.py`
  - Add typed `pending_injector` and `memory_context` runtime fields.
- Modify: `framework/agents/react/nodes/llm.py`
  - Run pending injection after governance and before provider call.
- Modify: `framework/pipeline/pipeline.py`
  - Set runtime `memory_context` to the active session before each turn.
- Modify: `framework/memory/__init__.py`
  - Export public pending abstractions.
- Modify: `examples/bot_project/bot/service/core.py`
  - Wire pending injector/runtime context for main agent.
- Modify: `examples/bot_project/bot/service/builders.py`
  - Wire pending config for peer/subagent memory through generic config.
- Tests:
  - Create `tests/unit/memory/test_pending_pruned_inputs.py`
  - Modify `tests/unit/memory/test_lifecycle.py`
  - Modify `tests/unit/memory/test_compression_policies.py`
  - Create `tests/unit/memory/test_pending_injection.py`
  - Create `examples/bot_project/tests/test_pending_memory_config.py`

---

### Task 1: Add Role, Layer Name, Config, and LayerSet Shape

**Files:**
- Modify: `framework/core/types.py`
- Modify: `framework/memory/core/scope.py`
- Modify: `framework/memory/layers/config.py`
- Modify: `framework/memory/core/layers.py`
- Test: `tests/unit/memory/test_pending_pruned_inputs.py`

- [ ] **Step 1: Write failing enum/config tests**

Create `tests/unit/memory/test_pending_pruned_inputs.py` with:

```python
from __future__ import annotations

from framework.core.types import MessageRole
from framework.memory.core.layers import MemoryLayerSet, PendingPrunedInputMemoryManager
from framework.memory.core.scope import MemoryLayerName, SessionScope
from framework.memory.layers.config import PendingPrunedInputMemoryConfig


def test_pending_role_is_internal_message_role() -> None:
    assert MessageRole.PENDING == "pending"


def test_pending_layer_name_exists() -> None:
    assert MemoryLayerName.PENDING == "pending"


def test_pending_config_defaults_enabled_and_session_scoped() -> None:
    config = PendingPrunedInputMemoryConfig()
    assert config.enabled is True
    assert config.max_entries == 8
    assert config.max_chars == 12000
    assert isinstance(config.scope, SessionScope)


def test_memory_layer_set_accepts_optional_pending_manager() -> None:
    class DummyPending(PendingPrunedInputMemoryManager):
        async def append_entries(self, context, entries):
            return None

        async def get_entries(self, context):
            return []

        async def replace_entries(self, context, entries):
            return None

        async def clear(self, context):
            return None

    class DummySession:
        pass

    pending = DummyPending()
    layer_set = MemoryLayerSet(session=DummySession(), pending=pending)  # type: ignore[arg-type]
    assert layer_set.pending is pending
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_pending_pruned_inputs.py -q
```

Expected: FAIL because `MessageRole.PENDING`, `MemoryLayerName.PENDING`, `PendingPrunedInputMemoryConfig`, and `PendingPrunedInputMemoryManager` do not exist.

- [ ] **Step 3: Implement enum/config/ABC shape**

In `framework/core/types.py`, replace the `MessageRole` body with documented values:

```python
class MessageRole(StrEnum):
    """Canonical role values used by framework messages."""

    SYSTEM = "system"
    """Provider-visible system instruction."""

    USER = "user"
    """Human or normalized user input sent to the provider."""

    ASSISTANT = "assistant"
    """Assistant response, with or without tool calls."""

    TOOL = "tool"
    """Tool execution result tied to an assistant tool call."""

    AGENT = "agent"
    """Internal peer/subagent input; converted to user at the provider boundary."""

    PENDING = "pending"
    """Internal-only pruned unfinished input; never sent to providers as pending."""
```

In `framework/memory/core/scope.py`, add:

```python
class MemoryLayerName(StrEnum):
    """Canonical memory layer names used in metadata and config."""

    SESSION = "session"
    ARCHIVE = "archive"
    KNOWLEDGE = "knowledge"
    PROVIDER = "provider"
    PENDING = "pending"
```

In `framework/memory/layers/config.py`, add:

```python
@dataclass(frozen=True)
class PendingPrunedInputMemoryConfig:
    enabled: bool = True
    max_entries: int = 8
    max_chars: int = 12000
    scope: MemoryScope = field(default_factory=SessionScope)
```

Then extend `MemoryLayerConfigSet`:

```python
@dataclass(frozen=True)
class MemoryLayerConfigSet:
    session: SessionMemoryConfig = field(default_factory=SessionMemoryConfig)
    archive: ArchiveMemoryConfig | None = field(default_factory=ArchiveMemoryConfig)
    knowledge: KnowledgeMemoryConfig | None = field(default_factory=KnowledgeMemoryConfig)
    pending: PendingPrunedInputMemoryConfig | None = field(
        default_factory=PendingPrunedInputMemoryConfig
    )
```

In `framework/memory/core/layers.py`, add the ABC before `MemoryLayerSet`:

```python
class PendingPrunedInputMemoryManager(ABC):
    """Auxiliary memory for pruned unfinished user/agent inputs."""

    @abstractmethod
    async def append_entries(
        self,
        context: MemoryContext,
        entries: Sequence[Any],
    ) -> None:
        pass

    @abstractmethod
    async def get_entries(self, context: MemoryContext) -> list[Any]:
        pass

    @abstractmethod
    async def replace_entries(
        self,
        context: MemoryContext,
        entries: Sequence[Any],
    ) -> None:
        pass

    @abstractmethod
    async def clear(self, context: MemoryContext) -> None:
        pass
```

Extend `MemoryLayerSet`:

```python
@dataclass(frozen=True)
class MemoryLayerSet:
    """Fieldized memory layer ownership for the default tiered system."""

    session: SessionMemoryManager
    archive: ArchiveMemoryManager | None = None
    knowledge: KnowledgeMemoryManager | None = None
    pending: PendingPrunedInputMemoryManager | None = None

    def with_session(self, manager: SessionMemoryManager) -> MemoryLayerSet:
        return replace(self, session=manager)

    def with_archive(self, manager: ArchiveMemoryManager | None) -> MemoryLayerSet:
        return replace(self, archive=manager)

    def with_knowledge(self, manager: KnowledgeMemoryManager | None) -> MemoryLayerSet:
        return replace(self, knowledge=manager)

    def with_pending(
        self,
        manager: PendingPrunedInputMemoryManager | None,
    ) -> MemoryLayerSet:
        return replace(self, pending=manager)
```

- [ ] **Step 4: Run tests to verify pass**

Run:

```powershell
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_pending_pruned_inputs.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git -C F:\tool\pythonProject\ModexAgent add framework/core/types.py framework/memory/core/scope.py framework/memory/layers/config.py framework/memory/core/layers.py tests/unit/memory/test_pending_pruned_inputs.py
git -C F:\tool\pythonProject\ModexAgent commit -m "feat: add pending memory layer contracts"
```

---

### Task 2: Implement Default Pending Manager and Factory Wiring

**Files:**
- Create: `framework/memory/layers/pending.py`
- Modify: `framework/memory/layers/factory.py`
- Modify: `framework/memory/layers/__init__.py`
- Modify: `framework/memory/__init__.py`
- Test: `tests/unit/memory/test_pending_pruned_inputs.py`

- [ ] **Step 1: Add failing manager/factory tests**

Append to `tests/unit/memory/test_pending_pruned_inputs.py`:

```python
import time

import pytest

from framework.memory.core.scope import MemoryContext
from framework.memory.layers.factory import MemoryLayerFactory
from framework.memory.layers.pending import (
    PendingPrunedInputEntry,
    ScopedPendingPrunedInputMemoryManager,
)
from framework.memory.registry.in_memory import InMemoryStoreRegistry


@pytest.mark.asyncio
async def test_pending_manager_deduplicates_and_moves_duplicate_to_latest() -> None:
    registry = InMemoryStoreRegistry()
    manager = ScopedPendingPrunedInputMemoryManager(
        MemoryLayerFactory._storage_factory(
            registry,
            MemoryLayerName.PENDING,
            SessionScope(),
        ),
        PendingPrunedInputMemoryConfig(max_entries=8, max_chars=12000),
    )
    ctx = MemoryContext(session_id="s1")
    first = PendingPrunedInputEntry.from_message(
        {"role": "user", "content": "same"},
        pruned_at=time.time(),
    )
    second = PendingPrunedInputEntry.from_message(
        {"role": "agent", "source_agent": "peer", "content": "[From Agent peer]\nsend"},
        pruned_at=time.time(),
    )
    duplicate = PendingPrunedInputEntry.from_message(
        {"role": "user", "content": "same"},
        pruned_at=time.time(),
    )

    await manager.append_entries(ctx, [first, second, duplicate])

    entries = await manager.get_entries(ctx)
    assert [entry.content for entry in entries] == ["[From Agent peer]\nsend", "same"]


@pytest.mark.asyncio
async def test_pending_manager_enforces_max_entries_from_oldest() -> None:
    registry = InMemoryStoreRegistry()
    manager = ScopedPendingPrunedInputMemoryManager(
        MemoryLayerFactory._storage_factory(
            registry,
            MemoryLayerName.PENDING,
            SessionScope(),
        ),
        PendingPrunedInputMemoryConfig(max_entries=2, max_chars=12000),
    )
    ctx = MemoryContext(session_id="s1")
    entries = [
        PendingPrunedInputEntry.from_message(
            {"role": "user", "content": f"msg-{idx}"},
            pruned_at=time.time(),
        )
        for idx in range(3)
    ]

    await manager.append_entries(ctx, entries)

    stored = await manager.get_entries(ctx)
    assert [entry.content for entry in stored] == ["msg-1", "msg-2"]


@pytest.mark.asyncio
async def test_pending_layer_uses_distinct_storage_from_session() -> None:
    registry = InMemoryStoreRegistry()
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    ctx = MemoryContext(session_id="s1")

    await layer_set.session.add_messages(ctx, [{"role": "user", "content": "session"}])
    assert layer_set.pending is not None
    await layer_set.pending.append_entries(ctx, [
        PendingPrunedInputEntry.from_message(
            {"role": "user", "content": "pending"},
            pruned_at=time.time(),
        )
    ])

    session_messages = await layer_set.session.get_all_messages(ctx)
    pending_entries = await layer_set.pending.get_entries(ctx)
    assert [msg.content for msg in session_messages] == ["session"]
    assert [entry.content for entry in pending_entries] == ["pending"]
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_pending_pruned_inputs.py -q
```

Expected: FAIL because `framework.memory.layers.pending` and factory wiring do not exist.

- [ ] **Step 3: Create pending manager implementation**

Create `framework/memory/layers/pending.py`:

```python
"""Pending pruned input memory manager."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from framework.core.types import MessageRole
from framework.memory.core.layers import PendingPrunedInputMemoryManager
from framework.memory.core.scope import MemoryContext
from framework.memory.layers.config import PendingPrunedInputMemoryConfig, StorageFactory

_PENDING_MESSAGES_KEY = ".pending_pruned_inputs"


@dataclass(frozen=True)
class PendingPrunedInputEntry:
    """Stored unfinished user/agent input pruned from session memory."""

    role: MessageRole
    content: str | list[dict[str, Any]]
    source_agent: str | None
    created_at: float
    pruned_at: float
    fingerprint: str

    @classmethod
    def from_message(
        cls,
        message: dict[str, Any],
        *,
        pruned_at: float,
    ) -> PendingPrunedInputEntry:
        role = MessageRole(str(message.get("role", "")))
        if role not in {MessageRole.USER, MessageRole.AGENT}:
            raise ValueError(f"pending input role must be user or agent, got {role}")
        content = message.get("content", "")
        normalized_content: str | list[dict[str, Any]]
        if isinstance(content, list):
            normalized_content = [dict(item) for item in content if isinstance(item, dict)]
        else:
            normalized_content = str(content)
        source_agent = message.get("source_agent")
        source_agent_text = str(source_agent) if source_agent is not None else None
        created_at_raw = message.get("created_at") or message.get("timestamp") or pruned_at
        created_at = float(created_at_raw) if isinstance(created_at_raw, int | float) else pruned_at
        return cls(
            role=role,
            content=normalized_content,
            source_agent=source_agent_text,
            created_at=created_at,
            pruned_at=pruned_at,
            fingerprint=cls.fingerprint_for(role, normalized_content, source_agent_text),
        )

    @staticmethod
    def fingerprint_for(
        role: MessageRole,
        content: str | list[dict[str, Any]],
        source_agent: str | None,
    ) -> str:
        payload = {
            "role": str(role),
            "source_agent": source_agent or "",
            "content": content,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["role"] = str(self.role)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PendingPrunedInputEntry | None:
        try:
            role = MessageRole(str(data.get("role", "")))
            if role not in {MessageRole.USER, MessageRole.AGENT}:
                return None
            content = data.get("content", "")
            if isinstance(content, list):
                content = [dict(item) for item in content if isinstance(item, dict)]
            else:
                content = str(content)
            source_agent = data.get("source_agent")
            return cls(
                role=role,
                content=content,
                source_agent=str(source_agent) if source_agent is not None else None,
                created_at=float(data.get("created_at", 0.0)),
                pruned_at=float(data.get("pruned_at", 0.0)),
                fingerprint=str(data.get("fingerprint", "")),
            )
        except Exception:
            return None


class ScopedPendingPrunedInputMemoryManager(PendingPrunedInputMemoryManager):
    """Pending layer manager backed by scoped memory storage."""

    def __init__(
        self,
        storage_factory: StorageFactory,
        config: PendingPrunedInputMemoryConfig | None = None,
    ) -> None:
        self._storage_factory = storage_factory
        self._config = config or PendingPrunedInputMemoryConfig()

    async def append_entries(
        self,
        context: MemoryContext,
        entries: Sequence[PendingPrunedInputEntry],
    ) -> None:
        if not self._config.enabled or not entries:
            return
        storage = await self._storage_factory(context)
        async with storage.get_lock().write():
            existing = await self._load_entries_locked(storage)
            by_fingerprint = {entry.fingerprint: entry for entry in existing}
            ordered = [entry for entry in existing if entry.fingerprint not in {
                new_entry.fingerprint for new_entry in entries
            }]
            for entry in entries:
                by_fingerprint[entry.fingerprint] = entry
                ordered.append(entry)
            ordered = self._enforce_limits(ordered)
            await storage.set(
                _PENDING_MESSAGES_KEY,
                [entry.to_dict() for entry in ordered],
            )

    async def get_entries(self, context: MemoryContext) -> list[PendingPrunedInputEntry]:
        if not self._config.enabled:
            return []
        storage = await self._storage_factory(context)
        raw = await storage.get(_PENDING_MESSAGES_KEY)
        return self._decode_entries(raw)

    async def clear(self, context: MemoryContext) -> None:
        storage = await self._storage_factory(context)
        async with storage.get_lock().write():
            await storage.delete(_PENDING_MESSAGES_KEY)

    async def _load_entries_locked(self, storage: Any) -> list[PendingPrunedInputEntry]:
        raw = await storage.get(_PENDING_MESSAGES_KEY)
        return self._decode_entries(raw)

    @staticmethod
    def _decode_entries(raw: Any) -> list[PendingPrunedInputEntry]:
        if not isinstance(raw, list):
            return []
        result: list[PendingPrunedInputEntry] = []
        for item in raw:
            if isinstance(item, dict):
                entry = PendingPrunedInputEntry.from_dict(item)
                if entry is not None:
                    result.append(entry)
        return result

    def _enforce_limits(
        self,
        entries: list[PendingPrunedInputEntry],
    ) -> list[PendingPrunedInputEntry]:
        max_entries = max(0, self._config.max_entries)
        if max_entries == 0:
            return []
        kept = entries[-max_entries:]
        while kept and self._content_chars(kept) > self._config.max_chars:
            if len(kept) == 1:
                kept = [self._truncate_entry(kept[0], self._config.max_chars)]
                break
            kept = kept[1:]
        return kept

    @staticmethod
    def _content_chars(entries: Sequence[PendingPrunedInputEntry]) -> int:
        return sum(len(json.dumps(entry.content, ensure_ascii=False)) for entry in entries)

    @staticmethod
    def _truncate_entry(
        entry: PendingPrunedInputEntry,
        max_chars: int,
    ) -> PendingPrunedInputEntry:
        if isinstance(entry.content, str):
            content: str | list[dict[str, Any]] = entry.content[:max_chars]
        else:
            raw = json.dumps(entry.content, ensure_ascii=False)
            content = raw[:max_chars]
        return PendingPrunedInputEntry(
            role=entry.role,
            content=content,
            source_agent=entry.source_agent,
            created_at=entry.created_at,
            pruned_at=entry.pruned_at,
            fingerprint=entry.fingerprint,
        )
```

- [ ] **Step 4: Wire factory and exports**

In `framework/memory/layers/factory.py`, import the manager and config, build pending in both factory methods:

```python
from framework.memory.layers.config import (
    MemoryLayerConfigSet,
    PendingPrunedInputMemoryConfig,
    SessionMemoryConfig,
    StorageFactory,
)
from framework.memory.layers.pending import ScopedPendingPrunedInputMemoryManager
```

In `single_user()`, add:

```python
pending_manager = (
    ScopedPendingPrunedInputMemoryManager(
        MemoryLayerFactory._storage_factory(
            registry, MemoryLayerName.PENDING, config.pending.scope
        ),
        config.pending,
    )
    if config.pending is not None and config.pending.enabled
    else None
)
return MemoryLayerSet(
    session=session_manager,
    archive=archive_manager,
    knowledge=knowledge_manager,
    pending=pending_manager,
)
```

In `session_only()`, change signature and body:

```python
def session_only(
    *,
    registry: MemoryStoreRegistry,
    config: SessionMemoryConfig | None = None,
    pending_config: PendingPrunedInputMemoryConfig | None = None,
) -> MemoryLayerSet:
    session_config = config or SessionMemoryConfig()
    session_manager = ScopedSessionMemoryManager(
        MemoryLayerFactory._storage_factory(
            registry,
            MemoryLayerName.SESSION,
            session_config.scope,
        ),
        session_config,
    )
    effective_pending = pending_config if pending_config is not None else PendingPrunedInputMemoryConfig()
    pending_manager = (
        ScopedPendingPrunedInputMemoryManager(
            MemoryLayerFactory._storage_factory(
                registry,
                MemoryLayerName.PENDING,
                effective_pending.scope,
            ),
            effective_pending,
        )
        if effective_pending.enabled
        else None
    )
    return MemoryLayerSet(session=session_manager, pending=pending_manager)
```

In `framework/memory/layers/__init__.py`, export:

```python
from framework.memory.layers.config import PendingPrunedInputMemoryConfig
from framework.memory.layers.pending import (
    PendingPrunedInputEntry,
    ScopedPendingPrunedInputMemoryManager,
)
```

In `framework/memory/__init__.py`, export `PendingPrunedInputMemoryManager`, `PendingPrunedInputMemoryConfig`, `PendingPrunedInputEntry`, and `ScopedPendingPrunedInputMemoryManager`.

- [ ] **Step 5: Run tests**

Run:

```powershell
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_pending_pruned_inputs.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git -C F:\tool\pythonProject\ModexAgent add framework/memory/layers/pending.py framework/memory/layers/factory.py framework/memory/layers/__init__.py framework/memory/__init__.py tests/unit/memory/test_pending_pruned_inputs.py
git -C F:\tool\pythonProject\ModexAgent commit -m "feat: add default pending memory manager"
```

---

### Task 3: Add Extractor and Injector

**Files:**
- Create: `framework/memory/pending.py`
- Test: `tests/unit/memory/test_pending_injection.py`
- Modify: `tests/unit/memory/test_pending_pruned_inputs.py`

- [ ] **Step 1: Write failing extractor/injector tests**

Create `tests/unit/memory/test_pending_injection.py`:

```python
from __future__ import annotations

import time

import pytest

from framework.core.types import MessageRole
from framework.memory.core.scope import MemoryContext, MemoryLayerName, SessionScope
from framework.memory.layers.config import PendingPrunedInputMemoryConfig
from framework.memory.layers.factory import MemoryLayerFactory
from framework.memory.layers.pending import (
    PendingPrunedInputEntry,
    ScopedPendingPrunedInputMemoryManager,
)
from framework.memory.pending import (
    DefaultPendingPrunedInputExtractor,
    DefaultPendingPrunedInputInjector,
)
from framework.memory.registry.in_memory import InMemoryStoreRegistry


def test_extractor_keeps_only_unfinished_pruned_inputs() -> None:
    extractor = DefaultPendingPrunedInputExtractor()
    pruned = [
        {"role": "user", "content": "completed"},
        {"role": "assistant", "content": "done"},
        {"role": "agent", "source_agent": "peer", "content": "[From Agent peer]\nopen"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "result"},
    ]

    entries = extractor.extract(pruned, pruned_at=100.0)

    assert len(entries) == 1
    assert entries[0].role == MessageRole.AGENT
    assert entries[0].content == "[From Agent peer]\nopen"


@pytest.mark.asyncio
async def test_injector_inserts_single_user_after_system() -> None:
    registry = InMemoryStoreRegistry()
    manager = ScopedPendingPrunedInputMemoryManager(
        MemoryLayerFactory._storage_factory(
            registry,
            MemoryLayerName.PENDING,
            SessionScope(),
        ),
        PendingPrunedInputMemoryConfig(max_entries=8, max_chars=12000),
    )
    ctx = MemoryContext(session_id="s1")
    await manager.append_entries(ctx, [
        PendingPrunedInputEntry.from_message(
            {"role": "user", "content": "old user"},
            pruned_at=time.time(),
        ),
        PendingPrunedInputEntry.from_message(
            {"role": "agent", "source_agent": "peer", "content": "[From Agent peer]\nold agent"},
            pruned_at=time.time(),
        ),
    ])
    injector = DefaultPendingPrunedInputInjector(manager)
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "current"},
    ]

    result = await injector.apply(messages, ctx)

    assert [msg["role"] for msg in result] == ["system", "user", "user"]
    assert result[1]["content"] == "old user\n\n[From Agent peer]\nold agent"
    assert result[1]["metadata"]["memory_source"] == "pending_pruned_inputs"
    assert result[2]["content"] == "current"


@pytest.mark.asyncio
async def test_injector_returns_messages_unchanged_without_entries() -> None:
    registry = InMemoryStoreRegistry()
    manager = ScopedPendingPrunedInputMemoryManager(
        MemoryLayerFactory._storage_factory(
            registry,
            MemoryLayerName.PENDING,
            SessionScope(),
        ),
        PendingPrunedInputMemoryConfig(),
    )
    ctx = MemoryContext(session_id="s1")
    injector = DefaultPendingPrunedInputInjector(manager)
    messages = [{"role": "user", "content": "current"}]

    result = await injector.apply(messages, ctx)

    assert result == messages
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_pending_injection.py -q
```

Expected: FAIL because `framework.memory.pending` does not exist.

- [ ] **Step 3: Implement pending extractor/injector**

Create `framework/memory/pending.py`:

```python
"""Pending pruned input extraction and injection helpers."""

from __future__ import annotations

import logging
import json
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

from framework.core.types import MessageRole
from framework.memory.core.layers import PendingPrunedInputMemoryManager, SessionMemoryManager
from framework.memory.core.scope import MemoryContext
from framework.memory.layers.pending import PendingPrunedInputEntry

logger = logging.getLogger(__name__)

PENDING_PRUNED_INPUT_SOURCE = "pending_pruned_inputs"


class PendingPrunedInputExtractor(ABC):
    """Extract unfinished inputs whose original session indices were pruned."""

    @abstractmethod
    def extract(
        self,
        messages: Sequence[dict[str, Any]],
        pruned_indices: set[int],
    ) -> list[PendingPrunedInputEntry]:
        pass


class DefaultPendingPrunedInputExtractor(PendingPrunedInputExtractor):
    """Default extractor using plain assistant as completion boundary."""

    def extract(
        self,
        messages: Sequence[dict[str, Any]],
        pruned_indices: set[int],
    ) -> list[PendingPrunedInputEntry]:
        timestamp = time.time()
        open_inputs: list[PendingPrunedInputEntry] = []
        for index, message in enumerate(messages):
            role = str(message.get("role", ""))
            if role in {str(MessageRole.USER), str(MessageRole.AGENT)}:
                if index in pruned_indices:
                    open_inputs.append(
                        PendingPrunedInputEntry.from_message(message, pruned_at=timestamp)
                    )
                continue
            if role == str(MessageRole.ASSISTANT) and not message.get("tool_calls"):
                open_inputs.clear()
        return open_inputs


class PendingPrunedInputInjector(ABC):
    """Inject pending pruned inputs into provider-visible message copies."""

    @abstractmethod
    async def apply(
        self,
        messages: list[dict[str, Any]],
        context: MemoryContext,
    ) -> list[dict[str, Any]]:
        pass


class DefaultPendingPrunedInputInjector(PendingPrunedInputInjector):
    """Merge pending entries into one user message after system messages."""

    def __init__(
        self,
        manager: PendingPrunedInputMemoryManager | None,
        session: SessionMemoryManager | None = None,
    ) -> None:
        self._manager = manager
        self._session = session

    async def apply(
        self,
        messages: list[dict[str, Any]],
        context: MemoryContext,
    ) -> list[dict[str, Any]]:
        if self._manager is None:
            return list(messages)
        if await self._clear_if_session_completed(context):
            return list(messages)
        try:
            entries = await self._manager.get_entries(context)
        except Exception:
            logger.warning("Pending pruned input injection skipped", exc_info=True)
            return list(messages)
        if not entries:
            return list(messages)
        content = self._merge_content(entries)
        if not content:
            return list(messages)
        synthetic = {
            "role": str(MessageRole.USER),
            "content": content,
            "metadata": {
                "memory_source": PENDING_PRUNED_INPUT_SOURCE,
                "entry_count": len(entries),
            },
        }
        insert_at = 0
        while insert_at < len(messages) and messages[insert_at].get("role") == str(MessageRole.SYSTEM):
            insert_at += 1
        return [*messages[:insert_at], synthetic, *messages[insert_at:]]

    @staticmethod
    def _merge_content(entries: Sequence[PendingPrunedInputEntry]) -> str:
        parts: list[str] = []
        for entry in entries:
            if isinstance(entry.content, str):
                text = entry.content
            else:
                text = json.dumps(entry.content, ensure_ascii=False, sort_keys=True)
            if text:
                parts.append(text)
        return "\n\n".join(parts)

    async def _clear_if_session_completed(self, context: MemoryContext) -> bool:
        if self._session is None or self._manager is None:
            return False
        raw_messages = await self._session.get_all_messages(context)
        for message in raw_messages:
            data = message.to_dict() if hasattr(message, "to_dict") else dict(message)
            if data.get("role") == str(MessageRole.ASSISTANT) and not data.get("tool_calls"):
                await self._manager.clear(context)
                return True
        return False
```

- [ ] **Step 4: Export pending abstractions**

In `framework/memory/__init__.py`, import and add to `__all__`:

```python
from framework.memory.pending import (
    DefaultPendingPrunedInputExtractor,
    DefaultPendingPrunedInputInjector,
    PendingPrunedInputExtractor,
    PendingPrunedInputInjector,
)
```

- [ ] **Step 5: Run tests**

Run:

```powershell
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_pending_injection.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git -C F:\tool\pythonProject\ModexAgent add framework/memory/pending.py framework/memory/__init__.py tests/unit/memory/test_pending_injection.py
git -C F:\tool\pythonProject\ModexAgent commit -m "feat: add pending input extraction and injection"
```

---

### Task 4: Persist Pending Inputs During Compression Commit

**Files:**
- Modify: `framework/memory/compression/policies.py`
- Test: `tests/unit/memory/test_compression_policies.py`

- [ ] **Step 1: Write failing compression persistence tests**

Append to `tests/unit/memory/test_compression_policies.py`:

```python
import pytest

from framework.memory.compression.policies import DefaultMemoryCompressionCoordinator
from framework.memory.core.scope import MemoryContext
from framework.memory.layers.factory import MemoryLayerFactory
from framework.memory.registry.in_memory import InMemoryStoreRegistry


@pytest.mark.asyncio
async def test_compression_persists_pruned_unfinished_user_to_pending() -> None:
    registry = InMemoryStoreRegistry()
    layers = MemoryLayerFactory.single_user(registry=registry)
    ctx = MemoryContext(session_id="pending-compress")
    await layers.session.add_messages(ctx, [
        {"role": "user", "content": "unfinished task"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "tool output"},
        {"role": "assistant", "content": "latest final"},
    ])
    coordinator = DefaultMemoryCompressionCoordinator(max_messages=3, keep_ratio_for_messages=0.5)

    result = await coordinator.maybe_compress(
        session=layers.session,
        archive=layers.archive,
        pending=layers.pending,
        context=ctx,
    )

    assert result.committed is True
    assert layers.pending is not None
    entries = await layers.pending.get_entries(ctx)
    assert [entry.content for entry in entries] == ["unfinished task"]


@pytest.mark.asyncio
async def test_compression_does_not_persist_completed_pruned_user_to_pending() -> None:
    registry = InMemoryStoreRegistry()
    layers = MemoryLayerFactory.single_user(registry=registry)
    ctx = MemoryContext(session_id="pending-completed")
    await layers.session.add_messages(ctx, [
        {"role": "user", "content": "completed task"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "latest"},
        {"role": "assistant", "content": "latest final"},
    ])
    coordinator = DefaultMemoryCompressionCoordinator(max_messages=3, keep_ratio_for_messages=0.5)

    result = await coordinator.maybe_compress(
        session=layers.session,
        archive=layers.archive,
        pending=layers.pending,
        context=ctx,
    )

    assert result.committed is True
    assert layers.pending is not None
    assert await layers.pending.get_entries(ctx) == []
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_compression_policies.py::test_compression_persists_pruned_unfinished_user_to_pending F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_compression_policies.py::test_compression_does_not_persist_completed_pruned_user_to_pending -q
```

Expected: FAIL because `maybe_compress()` and commit flow do not accept or persist `pending`.

- [ ] **Step 3: Extend coordinator and commit policy signatures**

In `framework/memory/compression/policies.py`, import:

```python
from framework.memory.core.layers import (
    ArchiveMemoryManager,
    PendingPrunedInputMemoryManager,
    SessionMemoryManager,
)
from framework.memory.pending import (
    DefaultPendingPrunedInputExtractor,
    PendingPrunedInputExtractor,
)
```

Update `CommitPolicy.commit()`:

```python
async def commit(
    self,
    *,
    plan: CompressionPlan,
    session: SessionMemoryManager,
    archive: ArchiveMemoryManager | None,
    pending: PendingPrunedInputMemoryManager | None = None,
    context: MemoryContext,
    error_policy: CompressionErrorPolicy,
) -> CompressionResult: ...
```

Update `DefaultCommitPolicy.commit()` with the same signature. Persist pending
after archive write succeeds or is skipped, and before replacing session
messages:

```python
pending_snapshot: list[Any] | None = None
if pending is not None and plan.pending_pruned_input_entries:
    try:
        pending_snapshot = await pending.get_entries(context)
        await pending.append_entries(context, plan.pending_pruned_input_entries)
    except Exception:
        logger.warning("Pending pruned input append failed; preserving session", exc_info=True)
        return CompressionResult(
            committed=False,
            retryable=True,
            reason=CompressionResultReason.PENDING_FAILED,
        )
```

If `replace_messages_if_revision()` returns `None`, restore `pending_snapshot`
with `pending.replace_entries(context, pending_snapshot)` before returning
`REVISION_CHANGED`. If archive append fails and the error policy says not to
proceed, return before pending append so no pending entry is written for an
uncommitted compression plan.

Update `MemoryCompressionCoordinator.maybe_compress()`:

```python
async def maybe_compress(
    self,
    *,
    session: SessionMemoryManager,
    archive: ArchiveMemoryManager | None,
    pending: PendingPrunedInputMemoryManager | None = None,
    context: MemoryContext,
) -> CompressionResult: ...
```

Add constructor parameter:

```python
pending_extractor: PendingPrunedInputExtractor | None = None,
```

Store:

```python
self._pending_extractor = pending_extractor or DefaultPendingPrunedInputExtractor()
```

Before commit, compute pruned indices from the keep plan and store extracted
entries in `CompressionPlan.pending_pruned_input_entries`:

```python
pruned_indices_set = set(keep_plan.pruned_indices)
plan = CompressionPlan(
    ...,
    pending_pruned_input_entries=self._pending_extractor.extract(
        all_msgs,
        pruned_indices_set,
    ),
)
```

Pass to commit:

```python
return await self._commit.commit(
    plan=plan,
    session=session,
    archive=archive,
    pending=pending,
    context=context,
    error_policy=self._error,
)
```

- [ ] **Step 4: Run targeted tests**

Run:

```powershell
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_compression_policies.py::test_compression_persists_pruned_unfinished_user_to_pending F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_compression_policies.py::test_compression_does_not_persist_completed_pruned_user_to_pending -q
```

Expected: PASS.

- [ ] **Step 5: Run broader compression tests**

Run:

```powershell
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_compression_policies.py F:\tool\pythonProject\ModexAgent\tests\unit\memory\compression -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git -C F:\tool\pythonProject\ModexAgent add framework/memory/compression/policies.py tests/unit/memory/test_compression_policies.py
git -C F:\tool\pythonProject\ModexAgent commit -m "feat: persist pruned unfinished inputs during compression"
```

---

### Task 5: Clear Pending Memory From Lifecycle and MemorySystem Clear

**Files:**
- Modify: `framework/memory/lifecycle.py`
- Modify: `framework/memory/default_system.py`
- Test: `tests/unit/memory/test_lifecycle.py`
- Test: `tests/unit/memory/test_pending_pruned_inputs.py`

- [ ] **Step 1: Write failing lifecycle clear tests**

Append to `tests/unit/memory/test_lifecycle.py`:

```python
@pytest.mark.asyncio
async def test_on_messages_added_clears_pending_on_plain_assistant():
    coordinator = AsyncMock()
    policy = DefaultMemoryLifecyclePolicy(compression_coordinator=coordinator)
    ctx = MemoryContext(session_id="s1", user_id="u1")
    session = AsyncMock()
    session.get_all_messages = AsyncMock(return_value=[
        {"role": str(MessageRole.ASSISTANT), "content": "done"}
    ])
    pending = AsyncMock()
    layers = MemoryLayerSet(session=session, archive=None, pending=pending)

    await policy.on_messages_added(ctx, layers)

    pending.clear.assert_called_once_with(ctx)


@pytest.mark.asyncio
async def test_on_messages_added_does_not_clear_pending_on_tool_call_assistant():
    coordinator = AsyncMock()
    policy = DefaultMemoryLifecyclePolicy(compression_coordinator=coordinator)
    ctx = MemoryContext(session_id="s1", user_id="u1")
    session = AsyncMock()
    session.get_all_messages = AsyncMock(return_value=[
        {"role": str(MessageRole.ASSISTANT), "content": None, "tool_calls": [{"id": "t1"}]}
    ])
    pending = AsyncMock()
    layers = MemoryLayerSet(session=session, archive=None, pending=pending)

    await policy.on_messages_added(ctx, layers)

    pending.clear.assert_not_called()
```

Append to `tests/unit/memory/test_pending_pruned_inputs.py`:

```python
@pytest.mark.asyncio
async def test_memory_system_clear_clears_pending_layer() -> None:
    registry = InMemoryStoreRegistry()
    layers = MemoryLayerFactory.single_user(registry=registry)
    system = DefaultMemorySystem(layer_set=layers, store_registry=registry)
    ctx = MemoryContext(session_id="s1")
    assert layers.pending is not None
    await layers.pending.append_entries(ctx, [
        PendingPrunedInputEntry.from_message(
            {"role": "user", "content": "pending"},
            pruned_at=time.time(),
        )
    ])

    await system.clear(ctx)

    assert await layers.pending.get_entries(ctx) == []
```

Add imports in the test file:

```python
from framework.memory.default_system import DefaultMemorySystem
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_lifecycle.py::TestDefaultMemoryLifecyclePolicy::test_on_messages_added_clears_pending_on_plain_assistant F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_lifecycle.py::TestDefaultMemoryLifecyclePolicy::test_on_messages_added_does_not_clear_pending_on_tool_call_assistant F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_pending_pruned_inputs.py::test_memory_system_clear_clears_pending_layer -q
```

Expected: FAIL because lifecycle and system clear do not manage pending.

- [ ] **Step 3: Implement lifecycle clearing**

In `framework/memory/lifecycle.py`, add helper:

```python
    async def _clear_pending_on_completed_assistant(
        self,
        context: MemoryContext,
        layers: MemoryLayerSet,
    ) -> None:
        pending = getattr(layers, "pending", None)
        if pending is None:
            return
        try:
            raw_messages = await layers.session.get_all_messages(context)
            messages = [
                msg.to_dict() if hasattr(msg, "to_dict") else dict(msg)
                for msg in raw_messages
            ]
        except Exception:
            logger.debug("Unable to inspect session for pending clear", exc_info=True)
            return
        if any(
            message.get("role") == str(MessageRole.ASSISTANT) and not message.get("tool_calls")
            for message in messages
        ):
            await pending.clear(context)
```

At the start of `on_messages_added()` before compression:

```python
        await self._clear_pending_on_completed_assistant(context, layers)
```

When invoking coordinator, pass pending:

```python
await self._coordinator.maybe_compress(
    session=layers.session,
    archive=layers.archive,
    pending=getattr(layers, "pending", None),
    context=context,
)
```

In `on_session_end()`, after subagent session clear:

```python
pending = getattr(layers, "pending", None)
if pending is not None:
    await pending.clear(context)
```

- [ ] **Step 4: Implement MemorySystem clear**

In `framework/memory/default_system.py`, extend `clear()`:

```python
    async def clear(self, context: MemoryContext) -> None:
        await self._layers.session.clear(context)
        if self._layers.archive is not None:
            await self._layers.archive.clear(context)
        if self._layers.knowledge is not None:
            await self._layers.knowledge.clear(context)
        if self._layers.pending is not None:
            await self._layers.pending.clear(context)
```

- [ ] **Step 5: Run tests**

Run:

```powershell
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_lifecycle.py F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_pending_pruned_inputs.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git -C F:\tool\pythonProject\ModexAgent add framework/memory/lifecycle.py framework/memory/default_system.py tests/unit/memory/test_lifecycle.py tests/unit/memory/test_pending_pruned_inputs.py
git -C F:\tool\pythonProject\ModexAgent commit -m "feat: clear pending memory on completed assistant"
```

---

### Task 6: Inject Pending Inputs After Governance in ReAct LLM Path

**Files:**
- Modify: `framework/agents/react/runtime.py`
- Modify: `framework/agents/react/nodes/llm.py`
- Modify: `framework/pipeline/pipeline.py`
- Test: `tests/unit/memory/test_pending_injection.py`

- [ ] **Step 1: Write failing post-governance injection test**

Append to `tests/unit/memory/test_pending_injection.py`:

```python
from framework.agents.react.nodes.llm import LLMNode


@pytest.mark.asyncio
async def test_llm_node_applies_pending_injector_after_governance() -> None:
    calls: list[str] = []

    class Governance:
        async def apply(self, messages):
            calls.append("governance")
            return [*messages, {"role": "user", "content": "from governance"}]

    class PendingInjector:
        async def apply(self, messages, context):
            calls.append("pending")
            assert messages[-1]["content"] == "from governance"
            return [messages[0], {"role": "user", "content": "pending"}, *messages[1:]]

    class Runtime:
        governance = Governance()
        pending_injector = PendingInjector()
        memory_context = MemoryContext(session_id="s1")

    class Ctx:
        system_prompt = "sys"
        runtime = Runtime()

        async def to_messages(self):
            return [{"role": "user", "content": "current"}]

    node = LLMNode.__new__(LLMNode)

    result = await node._build_messages(Ctx())

    assert calls == ["governance", "pending"]
    assert [msg["content"] for msg in result] == ["sys", "pending", "current", "from governance"]
```

- [ ] **Step 2: Run test to verify failure**

Run:

```powershell
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_pending_injection.py::test_llm_node_applies_pending_injector_after_governance -q
```

Expected: FAIL because runtime has no typed pending injector handling and `_build_messages()` does not call it.

- [ ] **Step 3: Extend runtime type**

In `framework/agents/react/runtime.py`, update `TYPE_CHECKING`:

```python
from framework.memory.core.scope import MemoryContext
from framework.memory.pending import PendingPrunedInputInjector
```

Add dataclass fields:

```python
pending_injector: PendingPrunedInputInjector | None = None
memory_context: MemoryContext | None = None
```

In `from_context()`, pop extension keys:

```python
pending_injector=ctx.extensions.pop("pending_pruned_input_injector", None),
memory_context=ctx.extensions.pop("memory_context", None),
```

- [ ] **Step 4: Apply injector after governance**

In `framework/agents/react/nodes/llm.py`, update `_build_messages()`:

```python
        governance = ctx.runtime.governance if ctx.runtime else None
        if governance is not None:
            messages = await governance.apply(messages)

        pending_injector = ctx.runtime.pending_injector if ctx.runtime else None
        memory_context = ctx.runtime.memory_context if ctx.runtime else None
        if pending_injector is not None and memory_context is not None:
            messages = await pending_injector.apply(messages, memory_context)
        return messages
```

- [ ] **Step 5: Set per-turn memory context in pipeline**

In `framework/pipeline/pipeline.py`, import:

```python
from framework.memory.core.scope import MemoryContext
```

In `_build_runtime_and_context()`, after assigning `agent_context.runtime`, add:

```python
        if agent_context.runtime is not None:
            agent_context.runtime.memory_context = MemoryContext(
                session_id=session_id,
                user_id=getattr(ctx_mgr, "default_user_id", "default"),
                agent_id=getattr(ctx_mgr, "default_agent_id", None),
                agent_role=getattr(ctx_mgr, "default_agent_role", None),
            )
```

This makes a shared prebuilt runtime safe for different sessions because the
session-scoped `memory_context` is refreshed before each turn.

- [ ] **Step 6: Run tests**

Run:

```powershell
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_pending_injection.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git -C F:\tool\pythonProject\ModexAgent add framework/agents/react/runtime.py framework/agents/react/nodes/llm.py framework/pipeline/pipeline.py tests/unit/memory/test_pending_injection.py
git -C F:\tool\pythonProject\ModexAgent commit -m "feat: inject pending inputs after governance"
```

---

### Task 7: Wire Memory System Creation and Bot Project Runtime Extensions

**Files:**
- Modify: `framework/memory/system.py`
- Modify: `examples/bot_project/bot/service/core.py`
- Modify: `examples/bot_project/bot/service/builders.py`
- Test: `examples/bot_project/tests/test_pending_memory_config.py`

- [ ] **Step 1: Write failing bot project wiring tests**

Create `examples/bot_project/tests/test_pending_memory_config.py`:

```python
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from bot.service.core import BotService
from framework.memory.layers.config import PendingPrunedInputMemoryConfig


def _service() -> BotService:
    input_adapter = MagicMock()
    input_adapter.name = "mock_input"
    output_adapter = MagicMock()
    output_adapter.name = "mock_output"
    emitter_factory = MagicMock()
    service = BotService(
        config_dir=Path("."),
        input_adapter=input_adapter,
        output_adapter=output_adapter,
        emitter_factory=emitter_factory,
    )
    service.provider = MagicMock()
    return service


def test_pending_config_defaults_enabled_for_main_memory() -> None:
    service = _service()
    config = service._build_memory_layer_config({"short_term": {"max_messages": 10}})
    assert isinstance(config.pending, PendingPrunedInputMemoryConfig)
    assert config.pending.enabled is True


def test_pending_config_can_be_disabled_for_main_memory() -> None:
    service = _service()
    config = service._build_memory_layer_config({
        "short_term": {"max_messages": 10},
        "pending_pruned_inputs": {"enabled": False},
    })
    assert config.pending is None


def test_peer_compression_uses_generic_pending_config_defaults() -> None:
    service = _service()
    memory_config = service._session_only_memory_config({"short_term": {"max_messages": 10}})
    assert memory_config.pending is not None
    assert memory_config.pending.enabled is True
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\examples\bot_project\tests\test_pending_memory_config.py -q
```

Expected: FAIL because bot builders do not expose pending config yet.

- [ ] **Step 3: Build config helper in bot service**

In `examples/bot_project/bot/service/core.py`, add import inside memory config builder area:

```python
from framework.memory.layers.config import PendingPrunedInputMemoryConfig
```

Where main `MemoryLayerConfigSet` is built, parse:

```python
pending_raw = main_memory_config.get("pending_pruned_inputs", {})
pending_config = (
    None
    if pending_raw.get("enabled") is False
    else PendingPrunedInputMemoryConfig(
        enabled=True,
        max_entries=pending_raw.get("max_entries", 8),
        max_chars=pending_raw.get("max_chars", 12000),
    )
)
```

Pass `pending=pending_config` into `MemoryLayerConfigSet(...)`.

When building main agent context extensions, add:

```python
from framework.memory.pending import DefaultPendingPrunedInputInjector

if memory_system.layers.pending is not None:
    extensions["pending_pruned_input_injector"] = DefaultPendingPrunedInputInjector(
        memory_system.layers.pending
    )
    extensions["memory_context"] = memory_context
```

In `_assemble_runtime()`, after `RuntimeAssembler.assemble(...)` returns `runtime`, attach pending injection services directly:

```python
        if self.memory_system is not None and self.memory_system.layers.pending is not None:
            from framework.memory.pending import DefaultPendingPrunedInputInjector

            runtime.pending_injector = DefaultPendingPrunedInputInjector(
                self.memory_system.layers.pending
            )
            runtime.memory_context = MemoryContext(
                session_id="default",
                user_id="default",
                agent_id=self.config.get("multi_agent", {}).get("parent_agent_name", "main"),
                agent_role="main",
            )
```

`framework/pipeline/pipeline.py::_build_runtime_and_context()` refreshes
`runtime.memory_context` to the active `session_id` before each turn, so the
default context above is only a startup fallback.

- [ ] **Step 4: Wire peer/subagent config in builders**

In `examples/bot_project/bot/service/builders.py`, update `_session_only_memory_config()`:

```python
    def _session_only_memory_config(self, section: dict[str, Any]) -> MemoryLayerConfigSet:
        short_term = section.get("short_term", {})
        pending_raw = section.get("pending_pruned_inputs", {})
        pending_config = (
            None
            if pending_raw.get("enabled") is False
            else PendingPrunedInputMemoryConfig(
                enabled=True,
                max_entries=pending_raw.get("max_entries", 6),
                max_chars=pending_raw.get("max_chars", 8000),
            )
        )
        return MemoryLayerConfigSet(
            session=SessionMemoryConfig(
                max_messages=short_term.get("max_messages", 50),
            ),
            archive=None,
            knowledge=None,
            pending=pending_config,
        )
```

Add import:

```python
from framework.memory.layers.config import (
    MemoryLayerConfigSet,
    PendingPrunedInputMemoryConfig,
    SessionMemoryConfig,
)
```

Where peer/subagent pipeline extensions are built, add `DefaultPendingPrunedInputInjector` with `memory_context` the same way as main.

- [ ] **Step 5: Ensure `create_memory_system()` passes pending config to session-only factory**

In `framework/memory/system.py`, update `create_memory_system()`:

```python
    if session_only:
        session_config = config.session if config else None
        pending_config = config.pending if config else None
        layer_set = MemoryLayerFactory.session_only(
            registry=registry,
            config=session_config,
            pending_config=pending_config,
        )
```

- [ ] **Step 6: Run bot wiring tests**

Run:

```powershell
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\examples\bot_project\tests\test_pending_memory_config.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git -C F:\tool\pythonProject\ModexAgent add framework/memory/system.py examples/bot_project/bot/service/core.py examples/bot_project/bot/service/builders.py examples/bot_project/tests/test_pending_memory_config.py
git -C F:\tool\pythonProject\ModexAgent commit -m "feat: wire pending memory into bot agents"
```

---

### Task 8: Verify End-to-End Memory Boundaries

**Files:**
- Modify: `tests/unit/memory/test_compression_policies.py`
- Modify: `tests/unit/memory/test_pending_injection.py`
- Test-only task for boundary guarantees.

- [ ] **Step 1: Add boundary regression tests**

Append to `tests/unit/memory/test_compression_policies.py`:

```python
@pytest.mark.asyncio
async def test_compression_input_does_not_include_pending_entries() -> None:
    registry = InMemoryStoreRegistry()
    layers = MemoryLayerFactory.single_user(registry=registry)
    ctx = MemoryContext(session_id="pending-not-compressed")
    assert layers.pending is not None
    await layers.pending.append_entries(ctx, [
        PendingPrunedInputEntry.from_message(
            {"role": "user", "content": "pending only"},
            pruned_at=100.0,
        )
    ])
    await layers.session.add_messages(ctx, [
        {"role": "user", "content": "s0"},
        {"role": "assistant", "content": "a0"},
        {"role": "user", "content": "s1"},
        {"role": "assistant", "content": "a1"},
    ])
    coordinator = DefaultMemoryCompressionCoordinator(max_messages=3, keep_ratio_for_messages=0.5)

    await coordinator.maybe_compress(
        session=layers.session,
        archive=layers.archive,
        pending=layers.pending,
        context=ctx,
    )

    session_contents = [msg.content for msg in await layers.session.get_all_messages(ctx)]
    assert "pending only" not in session_contents
```

Add import:

```python
from framework.memory.layers.pending import PendingPrunedInputEntry
```

Append to `tests/unit/memory/test_pending_injection.py`:

```python
@pytest.mark.asyncio
async def test_injected_pending_message_is_never_pending_role() -> None:
    registry = InMemoryStoreRegistry()
    manager = ScopedPendingPrunedInputMemoryManager(
        MemoryLayerFactory._storage_factory(
            registry,
            MemoryLayerName.PENDING,
            SessionScope(),
        ),
        PendingPrunedInputMemoryConfig(),
    )
    ctx = MemoryContext(session_id="s1")
    await manager.append_entries(ctx, [
        PendingPrunedInputEntry.from_message(
            {"role": "user", "content": "unfinished"},
            pruned_at=time.time(),
        )
    ])
    result = await DefaultPendingPrunedInputInjector(manager).apply([], ctx)
    assert result == [
        {
            "role": "user",
            "content": "unfinished",
            "metadata": {
                "memory_source": "pending_pruned_inputs",
                "entry_count": 1,
            },
        }
    ]
```

- [ ] **Step 2: Run boundary tests**

Run:

```powershell
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_compression_policies.py::test_compression_input_does_not_include_pending_entries F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_pending_injection.py::test_injected_pending_message_is_never_pending_role -q
```

Expected: PASS.

- [ ] **Step 3: Run full targeted memory suite**

Run:

```powershell
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_pending_pruned_inputs.py F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_pending_injection.py F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_lifecycle.py F:\tool\pythonProject\ModexAgent\tests\unit\memory\test_compression_policies.py F:\tool\pythonProject\ModexAgent\tests\unit\memory\compression -q
```

Expected: PASS.

- [ ] **Step 4: Run bot project targeted tests**

Run:

```powershell
python -m pytest --rootdir F:\tool\pythonProject\ModexAgent F:\tool\pythonProject\ModexAgent\examples\bot_project\tests\test_pending_memory_config.py F:\tool\pythonProject\ModexAgent\examples\bot_project\tests\test_memory_construction.py -q
```

Expected: PASS.

- [ ] **Step 5: Type-check and lint touched modules**

Run:

```powershell
mypy F:\tool\pythonProject\ModexAgent\framework\memory F:\tool\pythonProject\ModexAgent\framework\agents\react
ruff check F:\tool\pythonProject\ModexAgent\framework\memory F:\tool\pythonProject\ModexAgent\framework\agents\react F:\tool\pythonProject\ModexAgent\tests\unit\memory
```

Expected: both commands complete without errors.

- [ ] **Step 6: Commit tests and final fixes**

```powershell
git -C F:\tool\pythonProject\ModexAgent add tests/unit/memory/test_compression_policies.py tests/unit/memory/test_pending_injection.py
git -C F:\tool\pythonProject\ModexAgent commit -m "test: verify pending memory boundaries"
```

---

## Self-Review

**Spec coverage:** The plan adds the role, layer name, MemorySystem ownership, default scoped manager, typed entries, extraction from the full session timeline using pruned indices, physical clear on completed assistant, post-governance injection, main/peer/subagent default-on configuration, disable path, and boundary tests proving pending memory is excluded from compression/archive/session content.

**Implementation timing update:** The verified implementation scans the full
session timeline with a pruned-index set, not only the pruned slice, so later
kept plain assistant messages can complete older pruned inputs. Pending append
runs after archive success/skip and before session replace; on session revision
conflict it restores a pending snapshot with `replace_entries()`. Lifecycle
clearing scans persisted session messages for any plain assistant, and
structured pending content is injected as deterministic JSON text.

**Placeholder scan:** The plan contains no `TBD`, no deferred implementation language, and every task has concrete files, commands, expected results, and code snippets.

**Type consistency:** The plan consistently uses `PendingPrunedInputEntry`, `PendingPrunedInputMemoryManager`, `PendingPrunedInputMemoryConfig`, `DefaultPendingPrunedInputExtractor`, `DefaultPendingPrunedInputInjector`, `MemoryLayerName.PENDING`, and `MessageRole.PENDING`.
