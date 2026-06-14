# Session ID Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the bare-string `session_id` (`{conv}.{agent}[.{invocation}]`) with a first-class `SessionId` pydantic object, backed by an authoritative `SessionStore` + runtime `SessionRegistry`, removing all string-parsing of session ids and all backward-compatibility shims.

**Architecture:** `SessionId` (pydantic) is the single identity object — its fields (`agent_name`, `parent_session_id`, `created_at`, `updated_at`, `metadata`) are the authoritative source; the string is opaque and never parsed except as a last-resort `from_str` fallback. Agent identity is resolved at session creation and stored in a `SessionRegistry` (asyncio-locked cache) backed by a `SessionStore` (framework ABC + `LocalFileSessionStore`; business inherits to add workspace/pool partitioning). Subagent sessions are independent snowflakes linked to parents via `parent_session_id`.

**Tech Stack:** Python 3.12+, pydantic 2.12, asyncio, pytest. No new third-party deps (base58 encoded with stdlib).

**Spec:** `docs/superpowers/specs/2026-06-14-session-id-redesign-design.md`

**Critical rules (from user):**
- **No backward-compatibility shims.** Delete `DefaultSessionIdStrategy`, `AgentSessionMeta`, `SessionRelationStore` outright. Do not leave adapters that resurrect the old format.
- `InputMessage.session_id`, `AgentContext.session_id`, `TurnIdentity.session_id`, `MemoryContext.session_id` all become `SessionId` objects.
- Business layer never parses the string; it reads `SessionId` fields or queries the registry.
- `invocation_id` is no longer a separate identity concept — for subagents it equals the session's snowflake part, exposed via `SessionId.snowflake`.
- Timestamps are millisecond integers everywhere.

---

## File Structure

### New files
| File | Responsibility |
|---|---|
| `framework/core/session_id.py` | `SessionId` pydantic model, `SessionIdFactory`, `now_ms`, `encode_snowflake` |
| `framework/core/session_store.py` | `SessionStore` ABC + `LocalFileSessionStore` |
| `framework/core/session_registry.py` | `SessionRegistry` ABC + `InMemorySessionRegistry` |
| `tests/unit/core/test_session_id.py` | `SessionId` + factory unit tests |
| `tests/unit/core/test_session_store.py` | `LocalFileSessionStore` tests |
| `tests/unit/core/test_session_registry.py` | `InMemorySessionRegistry` concurrency tests |
| `examples/bot_project/bot/service/session_store.py` | `WorkspacePoolSessionStore` (inherits `LocalFileSessionStore`) |
| `scripts/migrate_session_ids.py` | One-shot migration (temporary, NOT committed) |

### Modified files
| File | Change |
|---|---|
| `framework/core/types.py` | `InputMessage.session_id: SessionId` (→ `session`); `OutputMessage.session_id` **stays `str`** (wire payload) |
| `framework/core/agent.py` | `AgentContext.session_id: SessionId` (→ `session`); add explicit `comm_kind: AgentCommKind \| None` field; **delete `AgentSessionMeta`** |
| `framework/runtime/models.py` | `TurnIdentity.session_id: SessionId` (→ `session`) |
| `framework/memory/core/scope.py` | `MemoryContext.session_id: SessionId` (→ `session`) |
| `framework/core/runtime_context.py` | `MemoryContext` usage updated |
| `framework/messaging/broker_bridge.py` | build `SessionId` via factory; orphan isolation encodes through `external_id`; `str(msg.session)` in payloads |
| `framework/multi_agent/router.py` | registry-aware routing |
| `framework/multi_agent/pool.py` | hold registry/store; replace `_session_meta` |
| `framework/multi_agent/communication.py` | `SessionIdFactory` on subagent creation |
| `framework/multi_agent/bus.py` | `str(session_id)` at boundaries |
| `framework/multi_agent/envelope.py` | `agent_session_id` stays `str` (wire); `invocation_id` semantics aligned to snowflake |
| `framework/multi_agent/utils.py` | drop parse helpers |
| `framework/pipeline/pipeline.py` | consume `SessionId` objects; set `AgentContext.comm_kind` from descriptor |
| `framework/pipeline/adapters.py` | drop `SessionPrefixStripAdapter` string-splitting |
| `framework/hook/builtin/subagent_auto_send.py` | read `ctx.session` object |
| `framework/hook/builtin/runtime_context.py` | read `ctx.session` object |
| `framework/hook/notification.py` | read `ctx.session` object + `ctx.comm_kind` |
| `framework/hook/builtin/logging.py` | `ctx.session_meta` → `ctx.session` |
| `framework/hook/builtin/progress_report.py` | `ctx.session_meta` → `ctx.session` |
| `framework/multi_agent/inbox/server_local.py` | `str(session_id)` for file paths |
| `examples/bot_project/bot/webui/events.py` | drop `_session_id`/`_conv_prefix`; use `SessionId` |
| `examples/bot_project/bot/webui/server.py` | drop `_parse_session_*`; registry-based |
| `examples/bot_project/bot/webui/transcript_store.py` | keep `_safe_name`; `SessionId` keys |
| `examples/bot_project/bot/service/web_ui_service.py` | wire `WorkspacePoolSessionStore` |
| `examples/bot_project/bot/service/pool_builder.py` | construct `SessionId` via factory |
| `examples/bot_project/bot/adapters/channels.py` | `SessionId` at inbound |

### Deleted files
| File | Why |
|---|---|
| `framework/multi_agent/session_id.py` | `DefaultSessionIdStrategy` superseded by `SessionId`/`SessionIdFactory` |
| `examples/bot_project/bot/service/session_relation_store.py` | parent-child is now a `SessionId` field |

---

## Conventions used throughout

**`now_ms()`** — `int(time.time() * 1000)`.

**`str(SessionId)`** returns the display id. Anywhere code needs a string key (file path, dict key, broker topic), use `str(session)` or `session.session_id`.

**Tests run from repo root** with: `python -m pytest <path> -x`. The bot_project tests run from `examples/bot_project` with: `cd examples/bot_project && python -m pytest <path> -x`.

**Line numbers are advisory.** All `file:line` references were captured at plan-writing time and **will drift** as earlier tasks edit files. When a task says "modify `pool.py:978`", locate the symbol with `codegraph_search`/`codegraph_node` (or grep by symbol name) rather than trusting the line number. Trust the symbol names and the before/after code, not the line numbers.

---

## Phase 1 — Core new modules (self-contained, fully tested)

### Task 1: Create `SessionId` pydantic model with encoding helpers

**Files:**
- Create: `framework/core/session_id.py`
- Create: `tests/unit/core/test_session_id.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/core/test_session_id.py`:
```python
from __future__ import annotations

import time

import pytest

from framework.core.session_id import (
    SessionId,
    SessionIdFactory,
    encode_snowflake,
    now_ms,
)


def test_now_ms_is_int_milliseconds():
    ts = now_ms()
    assert isinstance(ts, int)
    assert abs(ts - int(time.time() * 1000)) < 1000


def test_encode_snowflake_is_deterministic_and_short():
    a = encode_snowflake("1234567890")
    b = encode_snowflake("1234567890")
    assert a == b
    assert len(a) <= 24
    assert "." not in a and "/" not in a


def test_session_id_str_returns_display():
    session = SessionId(session_id="abc.main", agent_name="main")
    assert str(session) == "abc.main"


def test_session_id_hash_and_eq_by_string():
    a = SessionId(session_id="abc.main", agent_name="main", metadata={"x": 1})
    b = SessionId(session_id="abc.main", agent_name="main")
    assert a == b
    assert hash(a) == hash(b)
    assert {a, b} == {a}


def test_session_id_touch_updates_only_updated_at():
    base = SessionId(
        session_id="abc.main", agent_name="main", created_at=1000, updated_at=1000
    )
    touched = base.touch()
    assert touched.created_at == 1000
    assert touched.updated_at >= base.updated_at


def test_session_id_is_frozen():
    """Frozen model: field mutation raises; safe as dict key after creation."""
    import pytest
    from pydantic import ValidationError

    session = SessionId(session_id="abc.main", agent_name="main")
    with pytest.raises(ValidationError):
        session.session_id = "xyz.main"  # type: ignore[misc]
    # still usable as a dict key
    d = {session: 1}
    assert d[SessionId(session_id="abc.main", agent_name="main")] == 1


def test_from_str_with_separator():
    session = SessionId.from_str("abc.reviewer", default_agent_name="main")
    assert session.session_id == "abc.reviewer"
    assert session.agent_name == "reviewer"


def test_from_str_without_separator_warns():
    with pytest.warns(UserWarning):
        session = SessionId.from_str("abc", default_agent_name="main")
    assert session.agent_name == "main"


def test_from_str_empty_suffix_warns():
    with pytest.warns(UserWarning):
        SessionId.from_str("abc.", default_agent_name="main")


def test_factory_creates_main_session():
    factory = SessionIdFactory()
    session = factory.create(agent_name="main")
    assert session.agent_name == "main"
    assert "." in session.session_id
    assert session.parent_session_id is None
    assert session.created_at == session.updated_at
    assert session.session_id.endswith(".main")


def test_factory_subagent_links_parent():
    factory = SessionIdFactory()
    parent = factory.create(agent_name="main")
    child = factory.create(agent_name="reviewer", parent_session_id=parent)
    assert child.parent_session_id == str(parent)
    # subagent snowflake differs from parent
    assert child.snowflake != parent.snowflake


def test_factory_external_id_becomes_snowflake():
    factory = SessionIdFactory()
    session = factory.create(agent_name="main", external_id="qq-group-12345")
    assert session.session_id.startswith(encode_snowflake("qq-group-12345"))


def test_factory_invocation_id_as_external_becomes_session():
    factory = SessionIdFactory()
    # A subagent whose invocation_id was "a1b2c3d4" now has that as its snowflake
    session = factory.create(agent_name="reviewer", external_id="a1b2c3d4")
    assert session.snowflake == encode_snowflake("a1b2c3d4")


def test_session_id_snowflake_property():
    session = SessionId(session_id="abc123.reviewer", agent_name="reviewer")
    assert session.snowflake == "abc123"


def test_session_id_is_subagent_property():
    main = SessionId(session_id="abc.main", agent_name="main")
    sub = SessionId(
        session_id="xyz.reviewer", agent_name="reviewer", parent_session_id="abc.main"
    )
    assert main.is_subagent is False
    assert sub.is_subagent is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/core/test_session_id.py -x`
Expected: FAIL — `ModuleNotFoundError: No module named 'framework.core.session_id'`

- [ ] **Step 3: Implement `framework/core/session_id.py`**

```python
"""First-class SessionId object + factory.

`SessionId` is the single identity object across the framework. Its fields are
authoritative; the string is opaque and never parsed except via the
last-resort `from_str` fallback.
"""

from __future__ import annotations

import hashlib
import logging
import time
import warnings
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# base58 alphabet (Bitcoin), stdlib-only implementation.
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def now_ms() -> int:
    """Current Unix time in milliseconds."""
    return int(time.time() * 1000)


def encode_snowflake(raw: str) -> str:
    """Shorten an arbitrary raw id (IM id, invocation_id, conversation id) into
    a compact, filesystem-safe base58 string.

    Deterministic: same input always yields the same output. Length is ~16 chars
    for a 12-byte digest, well within filesystem path limits.
    """
    digest = hashlib.sha256(raw.encode("utf-8")).digest()[:12]
    num = int.from_bytes(digest, "big")
    if num == 0:
        return _BASE58_ALPHABET[0]
    out: list[str] = []
    while num > 0:
        num, rem = divmod(num, 58)
        out.append(_BASE58_ALPHABET[rem])
    # preserve leading zeros (sha256 digest won't start with zero bytes often,
    # but handle for correctness)
    return "".join(reversed(out))


class SessionId(BaseModel):
    """First-class session identifier.

    Required: ``session_id`` (complete display id ``snowflake.agentName``),
    ``agent_name``. All other fields default to ``None`` / empty.

    Frozen so it is hash-safe as a dict key / set member. ``__hash__`` derives
    from the immutable ``session_id`` string. Updates go through
    ``model_copy(update={...})`` (see ``touch()``).
    """

    model_config = ConfigDict(frozen=True)

    session_id: str = Field(..., description="Complete display id: snowflake.agentName")
    agent_name: str = Field(..., description="Bound agent name")
    parent_session_id: str | None = None
    created_at: int | None = Field(default=None, description="ms Unix epoch")
    updated_at: int | None = Field(default=None, description="ms Unix epoch")
    metadata: dict[str, Any] = Field(default_factory=dict)

    def __str__(self) -> str:
        return self.session_id

    def __hash__(self) -> int:
        return hash(self.session_id)

    # NOTE: isinstance here is required for value-equality semantics — comparing
    # a SessionId to a non-SessionId must return NotImplemented (not False) so
    # Python falls back to the other operand's __eq__. This is the standard
    # dataclass/pydantic equality idiom, not a runtime duck-typing check.
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SessionId):
            return NotImplemented
        return self.session_id == other.session_id

    @property
    def snowflake(self) -> str:
        """The snowflake part (segment before the first '.')."""
        return self.session_id.split(".", 1)[0] if "." in self.session_id else self.session_id

    @property
    def is_subagent(self) -> bool:
        """True when this session has a recorded parent."""
        return self.parent_session_id is not None

    def touch(self) -> SessionId:
        """Return a copy with ``updated_at`` refreshed to now."""
        return self.model_copy(update={"updated_at": now_ms()})

    @classmethod
    def from_str(
        cls,
        value: str,
        *,
        default_agent_name: str | None = None,
    ) -> SessionId:
        """Recover a SessionId from a display string (last-resort fallback).

        Emits a UserWarning when the value has no separator or an empty
        agent_name suffix. Callers should query the registry first.
        """
        if "." not in value:
            warnings.warn(
                f"SessionId {value!r} has no separator; treating as bare snowflake",
                UserWarning,
                stacklevel=2,
            )
            agent_name = default_agent_name or "unknown"
        else:
            _snowflake, _, suffix = value.rpartition(".")
            agent_name = suffix or default_agent_name or "unknown"
            if not suffix:
                warnings.warn(
                    f"SessionId {value!r} has empty agent_name suffix",
                    UserWarning,
                    stacklevel=2,
                )
        return cls(session_id=value, agent_name=agent_name)


class SessionIdFactory:
    """Generates new SessionId instances.

    The snowflake is ``encode_snowflake(external_id or uuid4)``. ``external_id``
    is an IM-provided id or an existing invocation_id; it forms the snowflake
    part only, never the complete session id.
    """

    def __init__(self) -> None:
        pass

    def create(
        self,
        agent_name: str,
        *,
        parent_session_id: SessionId | str | None = None,
        external_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionId:
        raw = external_id if external_id is not None else self._generate_raw()
        encoded = encode_snowflake(raw)
        session_id = f"{encoded}.{agent_name}"
        now = now_ms()
        parent_str = str(parent_session_id) if parent_session_id else None
        return SessionId(
            session_id=session_id,
            agent_name=agent_name,
            parent_session_id=parent_str,
            created_at=now,
            updated_at=now,
            metadata=metadata or {},
        )

    def _generate_raw(self) -> str:
        import uuid

        return uuid.uuid4().hex
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/core/test_session_id.py -x`
Expected: PASS (13 tests)

- [ ] **Step 5: Commit**

```bash
git add framework/core/session_id.py tests/unit/core/test_session_id.py
git commit -m "feat(core): add SessionId pydantic model + factory with base58 snowflake encoding"
```

---

### Task 2: Create `SessionStore` ABC + `LocalFileSessionStore`

**Files:**
- Create: `framework/core/session_store.py`
- Create: `tests/unit/core/test_session_store.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/core/test_session_store.py`:
```python
from __future__ import annotations

from pathlib import Path

import pytest

from framework.core.session_id import SessionId, SessionIdFactory
from framework.core.session_store import LocalFileSessionStore


@pytest.fixture
def factory() -> SessionIdFactory:
    return SessionIdFactory()


async def test_save_and_get_roundtrip(tmp_path: Path, factory: SessionIdFactory):
    store = LocalFileSessionStore(tmp_path)
    session = factory.create(agent_name="main", metadata={"pool": "coding"})
    await store.save(session)
    got = await store.get(str(session))
    assert got is not None
    assert got == session
    assert got.metadata == {"pool": "coding"}


async def test_get_missing_returns_none(tmp_path: Path):
    store = LocalFileSessionStore(tmp_path)
    assert await store.get("nope.main") is None


async def test_delete_removes_session(tmp_path: Path, factory: SessionIdFactory):
    store = LocalFileSessionStore(tmp_path)
    session = factory.create(agent_name="main")
    await store.save(session)
    await store.delete(str(session))
    assert await store.get(str(session)) is None


async def test_list_sessions_returns_all(tmp_path: Path, factory: SessionIdFactory):
    store = LocalFileSessionStore(tmp_path)
    a = factory.create(agent_name="main")
    b = factory.create(agent_name="reviewer")
    await store.save(a)
    await store.save(b)
    listed = await store.list_sessions()
    ids = {str(s) for s in listed}
    assert ids == {str(a), str(b)}


async def test_get_children_returns_only_children(
    tmp_path: Path, factory: SessionIdFactory
):
    store = LocalFileSessionStore(tmp_path)
    parent = factory.create(agent_name="main")
    child1 = factory.create(agent_name="reviewer", parent_session_id=parent)
    child2 = factory.create(agent_name="reviewer", parent_session_id=parent)
    other = factory.create(agent_name="main")
    for s in (parent, child1, child2, other):
        await store.save(s)
    children = await store.get_children(str(parent))
    child_ids = {str(c) for c in children}
    assert child_ids == {str(child1), str(child2)}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/core/test_session_store.py -x`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement `framework/core/session_store.py`**

```python
"""SessionStore — authoritative persistence for SessionId records.

Framework provides the ABC and a flat-file default. Business inherits
LocalFileSessionStore to add workspace/pool directory partitioning.
"""

from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from pathlib import Path

from framework.core.session_id import SessionId


def _safe_name(session_id: str) -> str:
    """Replace path-unsafe characters for filesystem use."""
    return re.sub(r"[^\w\-.]", "_", session_id)


class SessionStore(ABC):
    """Authoritative persistent store of SessionId records.

    No workspace/pool awareness — that is the business layer's concern.
    """

    @abstractmethod
    async def save(self, session: SessionId) -> None:
        ...

    @abstractmethod
    async def get(self, session_id: str) -> SessionId | None:
        ...

    @abstractmethod
    async def delete(self, session_id: str) -> None:
        ...

    @abstractmethod
    async def list_sessions(self) -> list[SessionId]:
        ...

    @abstractmethod
    async def get_children(self, parent_session_id: str) -> list[SessionId]:
        ...


class LocalFileSessionStore(SessionStore):
    """Flat-file store: one JSON file per session, keyed by safe session_id.

    Layout: ``<base_dir>/<safe_session_id>.json``. Business subclasses override
    ``_path_for`` to add workspace/pool subdirectories.
    """

    def __init__(self, base_dir: Path) -> None:
        self._base = Path(base_dir)

    def _path_for(self, session_id: str) -> Path:
        return self._base / f"{_safe_name(session_id)}.json"

    async def save(self, session: SessionId) -> None:
        path = self._path_for(str(session))
        await asyncio.to_thread(self._write, path, session.model_dump_json())

    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    async def get(self, session_id: str) -> SessionId | None:
        path = self._path_for(session_id)
        if not await asyncio.to_thread(path.is_file):
            return None
        text = await asyncio.to_thread(path.read_text, "utf-8")
        return SessionId.model_validate_json(text)

    async def delete(self, session_id: str) -> None:
        path = self._path_for(session_id)
        await asyncio.to_thread(path.unlink, True)

    async def list_sessions(self) -> list[SessionId]:
        paths = await asyncio.to_thread(self._collect_paths)
        sessions: list[SessionId] = []
        for path in paths:
            text = await asyncio.to_thread(path.read_text, "utf-8")
            sessions.append(SessionId.model_validate_json(text))
        return sessions

    def _collect_paths(self) -> list[Path]:
        if not self._base.is_dir():
            return []
        return [p for p in self._base.glob("*.json") if p.is_file()]

    async def get_children(self, parent_session_id: str) -> list[SessionId]:
        all_sessions = await self.list_sessions()
        return [s for s in all_sessions if s.parent_session_id == parent_session_id]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/core/test_session_store.py -x`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add framework/core/session_store.py tests/unit/core/test_session_store.py
git commit -m "feat(core): add SessionStore ABC + LocalFileSessionStore"
```

---

### Task 3: Create `SessionRegistry` ABC + `InMemorySessionRegistry`

**Files:**
- Create: `framework/core/session_registry.py`
- Create: `tests/unit/core/test_session_registry.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/core/test_session_registry.py`:
```python
from __future__ import annotations

import asyncio

import pytest

from framework.core.session_id import SessionId, SessionIdFactory
from framework.core.session_registry import InMemorySessionRegistry


@pytest.fixture
def factory() -> SessionIdFactory:
    return SessionIdFactory()


async def test_register_and_get(factory: SessionIdFactory):
    reg = InMemorySessionRegistry()
    session = factory.create(agent_name="main")
    await reg.register(session)
    assert await reg.get(str(session)) == session


async def test_get_missing_returns_none():
    reg = InMemorySessionRegistry()
    assert await reg.get("nope.main") is None


async def test_touch_updates_updated_at(factory: SessionIdFactory):
    reg = InMemorySessionRegistry()
    session = factory.create(agent_name="main")
    await reg.register(session)
    before = (await reg.get(str(session))).updated_at
    await asyncio.sleep(0.01)
    await reg.touch(str(session))
    after = (await reg.get(str(session))).updated_at
    assert after > before


async def test_register_writes_through_to_store(tmp_path, factory: SessionIdFactory):
    from framework.core.session_store import LocalFileSessionStore

    store = LocalFileSessionStore(tmp_path)
    reg = InMemorySessionRegistry(store=store)
    session = factory.create(agent_name="main")
    await reg.register(session)
    # store now has the record
    assert await store.get(str(session)) == session


async def test_load_all_populates_cache_from_store(tmp_path, factory: SessionIdFactory):
    from framework.core.session_store import LocalFileSessionStore

    store = LocalFileSessionStore(tmp_path)
    session = factory.create(agent_name="main")
    await store.save(session)
    reg = InMemorySessionRegistry(store=store)
    await reg.load_all()
    assert await reg.get(str(session)) == session


async def test_concurrent_register_is_safe(factory: SessionIdFactory):
    """Two coroutines registering different sessions must not lose data."""
    reg = InMemorySessionRegistry()
    sessions = [factory.create(agent_name=f"agent{i}") for i in range(20)]
    await asyncio.gather(*(reg.register(s) for s in sessions))
    for s in sessions:
        assert await reg.get(str(s)) is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/core/test_session_registry.py -x`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement `framework/core/session_registry.py`**

```python
"""SessionRegistry — runtime cache for SessionId resolution.

The store is authoritative; the registry is a performance cache that writes
through on register and loads from the store at startup. All operations are
async and guarded by an asyncio.Lock for concurrency safety.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

from framework.core.session_id import SessionId
from framework.core.session_store import SessionStore


class SessionRegistry(ABC):
    """Runtime cache for SessionId lookups."""

    @abstractmethod
    async def register(self, session: SessionId) -> None:
        ...

    @abstractmethod
    async def get(self, session_id: str) -> SessionId | None:
        ...

    @abstractmethod
    async def touch(self, session_id: str) -> None:
        ...

    @abstractmethod
    async def load_all(self) -> None:
        ...


class InMemorySessionRegistry(SessionRegistry):
    """In-memory cache backed by an optional SessionStore."""

    def __init__(self, store: SessionStore | None = None) -> None:
        self._store = store
        self._cache: dict[str, SessionId] = {}
        self._lock = asyncio.Lock()

    async def load_all(self) -> None:
        if self._store is None:
            return
        async with self._lock:
            for session in await self._store.list_sessions():
                self._cache[str(session)] = session

    async def register(self, session: SessionId) -> None:
        async with self._lock:
            self._cache[str(session)] = session
        if self._store is not None:
            await self._store.save(session)

    async def get(self, session_id: str) -> SessionId | None:
        async with self._lock:
            return self._cache.get(session_id)

    async def touch(self, session_id: str) -> None:
        async with self._lock:
            session = self._cache.get(session_id)
            if session is not None:
                self._cache[session_id] = session.touch()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/core/test_session_registry.py -x`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add framework/core/session_registry.py tests/unit/core/test_session_registry.py
git commit -m "feat(core): add SessionRegistry ABC + asyncio-locked InMemorySessionRegistry"
```

---

## Phase 2 — Framework core type signatures

These tasks change the type of `session_id` fields from `str` to `SessionId`. They are sequenced so each compiles independently. Run the relevant test suite after each.

### Task 4: `MemoryContext.session_id: SessionId`

**Files:**
- Modify: `framework/memory/core/scope.py:26-72` (`MemoryContext`)
- Modify: `framework/core/runtime_context.py:215-227` (`_resolve_scope_key`)
- Test: `tests/unit/memory/test_memory_scope_isolation.py`

- [ ] **Step 1: Change the field type**

In `framework/memory/core/scope.py`, change the `MemoryContext.session_id` field:

```python
# Before
session_id: str | None = None
# After
session_id: SessionId | None = None
```

Add the import at the top:
```python
from framework.core.session_id import SessionId
```

Update `with_defaults` and `to_dict`/`from_dict` to handle `SessionId`:
- In `to_dict`: serialize `session_id` via `str(self.session_id)` when not None.
- In `from_dict`: reconstruct via `SessionId.from_str(value)` when the stored value is a `str`.

Concrete edit to `to_dict`:
```python
def to_dict(self) -> dict[str, Any]:
    data = asdict(self)
    if self.session_id is not None:
        data["session_id"] = str(self.session_id)
    return data
```

Concrete edit to `from_dict`:
```python
@classmethod
def from_dict(cls, data: dict[str, Any] | None) -> "MemoryContext":
    if not data:
        return cls()
    allowed = cls.__dataclass_fields__.keys()
    kwargs = {key: data.get(key) for key in allowed}
    raw_sid = kwargs.get("session_id")
    if type(raw_sid) is str:
        kwargs["session_id"] = SessionId.from_str(raw_sid)
    return cls(**kwargs)
```

- [ ] **Step 2: Update `SessionScope.get_scope_key`**

In `framework/memory/core/scope.py:131-135`:
```python
class SessionScope(MemoryScope):
    def get_scope_key(self, context: MemoryContext) -> str:
        sid = context.session_id
        if sid is None:
            return "default"
        return str(sid)
```

- [ ] **Step 3: Update `RuntimeContextManager._resolve_scope_key`**

In `framework/core/runtime_context.py:215-227`, the `MemoryContext` constructor receives `session_id=session_id` where the caller passes a string. Change the method signature to accept `SessionId`:

```python
async def get_context(
    self,
    session: SessionId,
    metadata: dict[str, Any] | None = None,
) -> RuntimeContext:
    scope_key = self._resolve_scope_key(session, metadata)
    return await self._store.get_or_create(scope_key)

async def clear_context(
    self,
    session: SessionId,
    metadata: dict[str, Any] | None = None,
) -> None:
    scope_key = self._resolve_scope_key(session, metadata)
    await self._store.clear(scope_key)

def _resolve_scope_key(self, session: SessionId, metadata: dict[str, Any] | None) -> str:
    meta = metadata or {}
    mem_ctx = MemoryContext(
        session_id=session,
        user_id=meta.get("user_id"),
        tenant_id=meta.get("tenant_id"),
        agent_id=meta.get("agent_id"),
        channel=meta.get("channel"),
        chat_id=meta.get("chat_id"),
        sender_agent=meta.get("sender_agent"),
        receiver_agent=meta.get("receiver_agent"),
    )
    return self._scope.get_scope_key(mem_ctx)
```

Add `from framework.core.session_id import SessionId` import.

- [ ] **Step 4: Update the caller in `RuntimeContextHook`**

In `framework/hook/builtin/runtime_context.py:28-34`, change:
```python
async def before_turn(self, ctx: AgentContext) -> None:
    rt = ctx.runtime
    if rt is None:
        return
    rt_mgr = rt.services.runtime_context_manager
    if rt_mgr is not None and rt._runtime_context is None:
        rt._runtime_context = await rt_mgr.get_context(ctx.session_id, None)
```
`ctx.session_id` is now a `SessionId`, so this call already matches the new signature (no change needed beyond ensuring `ctx.session_id` is the object — handled in Task 6).

- [ ] **Step 5: Run scope isolation tests**

Run: `python -m pytest tests/unit/memory/test_memory_scope_isolation.py -x`
Expected: PASS (fix any test that constructs `MemoryContext(session_id="x")` to use `SessionId`).

- [ ] **Step 6: Commit**

```bash
git add framework/memory/core/scope.py framework/core/runtime_context.py framework/hook/builtin/runtime_context.py tests/unit/memory/test_memory_scope_isolation.py
git commit -m "refactor(memory): MemoryContext.session_id becomes SessionId object"
```

---

### Task 5: `TurnIdentity.session_id: SessionId`

**Files:**
- Modify: `framework/runtime/models.py:52-58` (`TurnIdentity`)
- Test: any test constructing `TurnIdentity` (grep `TurnIdentity(`)

- [ ] **Step 1: Change the field**

In `framework/runtime/models.py`:
```python
from framework.core.session_id import SessionId

@dataclass
class TurnIdentity:
    """Stable identity for every turn."""

    agent_id: str
    session: SessionId
    turn_id: str
    conversation_id: str | None = None
```

Rename `session_id` → `session` to reflect it's now an object. (This is a deliberate rename; all references update in later tasks.)

- [ ] **Step 2: Run the full test suite to find breakages**

Run: `python -m pytest tests/unit -x -q 2>&1 | head -40`
Expected: FAIL — references to `TurnIdentity(session_id=...)` and `.session_id`. Note all file:line locations.

- [ ] **Step 3: Fix all `TurnIdentity` construction sites**

For each location found in Step 2, change `session_id=<value>` → `session=<SessionId object>`. Typical sites: `framework/pipeline/pipeline.py:591`, tests. Where the value was a string, wrap with `SessionId.from_str(value)` or pass the already-constructed object.

This task does NOT commit alone — it completes together with Task 6 (which provides the object). Mark Step 3 done and proceed to Task 6.

---

### Task 6: `AgentContext.session_id: SessionId`; delete `AgentSessionMeta`

**Files:**
- Modify: `framework/core/agent.py:29-109`
- Test: `tests/unit/core/test_agent_context.py`, `tests/unit/multi_agent/test_comm_kind_session_id.py` (delete the latter — tests a deleted type)

- [ ] **Step 1: Rewrite the identity section of `framework/core/agent.py`**

Replace lines 29-109 (the `AgentSessionMeta` class and the `AgentContext` fields up to `session_meta`) with:

```python
@dataclass
class AgentContext:
    """Agent execution context — typed runtime state via ``runtime`` field."""

    system_prompt: str
    history: MessageHistory
    tool_manager: ToolManager
    session: SessionId
    comm_kind: AgentCommKind | None = None
    max_iterations: int = 10
    temperature: float | None = None
    max_tokens: int | None = None
    attachments: list[str] = field(default_factory=list)
    emitter: ContentEmitter | None = None
    runtime: AgentRuntime | None = None
    identity: TurnIdentity | None = None
    system_prompt_pipeline: SystemPromptPipeline | None = None
```

Remove the `session_meta` field. Update imports: add
```python
from framework.core.session_id import SessionId
from framework.multi_agent.comm_kind import AgentCommKind
```

**`comm_kind` is an explicit typed field** on `AgentContext` (not a `getattr` property,
not in `SessionId.metadata`). It is set once at pipeline build from
`descriptor.comm_kind` (see Task 8). This keeps `AgentCommKind` enum type safety intact
and satisfies the project's no-`getattr` / no-`hasattr` rule. The `current_turn_uuid`
property stays (reads `self.runtime`).

- [ ] **Step 2: Delete `tests/unit/multi_agent/test_comm_kind_session_id.py`**

This test exercises the deleted `AgentSessionMeta`/`DefaultSessionIdStrategy`. Remove the file.

- [ ] **Step 3: Delete `AgentSessionMeta` references everywhere**

Search and remove: `ctx.session_meta`, `.session_meta.` across `framework/`. Replace reads:
- `ctx.session_meta.agent_name` → `ctx.session.agent_name`
- `ctx.session_meta.invocation_id` → `ctx.session.snowflake` (subagent) or `None`
- `ctx.session_meta.comm_kind` → `ctx.comm_kind` (the new explicit field)
- `ctx.session_meta.conversation_id` → `ctx.session.metadata.get("conversation_id")` or `ctx.session.snowflake` for the main agent

Known consumers to update: `framework/hook/notification.py:42,56,73,98,99`,
`framework/hook/builtin/subagent_auto_send.py:84,85`,
`framework/hook/builtin/logging.py:50,51`,
`framework/hook/builtin/progress_report.py:39,40`.

- [ ] **Step 4: Run the core test suite**

Run: `python -m pytest tests/unit/core -x -q`
Expected: PASS after fixing construction sites in tests (change `AgentContext(session_id="x", ...)` → `AgentContext(session=SessionId.from_str("x.main"), ...)`).

- [ ] **Step 5: Commit (Phase 2 checkpoint)**

```bash
git add framework/core/agent.py framework/runtime/models.py tests/
git rm tests/unit/multi_agent/test_comm_kind_session_id.py
git commit -m "refactor(core): AgentContext.session + TurnIdentity.session are SessionId objects; delete AgentSessionMeta"
```

---

## Phase 3 — Framework routing & communication

### Task 7: `InputMessage.session_id: SessionId`

**Files:**
- Modify: `framework/core/types.py:46-77` (`InputMessage`)
- Adapt: every `InputMessage(content=..., session_id="x")` call site

- [ ] **Step 1: Change the field type**

In `framework/core/types.py`:
```python
from framework.core.session_id import SessionId

@dataclass
class InputMessage:
    content: str
    session: SessionId
    channel: str = field(default=DefaultValues.CHANNEL)
    # ... rest unchanged, but `session_id: str = "default"` → `session: SessionId`
```

Rename `session_id` → `session` for consistency with `AgentContext`. Update `TurnRequest` in `pipeline.py` similarly.

- [ ] **Step 2: Find all construction sites**

Run: `grep -rn "InputMessage(" framework/ examples/bot_project/bot/`
For each, ensure the caller passes a `SessionId` object. The primary construction sites:
- `framework/multi_agent/pool.py` (`_dispatch_*` methods): build `InputMessage` with the envelope's session.
- `examples/bot_project/bot/adapters/*`: inbound adapters create `InputMessage` — these must construct a `SessionId` via `SessionIdFactory` at inbound time (see Task 16).

- [ ] **Step 3: Adapt pool dispatch sites**

In `framework/multi_agent/pool.py`, `_dispatch_task_request` and `_dispatch_agent_message` currently do `InputMessage(content=..., session_id=session_id, metadata=metadata)`. Change to pass the resolved `SessionId`:

```python
# resolve the SessionId from registry or from_str
session = await self._session_registry.get(session_id) or SessionId.from_str(
    session_id, default_agent_name=descriptor.address.name
)
await instance.pipeline.process_message(
    InputMessage(content=task_prompt, session=session, metadata=metadata)
)
```

- [ ] **Step 4: Run pool tests**

Run: `python -m pytest tests/unit/multi_agent -x -q 2>&1 | head -40`
Expected: failures at sites still passing strings; fix each.

- [ ] **Step 5: Commit**

```bash
git add framework/core/types.py framework/multi_agent/pool.py
git commit -m "refactor(core): InputMessage.session is a SessionId object"
```

---

### Task 8: Wire `SessionRegistry` + `SessionStore` into `AgentPool`; registry-aware `DefaultMeshRouter`

**Files:**
- Modify: `framework/multi_agent/pool.py:57-112` (constructor + `_session_meta`)
- Modify: `framework/multi_agent/router.py` (full rewrite of `DefaultMeshRouter`)
- Modify: `framework/multi_agent/factory.py:273` (pass registry to router)
- Test: `tests/unit/multi_agent/test_router.py`

- [ ] **Step 1: Rewrite `DefaultMeshRouter`**

`framework/multi_agent/router.py` — replace the whole file:
```python
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from framework.core.session_id import SessionId
from framework.core.session_registry import SessionRegistry
from framework.core.types import InputMessage


@dataclass
class RouteResult:
    """Result of routing an input message to an agent-owned session."""

    session: SessionId
    prompt_modifier: str | None = None
    envelope_metadata: dict[str, Any] | None = None
    is_envelope: bool = False


class AgentMessageRouter(ABC):
    @abstractmethod
    def route(
        self,
        input_msg: InputMessage,
        default_agent_name: str = "main",
    ) -> RouteResult:
        ...


class DefaultMeshRouter(AgentMessageRouter):
    """Registry-first router. Reads agent identity from the SessionId object;
    never parses the string unless the registry is absent."""

    def __init__(
        self,
        registry: SessionRegistry | None = None,
        default_agent_name: str = "main",
    ) -> None:
        self._registry = registry
        self._default_agent_name = default_agent_name

    def route(
        self,
        input_msg: InputMessage,
        default_agent_name: str = "main",
    ) -> RouteResult:
        session = input_msg.session
        prompt_modifier = None
        metadata = input_msg.metadata or {}
        message_type = metadata.get("message_type", "agent_message")
        is_envelope = message_type in ("agent_message", "subagent_result", "rpc_request")

        if message_type == "subagent_result" and metadata.get("source_agent"):
            prompt_modifier = f"[Subagent {metadata['source_agent']} result]\n\n"

        return RouteResult(
            session=session,
            prompt_modifier=prompt_modifier,
            envelope_metadata=dict(metadata),
            is_envelope=is_envelope,
        )
```

Note: `RouteResult` now carries a `SessionId` instead of `agent_session_id`/`agent_name`/`conversation_id` strings. All `RouteResult` consumers update in Task 9.

- [ ] **Step 2: Update `AgentPool` constructor to hold registry + store**

In `framework/multi_agent/pool.py` constructor, add params and wire:
```python
from framework.core.session_id import SessionId, SessionIdFactory
from framework.core.session_registry import InMemorySessionRegistry, SessionRegistry
from framework.core.session_store import SessionStore

def __init__(
    self,
    broker: MessageBroker,
    agent_factory: AgentFactory,
    ...,
    session_registry: SessionRegistry | None = None,
    session_store: SessionStore | None = None,
) -> None:
    ...
    self._session_store = session_store
    self._session_registry = session_registry or InMemorySessionRegistry(store=session_store)
    self._session_factory = SessionIdFactory()
    ...
```

Replace the `SessionMeta` dataclass usage:
- Delete the `SessionMeta` dataclass (lines 37-45).
- Replace `self._session_meta: dict[str, SessionMeta]` → rely on `self._session_registry` for metadata; keep `self._session_locks` as-is (string keys).
- `_track_session(session_id, agent_name, is_dynamic=...)` becomes a registry register:
```python
def _track_session(self, session: SessionId, is_dynamic: bool = False) -> None:
    self._session_locks.setdefault(str(session), asyncio.Lock())
    meta = dict(session.metadata)
    meta["is_dynamic"] = is_dynamic
    updated = session.model_copy(update={"metadata": meta})
    self._schedule_registry_register(updated)
```
- `_touch_session(session_id)` → `self._schedule_registry_touch(session_id)`.
- Add helpers so fire-and-forget tasks don't silently swallow exceptions:
```python
def _schedule_registry_register(self, session: SessionId) -> None:
    """Fire-and-forget register with error logging."""
    async def _register():
        try:
            await self._session_registry.register(session)
        except Exception:
            logger.exception("Failed to register session %s in registry", session)
    asyncio.ensure_future(_register())

def _schedule_registry_touch(self, session_id: str) -> None:
    """Fire-and-forget touch with error logging."""
    async def _touch():
        try:
            await self._session_registry.touch(session_id)
        except Exception:
            logger.exception("Failed to touch session %s in registry", session_id)
    asyncio.ensure_future(_touch())
```
- Eviction code reads `is_dynamic` from registry records instead of `SessionMeta`.

- [ ] **Step 3: Pass registry to the router in `AgentFactory.create_agent`**

In `framework/multi_agent/factory.py:273`, change `router=DefaultMeshRouter()` → `router=DefaultMeshRouter(registry=self._session_registry, default_agent_name=descriptor.address.name)`. Add `session_registry` and `session_factory` params to `AgentFactory.__init__` / `DefaultAgentFactory.__init__` and store them.

- [ ] **Step 4: Update `RouteResult` consumers in `pipeline.py`**

In `framework/pipeline/pipeline.py:366-393`, `route_result.agent_session_id` / `.agent_name` / `.conversation_id` → `route_result.session`. Change:
```python
session = route_result.session
```
and use `session.agent_name`, `str(session)` etc.

When building `AgentContext` (`_build_runtime_and_context`), set the explicit
`comm_kind` field from the descriptor:
```python
agent_context.comm_kind = (
    self.agent_descriptor.comm_kind if self.agent_descriptor else None
)
```

- [ ] **Step 5: Rewrite the router test**

`tests/unit/multi_agent/test_router.py` — replace with tests that construct `InputMessage` with a `SessionId` and assert the router returns the same session and reads `agent_name` from it. Delete the legacy `test_defaults_external_conversation_to_main_agent_session` etc. tests that relied on string parsing.

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/unit/multi_agent/test_router.py tests/unit/multi_agent/test_core_runtime.py -x -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add framework/multi_agent/router.py framework/multi_agent/pool.py framework/multi_agent/factory.py framework/pipeline/pipeline.py tests/unit/multi_agent/test_router.py
git commit -m "refactor(multi_agent): registry-first routing; pool holds SessionRegistry + SessionStore"
```

---

### Task 9: `AgentCommunicationService` uses `SessionIdFactory`; subagent sessions linked via `parent_session_id`

**Files:**
- Modify: `framework/multi_agent/communication.py` (the `format`/`parse` calls in `_ensure_invocation`, `_create_dynamic_subagent`, `_send`)
- Test: `tests/unit/multi_agent/test_communication_service.py`, `tests/unit/multi_agent/test_dynamic_subagent_integration.py`

- [ ] **Step 1: Inject `SessionIdFactory` + registry into the service**

In `framework/multi_agent/communication.py:179-230` constructor, add:
```python
session_factory: SessionIdFactory | None = None,
session_registry: SessionRegistry | None = None,
```
Store `self._session_factory = session_factory or SessionIdFactory()` and `self._session_registry = session_registry`. Remove the `session_strategy: DefaultSessionIdStrategy` param and its import.

- [ ] **Step 2: Rewrite `_create_dynamic_subagent` session construction**

Replace the `self._session_strategy.format(...)` calls that build the child `session_id`. The new flow:
```python
# resolve parent SessionId from context
parent_session = context.session  # AgentContext.session is now SessionId
child_session = self._session_factory.create(
    agent_name=name,
    parent_session_id=parent_session,
    external_id=invocation_id,  # invocation_id becomes the snowflake
    metadata={"pool": self._pool_name} if self._pool_name else None,
)
await self._session_registry.register(child_session)

# trace/output dirs keyed by str(child_session)
trace_dir = runtime_dir / "trace" / str(child_session)
output_path = runtime_dir / "output" / str(child_session) / "OUTPUT.md"
```

For the `on_subagent_created` callback (parent→child relation recording), pass `str(child_session)` and `str(parent_session)`.

- [ ] **Step 3: Rewrite `_send` envelope construction**

`_send` currently builds `session_id` via `self._session_strategy.format(conversation_id=..., agent_name=target_agent, invocation_id=normalized_invocation_id)`. Replace with:
```python
target_session = self._session_factory.create(
    agent_name=target_agent,
    parent_session_id=None,
    external_id=normalized_invocation_id if target_kind == AgentCommKind.SUBAGENT else None,
)
await self._session_registry.register(target_session)
session_id_str = str(target_session)
```
Use `session_id_str` in the envelope's `agent_session_id` and the broker send.

- [ ] **Step 4: Drop `invocation_id` routing from envelope; align semantics**

In `framework/multi_agent/envelope.py`, keep the `invocation_id` field but document it as "the source subagent's snowflake, for trace correlation only". Routing uses `agent_session_id` (the full SessionId string). Update `build_agent_message` calls in `communication.py` to pass `invocation_id=target_session.snowflake`.

- [ ] **Step 5: Update the communication tests**

In `tests/unit/multi_agent/test_communication_service.py`, replace assertions on `DefaultSessionIdStrategy` output with assertions on the registry: after a send, `await registry.get(session_id_str)` returns a `SessionId` with the right `agent_name` and `parent_session_id`.

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/unit/multi_agent/test_communication_service.py tests/unit/multi_agent/test_dynamic_subagent_integration.py -x -q`
Expected: PASS (fix the large integration test's session_id string assertions).

- [ ] **Step 7: Commit**

```bash
git add framework/multi_agent/communication.py framework/multi_agent/envelope.py tests/unit/multi_agent/
git commit -m "refactor(multi_agent): communication uses SessionIdFactory; subagent sessions linked via parent_session_id"
```

---

### Task 10: Hooks read `ctx.session` object (`subagent_auto_send`, `notification`)

**Files:**
- Modify: `framework/hook/builtin/subagent_auto_send.py`
- Modify: `framework/hook/notification.py`
- Modify: `framework/hook/builtin/control_drain.py`

- [ ] **Step 1: Rewrite `SubagentAutoSendHook._notify_parent`**

In `framework/hook/builtin/subagent_auto_send.py:198-257`, remove the `DefaultSessionIdStrategy().parse(session_id)` logic. The parent inbox key is now `ctx.session.parent_session_id`:
```python
async def _notify_parent(self, ctx: AgentContext, xml: str) -> None:
    from framework.multi_agent.address import AgentAddress
    from framework.multi_agent.envelope import AgentMessageEnvelope

    session = ctx.session
    parent_session_id = session.parent_session_id
    if parent_session_id is None:
        logger.warning(
            "SubagentAutoSendHook: session %s has no parent_session_id", session
        )
        return
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
        conversation_id=str(session),
        agent_session_id=parent_session_id,
        invocation_id=session.snowflake,
    )
    try:
        await self._agent_bus.send(parent_session_id, envelope)
    except Exception:
        logger.exception("SubagentAutoSendHook: failed to notify parent")
```

Also in `finally_turn`, replace `session_id = ctx.session_id or ""` with `session = ctx.session; session_id_str = str(session)`.

- [ ] **Step 2: Rewrite `AgentNotificationService._notify_parent`**

In `framework/hook/notification.py:55-79`, replace the parse logic:
```python
async def _notify_parent(self, ctx: AgentContext, xml: str) -> None:
    session = ctx.session
    parent_session_id = session.parent_session_id
    if parent_session_id is None:
        return
    from framework.multi_agent.address import AgentAddress
    from framework.multi_agent.envelope import AgentMessageEnvelope

    envelope = AgentMessageEnvelope(
        payload={"content": xml, "message_type": "agent_result"},
        source=AgentAddress(name=session.agent_name),
        target=AgentAddress(name=self._session_strategy.main_agent_name or "main"),
        message_type="agent_result",
        conversation_id=str(session),
        agent_session_id=parent_session_id,
    )
    await self._agent_bus.send(parent_session_id, envelope)
```

Remove the `DefaultSessionIdStrategy` import; the service no longer needs it.

- [ ] **Step 3: Update `control_drain.py` and any other hook reading session strings**

Run `grep -rn "ctx.session_id\|session_id =" framework/hook/` and replace string reads with `ctx.session` object reads (use `str(ctx.session)` where a string is genuinely needed for paths/keys).

- [ ] **Step 4: Run hook tests**

Run: `python -m pytest tests/unit/hook -x -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add framework/hook/
git commit -m "refactor(hook): read ctx.session object instead of parsing session_id strings"
```

---

### Task 11: `AgentPipeline` consumes `SessionId` objects end-to-end

**Files:**
- Modify: `framework/pipeline/pipeline.py` (`_process_message`, `_build_runtime_and_context`, `_execute_turn`)
- Modify: `framework/pipeline/adapters.py` (`SessionPrefixStripAdapter` — delete or rewrite)

- [ ] **Step 1: Update `_build_runtime_and_context`**

In `framework/pipeline/pipeline.py:571-617`, the method parses `session_id` to build `TurnIdentity` and `AgentSessionMeta`. Replace with:
```python
def _build_runtime_and_context(
    self,
    session: SessionId,
    context_state: ContextState,
    ctx_mgr: ContextManager,
    *,
    input_metadata: dict[str, Any] | None = None,
) -> tuple[AgentContext, ContentEmitter]:
    from uuid import uuid4
    from framework.runtime.models import TurnIdentity

    self._injection_queues.setdefault(str(session), asyncio.Queue(maxsize=50))

    # self.agent.name is an abstract property on Agent — every agent has it
    turn_identity = TurnIdentity(
        agent_id=self.agent.name,
        session=session,
        turn_id=uuid4().hex,
        conversation_id=session.metadata.get("conversation_id"),
    )

    agent_context = AgentContext(
        system_prompt=context_state.system_prompt,
        history=context_state.history,
        tool_manager=self.tool_manager,
        session=session,
        max_iterations=self.max_iterations,
    )
    agent_context.system_prompt_pipeline = context_state.system_prompt_pipeline
    agent_context.identity = turn_identity
    # ... rest (runtime construction) unchanged except using str(session) for keys
```

- [ ] **Step 2: Update `_process_message` and `_execute_turn`**

- `_process_message`: `session_id = route_result.session` (a `SessionId`); all `_session_locks[str(session)]`, `_session_tasks[str(session)]` keyed by string. `input_msg.session` is the object.
- `_execute_turn`: replace `DefaultSessionIdStrategy().parse(raw_id)` with reading `agent_context.session.agent_name`. `conversation_id` comes from `session.metadata` or `session.snowflake`.

- [ ] **Step 3: Delete `SessionPrefixStripAdapter`**

In `framework/pipeline/adapters.py:336-371`, this adapter splits the session_id string to strip the agent prefix — exactly the pattern we're eliminating. Delete the class. Any IM adapter that used it should instead map the external id to a `SessionId` at inbound time (Task 16).

Also in `_try_intercept_control` (lines 145-147), replace `DefaultSessionIdStrategy().normalize(session_id)` with a no-op (the session is already a full `SessionId`).

- [ ] **Step 4: Run pipeline tests**

Run: `python -m pytest tests/unit/pipeline -x -q 2>&1 | head -40`
Expected: fix any construction-site issues; PASS.

- [ ] **Step 5: Commit**

```bash
git add framework/pipeline/
git commit -m "refactor(pipeline): consume SessionId objects; delete SessionPrefixStripAdapter"
```

---

### Task 12: `AgentMessageBus` and `LocalFileInboxServer` use string keys at boundaries

**Files:**
- Modify: `framework/multi_agent/bus.py:96-124` (`send` wakeup target name)
- Modify: `framework/multi_agent/inbox/server_local.py` (already string-keyed; verify)

- [ ] **Step 1: Fix `LocalAgentMessageBus.send` target resolution**

In `framework/multi_agent/bus.py:106-114`, the code parses the session_id to find the target agent name for the broker wakeup. Replace with reading from the envelope:
```python
target_name = envelope.target.name if envelope.target else session_id
```
Remove the `DefaultSessionIdStrategy().parse(session_id)` block entirely.

- [ ] **Step 2: Verify `LocalFileInboxServer`**

`framework/multi_agent/inbox/server_local.py` already uses `_safe_dir_name(session_id)` with a string param. Its callers now pass `str(session)`. No change needed inside the server, but confirm callers in `pool.py` / `bus.py` pass strings.

- [ ] **Step 3: Run bus + inbox tests**

Run: `python -m pytest tests/unit/multi_agent/test_agent_message_bus.py tests/unit/multi_agent/inbox -x -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add framework/multi_agent/bus.py
git commit -m "refactor(multi_agent): bus resolves wakeup target from envelope, not session_id parsing"
```

---

### Task 12.5: Broker bridge constructs `SessionId`; orphan isolation via factory

**Files:**
- Modify: `framework/messaging/broker_bridge.py:65-119` (`_broker_msg_to_input_message`)
- Modify: `framework/messaging/broker_bridge.py:329-350` (`_bridge_input`)
- Modify: `framework/core/types.py` (`OutputMessage.session_id` stays `str`)

- [ ] **Step 1: Inject `SessionIdFactory` + `default_agent_name` into `BrokerInputAdapter`**

`_broker_msg_to_input_message` needs a factory and a default agent name to build a `SessionId`. Add params to the function and to `BrokerInputAdapter.__init__`:
```python
def __init__(
    self,
    broker: MessageBroker,
    address: Address,
    deduplicator: Any | None = None,
    *,
    session_factory: SessionIdFactory | None = None,
    default_agent_name: str = "main",
) -> None:
    ...
    self._session_factory = session_factory or SessionIdFactory()
    self._default_agent_name = default_agent_name
```

- [ ] **Step 2: Rewrite `_broker_msg_to_input_message` to build a SessionId**

```python
def _broker_msg_to_input_message(
    msg: BrokerMessage,
    factory: SessionIdFactory,
    default_agent_name: str,
) -> InputMessage:
    payload = msg.payload
    sender = msg.sender
    metadata = dict(payload.get("metadata", {}))
    for key in (
        "conversation_id", "agent_session_id", "message_id",
        "in_reply_to", "message_type", "invocation_id",
    ):
        value = payload.get(key) or msg.headers.get(key)
        if value:
            metadata[key] = value

    _xml_message_types = frozenset(
        {"agent_message", "subagent_result", "task_request", "agent_result"}
    )
    content_fmt = None
    trunc_paths = None
    if metadata.get("message_type") in _xml_message_types:
        from framework.memory.core.message import ContentFormat
        content_fmt = ContentFormat.XML
        trunc_paths = ["content"]

    raw_session = payload.get("session_id", str(sender))

    # Orphan isolation: agent message missing conversation_id → synthetic id
    if sender.kind == "agent":
        cid = payload.get("conversation_id") or msg.headers.get("conversation_id")
        if not cid:
            import uuid
            logger.warning(
                "Orphan agent message from %s, isolating to synthetic session", sender
            )
            raw_session = f"orphan:{sender.name}:{uuid.uuid4().hex[:8]}"

    # Build a well-formed SessionId (raw_session becomes the encoded snowflake).
    session = factory.create(agent_name=default_agent_name, external_id=raw_session)

    return InputMessage(
        content=payload.get("content", ""),
        session=session,
        source=str(sender),
        sender_id=(sender.name if sender.kind == "user"
                   else payload.get("sender_id", DefaultValues.SENDER_ID)),
        channel=msg.headers.get("channel", DefaultValues.CHANNEL),
        chat_id=msg.headers.get("chat_id", DefaultValues.CHAT_ID),
        metadata=metadata,
        content_format=content_fmt,
        truncatable_paths=trunc_paths,
    )
```

Key point: the orphan `f"orphan:{...}:{...}"` value is passed as `external_id`, so it
is base58-encoded into a clean snowflake — the `:` separator never reaches `SessionId`.

- [ ] **Step 3: Update `_bridge_input` to serialize `str(msg.session)` into broker payloads**

In `_bridge_input` (lines 333-349), replace `msg.session_id` with `str(msg.session)`:
```python
broker_msg = BrokerMessage(
    payload={
        "content": msg.content,
        "session_id": str(msg.session),
        "metadata": msg.metadata,
        "sender_id": msg.sender_id,
        "chat_id": msg.chat_id,
        "conversation_id": str(msg.session),
    },
    ...
    headers={
        "channel": msg.channel,
        "chat_id": msg.chat_id,
        "conversation_id": str(msg.session),
    },
)
```

- [ ] **Step 4: Confirm `OutputMessage.session_id` stays `str`**

`OutputMessage.session_id` and `BrokerOutputAdapter.send(message, session_id: str)`
remain string-typed at the wire boundary. They are output payloads, not identity
objects. All sites that construct `OutputMessage` with a session pass `str(session)`.
Grep and verify: `grep -rn "OutputMessage(" framework/ examples/bot_project/bot/` —
each should use `str(session)`.

- [ ] **Step 5: Run broker bridge tests**

Run: `python -m pytest tests/unit/messaging -x -q 2>&1 | head -40`
Expected: PASS (fix tests asserting on the old orphan string format).

- [ ] **Step 6: Commit**

```bash
git add framework/messaging/broker_bridge.py framework/core/types.py
git commit -m "refactor(messaging): broker bridge builds SessionId via factory; orphan isolation encodes cleanly"
```

---

## Phase 4 — Business layer (bot_project)

Run bot tests from `examples/bot_project`.

### Task 13: Create `WorkspacePoolSessionStore` (business)

**Files:**
- Create: `examples/bot_project/bot/service/session_store.py`
- Create: `examples/bot_project/tests/test_session_store.py`

- [ ] **Step 1: Write the failing test**

`examples/bot_project/tests/test_session_store.py`:
```python
from __future__ import annotations

from pathlib import Path

import pytest

from bot.service.session_store import WorkspacePoolSessionStore
from framework.core.session_id import SessionIdFactory


@pytest.fixture
def factory() -> SessionIdFactory:
    return SessionIdFactory()


async def test_path_partitioned_by_workspace_and_pool(
    tmp_path: Path, factory: SessionIdFactory
):
    store = WorkspacePoolSessionStore(
        base_dir=tmp_path,
        workspace_resolver=lambda: "ws1",
        pool_resolver=lambda session: "coding",
    )
    session = factory.create(agent_name="main")
    await store.save(session)
    expected_dir = tmp_path / "ws1" / "coding"
    assert expected_dir.is_dir()
    files = list(expected_dir.glob("*.json"))
    assert len(files) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd examples/bot_project && python -m pytest tests/test_session_store.py -x`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `examples/bot_project/bot/service/session_store.py`**

```python
"""Business-layer session store: partitions SessionId records by workspace/pool.

Inherits framework's LocalFileSessionStore and overrides _path_for to add
<workspace>/<pool>/ subdirectories. Workspace and pool are business concepts;
the framework store has no awareness of them.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from framework.core.session_id import SessionId
from framework.core.session_store import LocalFileSessionStore, _safe_name


class WorkspacePoolSessionStore(LocalFileSessionStore):
    def __init__(
        self,
        base_dir: Path,
        workspace_resolver: Callable[[], str],
        pool_resolver: Callable[[SessionId], str],
    ) -> None:
        super().__init__(base_dir)
        self._workspace_resolver = workspace_resolver
        self._pool_resolver = pool_resolver

    def _path_for(self, session_id: str) -> Path:
        # Resolve the SessionId to determine the pool. The store holds the
        # record keyed by string; pool resolution needs the SessionId object,
        # which the registry provides at save time. For path computation we
        # rely on a precomputed pool stored on the record via metadata.
        return super()._path_for(session_id)

    async def save(self, session: SessionId) -> None:
        workspace = self._workspace_resolver()
        pool = self._pool_resolver(session)
        dir_ = self._base / workspace / pool
        dir_.mkdir(parents=True, exist_ok=True)
        path = dir_ / f"{_safe_name(str(session))}.json"
        import asyncio

        await asyncio.to_thread(path.write_text, session.model_dump_json(), "utf-8")

    async def get(self, session_id: str) -> SessionId | None:
        # Search all workspace/pool dirs for the record.
        import asyncio

        for path in self._collect_all_paths():
            if path.stem == _safe_name(session_id):
                text = await asyncio.to_thread(path.read_text, "utf-8")
                from framework.core.session_id import SessionId

                return SessionId.model_validate_json(text)
        return None

    def _collect_all_paths(self):
        if not self._base.is_dir():
            return []
        return [p for p in self._base.rglob("*.json") if p.is_file()]

    async def list_sessions(self) -> list:
        import asyncio

        from framework.core.session_id import SessionId

        sessions = []
        for path in self._collect_all_paths():
            text = await asyncio.to_thread(path.read_text, "utf-8")
            sessions.append(SessionId.model_validate_json(text))
        return sessions

    async def delete(self, session_id: str) -> None:
        import asyncio

        target = _safe_name(session_id)
        for path in self._collect_all_paths():
            if path.stem == target:
                await asyncio.to_thread(path.unlink, True)
                return

    async def get_children(self, parent_session_id: str) -> list:
        all_sessions = await self.list_sessions()
        return [s for s in all_sessions if s.parent_session_id == parent_session_id]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd examples/bot_project && python -m pytest tests/test_session_store.py -x`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd examples/bot_project
git add bot/service/session_store.py tests/test_session_store.py
git commit -m "feat(bot): add WorkspacePoolSessionStore partitioning SessionId by workspace/pool"
```

---

### Task 14: Wire `WorkspacePoolSessionStore` + `SessionRegistry` into the bot; delete `SessionRelationStore`

**Files:**
- Modify: `examples/bot_project/bot/service/web_ui_service.py`
- Modify: `examples/bot_project/bot/service/pool_builder.py`
- Delete: `examples/bot_project/bot/service/session_relation_store.py`
- Delete: `examples/bot_project/tests/test_session_relation_store.py`

- [ ] **Step 1: Construct the store in `web_ui_service.py`**

In `web_ui_service.py` `__init__`, replace the `SessionRelationStore` construction (lines 137-142) with:
```python
from bot.service.session_store import WorkspacePoolSessionStore
from framework.core.session_registry import InMemorySessionRegistry

self._session_store = WorkspacePoolSessionStore(
    sessions_dir,
    workspace_resolver=self._resolve_workspace,
    pool_resolver=self._resolve_pool_for_session,
)
self._session_registry = InMemorySessionRegistry(store=self._session_store)
```

Add a `_resolve_pool_for_session(self, session) -> str` helper that maps `session.agent_name` → pool via the existing `_agent_pool_map`. Remove the `_relation_store` attribute and all its uses (parent-child is now on `SessionId`).

- [ ] **Step 2: Inject registry + store into the pool builder**

In `bot/service/pool_builder.py`, pass `session_registry=self._session_registry` and `session_store=self._session_store` to the `AgentPool` constructor. Also pass `session_registry` to `AgentFactory`/`AgentCommunicationService` so subagent creation registers sessions.

- [ ] **Step 3: Delete `SessionRelationStore`**

```bash
cd examples/bot_project
git rm bot/service/session_relation_store.py tests/test_session_relation_store.py
```

Update `bot/webui/server.py` imports — replace `SessionRelationStore` usage with `SessionStore` queries (`get_children`, `get_parent` via `session.parent_session_id`).

- [ ] **Step 4: Run bot service tests**

Run: `cd examples/bot_project && python -m pytest tests/ -x -q 2>&1 | head -40`
Expected: fix import errors; PASS.

- [ ] **Step 5: Commit**

```bash
cd examples/bot_project
git add -A
git commit -m "refactor(bot): wire WorkspacePoolSessionStore + SessionRegistry; delete SessionRelationStore"
```

---

### Task 15: Rewrite WebUI `server.py` + `events.py` to use `SessionId` and the registry

**Files:**
- Modify: `examples/bot_project/bot/webui/server.py`
- Modify: `examples/bot_project/bot/webui/events.py`

- [ ] **Step 1: Remove string-parsing helpers in `server.py`**

Delete `_parse_session_id` and `_parse_session_parts` (lines 79-104). Replace their uses:
- `_handle_sessions`: query `self._session_store.list_sessions()` filtered by workspace/pool; build the response from `SessionId` fields directly:
```python
session_list = []
for session in await self._session_store.list_sessions():
    entry = {
        "session_id": str(session),
        "agent_name": session.agent_name,
        "pool": self._pool_of_agent(session.agent_name),
        "parent_session_id": session.parent_session_id,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "metadata": session.metadata,
    }
    session_list.append(entry)
session_list.sort(key=lambda s: s["updated_at"] or 0, reverse=True)
return web.json_response(session_list)
```
- `_ws_attach`, `_handle_get_messages`, `_handle_delete_session`: use `session.agent_name` / `str(session)` from the registry instead of parsing.

- [ ] **Step 2: Remove `_session_id`/`_conv_prefix` from `events.py`**

Delete `_session_id` (line 58-60) and `_conv_prefix` (line 63-65). The server now constructs `SessionId` via the factory and passes the object; event `session_id` fields take `str(session)`.

- [ ] **Step 3: Update `_handle_create_session`**

```python
session = self._session_factory.create(
    agent_name=agent_name,
    metadata={"pool": effective_pool},
)
set_conv_channel(session.snowflake, "websocket")
if self._pool_switch_callback is not None:
    self._pool_switch_callback(session.snowflake, effective_pool)
await self._session_registry.register(session)
return web.json_response({"session_id": str(session), "pool": effective_pool})
```

- [ ] **Step 4: Run webui tests**

Run: `cd examples/bot_project && python -m pytest tests/webui -x -q`
Expected: PASS (rewrite tests that asserted on conversation-prefix grouping).

- [ ] **Step 5: Commit**

```bash
cd examples/bot_project
git add bot/webui/
git commit -m "refactor(webui): use SessionId objects + registry; remove session_id string parsing"
```

---

### Task 16: Inbound adapters construct `SessionId` via factory

**Files:**
- Modify: `examples/bot_project/bot/adapters/channels.py`
- Modify: `examples/bot_project/bot/adapters/register_websocket.py` (if it constructs InputMessage)

- [ ] **Step 1: Find inbound InputMessage construction**

Run: `cd examples/bot_project && grep -rn "InputMessage(" bot/adapters/`

- [ ] **Step 2: Construct SessionId at inbound time**

For each adapter, when a message arrives with an external id (`conv_id` / IM group id), build:
```python
from framework.core.session_id import SessionIdFactory

session = self._session_factory.create(
    agent_name=resolved_agent_name,
    external_id=external_conv_id,
    metadata={"channel": self.name, "pool": pool_name},
)
await self._session_registry.register(session)
return InputMessage(content=text, session=session, ...)
```

Inject `session_factory` and `session_registry` into each adapter (via the existing `AdapterBuildContext`).

- [ ] **Step 3: Run adapter tests**

Run: `cd examples/bot_project && python -m pytest tests/ -x -q -k "adapter or channel" 2>&1 | head -40`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
cd examples/bot_project
git add bot/adapters/
git commit -m "refactor(bot): inbound adapters construct SessionId via factory"
```

---

## Phase 5 — Legacy deletion

### Task 17: Delete `DefaultSessionIdStrategy` and `framework/multi_agent/session_id.py`

**Files:**
- Delete: `framework/multi_agent/session_id.py`
- Modify: every remaining import of `framework.multi_agent.session_id`

- [ ] **Step 1: Find remaining references**

Run: `grep -rn "multi_agent.session_id\|DefaultSessionIdStrategy\|AgentSessionParts" framework/ examples/bot_project/bot/ tests/`
Expected: zero hits (all should be gone after Tasks 8-16). If any remain, fix them first.

- [ ] **Step 2: Delete the file**

```bash
git rm framework/multi_agent/session_id.py
```

- [ ] **Step 3: Run the full framework test suite**

Run: `python -m pytest tests/unit -x -q`
Expected: PASS, no import errors.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor(core): delete DefaultSessionIdStrategy — SessionId/SessionIdFactory replace it"
```

---

### Task 18: Delete `framework/multi_agent/utils.py` parse helpers (if any)

**Files:**
- Modify or Delete: `framework/multi_agent/utils.py`

- [ ] **Step 1: Inspect `utils.py`**

Run: `grep -n "session_id\|parse\|DefaultSession" framework/multi_agent/utils.py`

- [ ] **Step 2: Remove any session-id parsing helpers**

If the file only contains parse helpers, delete it. Otherwise strip the offending functions and update callers.

- [ ] **Step 3: Run tests**

Run: `python -m pytest tests/unit/multi_agent -x -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "chore(multi_agent): remove legacy session_id parse helpers"
```

---

## Phase 6 — Migration script (temporary, NOT committed)

### Task 19: Write one-shot migration script

**Files:**
- Create: `scripts/migrate_session_ids.py` (local only — add to `.gitignore` or delete after running)

- [ ] **Step 1: Write the migration script**

`scripts/migrate_session_ids.py`:
```python
"""One-shot migration: build SessionId records from legacy session_id strings.

NOT committed to the repo — run once per workspace, then delete.

Old formats:
  {conv}.{agent}                      -> main agent session
  {conv}.{agent}.{invocation_id}      -> subagent session

New SessionId:
  main:     snowflake = encode_snowflake(conv)
  subagent: snowflake = encode_snowflake(invocation_id)   # <-- invocation_id becomes snowflake
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# allow importing framework
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework.core.session_id import SessionId, SessionIdFactory, encode_snowflake, now_ms


def build_session(old_id: str, main_agent: str) -> SessionId:
    factory = SessionIdFactory()
    parts = old_id.split(".")
    if len(parts) == 2:
        conv, agent = parts
        return factory.create(agent_name=agent, external_id=conv)
    if len(parts) == 3:
        conv, agent, invocation_id = parts
        parent_session_id = f"{encode_snowflake(conv)}.{main_agent}"
        return factory.create(
            agent_name=agent,
            external_id=invocation_id,  # invocation_id becomes the snowflake
            parent_session_id=parent_session_id,
        )
    raise ValueError(f"unrecognized old session_id: {old_id}")


def mtime_ms(path: Path) -> int:
    return int(path.stat().st_mtime * 1000)


def migrate_sessions_dir(sessions_dir: Path, main_agent: str, pool: str) -> None:
    index_path = sessions_dir / "_session_index.jsonl"
    records: list[str] = []
    for jsonl in sorted(sessions_dir.glob("*.jsonl")):
        if jsonl.name.startswith("_"):
            continue
        old_id = jsonl.stem  # e.g. 626973b7592f.scout.0027c720
        try:
            session = build_session(old_id, main_agent)
        except ValueError:
            print(f"skip {old_id}")
            continue
        session = session.model_copy(
            update={
                "created_at": mtime_ms(jsonl),
                "updated_at": mtime_ms(jsonl),
                "metadata": {**session.metadata, "pool": pool},
            }
        )
        records.append(session.model_dump_json())
    index_path.write_text("\n".join(records) + "\n", encoding="utf-8")
    print(f"wrote {len(records)} records to {index_path}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--workspace", required=True, help="path to .modex/sessions root")
    p.add_argument("--main-agent", default="coding")
    p.add_argument("--pool", default="coding")
    args = p.parse_args()
    root = Path(args.workspace)
    for pool_dir in root.iterdir():
        if not pool_dir.is_dir():
            continue
        migrate_sessions_dir(pool_dir, args.main_agent, pool_dir.name)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Dry-run on one workspace**

Run (adjust path): `python scripts/migrate_session_ids.py --workspace examples/bot_project/.modex/sessions`
Expected: prints "wrote N records" per pool; inspect `_session_index.jsonl` output.

- [ ] **Step 3: Do NOT commit the script**

Add `scripts/migrate_session_ids.py` to `.gitignore` or delete it after running. The migration rules are documented in the spec (Section 8); the script is a throwaway tool.

- [ ] **Step 4: No commit** (script is intentionally uncommitted).

---

## Phase 7 — Final verification

### Task 20: Full test suite + import sanity

- [ ] **Step 1: Run the full framework test suite**

Run: `python -m pytest tests/unit -q`
Expected: all PASS (or only pre-existing unrelated failures).

- [ ] **Step 2: Run the full bot test suite**

Run: `cd examples/bot_project && python -m pytest tests/ -q`
Expected: all PASS.

- [ ] **Step 3: Verify no legacy references remain**

Run:
```bash
grep -rn "DefaultSessionIdStrategy\|AgentSessionMeta\|SessionRelationStore\|_parse_session" framework/ examples/bot_project/bot/ tests/
```
Expected: no hits in `.py` files (only doc/spec files may mention them historically).

- [ ] **Step 4: Smoke-run the bot**

Run: `cd examples/bot_project && python debug_main.py` (or the project's run command), send a message, confirm a turn completes and a `SessionId` record appears under `.modex/sessions/<workspace>/<pool>/`.

- [ ] **Step 5: Final commit if any cleanup**

```bash
git add -A
git commit -m "test: session-id redesign full suite green"
```

---

## Self-Review Notes

**Spec coverage:**
- §3 SessionId/Factory/now_ms → Tasks 1 ✓
- §4 SessionStore/LocalFile/WorkspacePool → Tasks 2, 13 ✓
- §5 SessionRegistry/InMemory → Task 3 ✓
- §6.1 AgentPool wiring → Task 8 ✓
- §6.2 DefaultMeshRouter → Task 8 ✓
- §6.3 AgentCommunicationService → Task 9 ✓
- §6.4 AgentPipeline → Task 11 ✓
- §6.5 type changes → Tasks 4, 5, 6, 7 ✓
- §6.6 persistence keys (safe_name, no persistence_key) → Tasks 2, 13 ✓
- §6.7 business cleanup → Tasks 14, 15, 16 ✓
- §6.8 broker bridge & orphan sessions → Task 12.5 ✓
- §7 file changes → covered across tasks ✓
- §8 migration rules (invocation_id → snowflake) → Task 19 ✓
- §9 frontend/API (full session_id, parent tree, updated_at) → Task 15 ✓
- Legacy deletion (no backward-compat) → Tasks 14, 17, 18 ✓
- comm_kind as explicit `AgentContext` field → Task 6 (field) + Task 8 (set from descriptor) ✓
- `SessionId` frozen → Task 1 (`model_config` + `test_session_id_is_frozen`) ✓
- `OutputMessage.session_id` stays str (wire) → Task 12.5 Step 4 ✓

**Type consistency:** `SessionId` field names used uniformly: `session_id`, `agent_name`, `parent_session_id`, `created_at`, `updated_at`, `metadata`. `AgentContext.session`, `InputMessage.session`, `TurnIdentity.session` all renamed consistently (was `session_id`). `AgentContext.comm_kind` is the single comm_kind source (was `session_meta.comm_kind`). `RouteResult.session` replaces the three string fields. `SessionId.snowflake` / `.is_subagent` / `.touch()` / `.from_str()` used consistently. `OutputMessage.session_id` and `AgentMessageEnvelope.agent_session_id` stay `str` at wire boundaries.

**No backward-compat shims:** `DefaultSessionIdStrategy`, `AgentSessionMeta`, `SessionRelationStore`, `SessionPrefixStripAdapter`, `_parse_session_*` all deleted, not adapted.
