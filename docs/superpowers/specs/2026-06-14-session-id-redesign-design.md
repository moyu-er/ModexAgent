# Session ID Redesign Design

**Date:** 2026-06-14
**Status:** Design
**Scope:** `framework/` core + `examples/bot_project` session management

## 1. Problem Statement

The current `session_id` is a bare string (`{conversation_id}.{agent_name}[.{invocation_id}]`)
that simultaneously serves as routing key, memory-isolation key, persistence key, API
identifier, and UI grouping key. The format assumption (`.` separator, agent_name as second
segment) leaks into every layer:

- Business code (`bot/webui/server.py`, `bot/webui/transcript_store.py`) re-implements
  `split(".")` parsing in multiple places.
- `framework/multi_agent/session_id.py:DefaultSessionIdStrategy.parse` carries legacy
  compatibility code in the core parser.
- `AgentContext` holds three overlapping identity sources (`session_id: str`,
  `TurnIdentity`, `AgentSessionMeta`) with no consistency guarantee.
- The format is rigid: adding pool/tenant/workspace/nested-subagent dimensions cannot be
  done without breaking the parse contract.

The fundamental flaw: `session_id` is a bare `str`, so the type system cannot prevent
components from parsing it. Business and framework code are both tempted to split it.

## 2. Design Principles

1. **`SessionId` is a first-class object**, not a bare string. It carries an explicit
   `__str__` and `__hash__` so it can be used wherever a string was used, but the object's
   fields are the authoritative source of truth.
2. **`session_id` string is opaque to business logic.** Framework and business code never
   parse the `session_id` string to determine `agent_name` — they read `SessionId.agent_name`.
   String parsing exists only as a last-resort fallback when no registry/store record exists.
3. **Agent identity is bound at session creation**, resolved via an authoritative
   `SessionRegistry` (runtime cache) backed by a `SessionStore` (persistent). `default_agent_name`
   for recovery is provided by `AgentPool` (the pool's main agent), never by `AgentPipeline`.
4. **Subagent sessions are independent.** Each subagent gets a freshly generated snowflake.
   Parent-child relationship is tracked via `parent_session_id`, forming a tree.
5. **Framework provides the abstraction; business provides the workspace/pool partitioning.**
   `SessionStore` ABC + `LocalFileSessionStore` live in `framework/`. Business inherits
   `LocalFileSessionStore` to add workspace/pool directory layout.
6. **Workspace and pool are business concepts.** Framework session storage has zero awareness
   of them.
7. **No data migration compatibility.** Old format files need not be readable post-refactor.
   A one-shot migration script (not committed to the repo) handles existing workspace data.

## 3. Core Data Model

### 3.1 `SessionId` (pydantic)

Location: `framework/core/session_id.py`

```python
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SessionId(BaseModel):
    """First-class session identifier.

    ``session_id`` is the complete display id (``snowflake.agentName``), used for
    UI, logs, wire transport, and persistence file names. Internal logic reads
    the explicit fields; it does NOT parse the string.

    Required fields: ``session_id``, ``agent_name``.
    Optional fields default to ``None``.

    The model is **frozen**: once created it cannot be mutated in place. This
    makes it safe to use as a dict key / set member, since ``__hash__`` derives
    from the immutable ``session_id`` string. Updates produce a new instance via
    ``model_copy(update={...})`` (see ``touch()``).
    """

    model_config = ConfigDict(frozen=True)

    session_id: str = Field(..., description="Complete display id: snowflake.agentName")
    agent_name: str = Field(..., description="Bound agent name")
    parent_session_id: str | None = Field(default=None)
    created_at: int | None = Field(default=None, description="ms Unix epoch")
    updated_at: int | None = Field(default=None, description="ms Unix epoch")
    metadata: dict[str, Any] = Field(default_factory=dict)

    def __str__(self) -> str:
        return self.session_id

    def __hash__(self) -> int:
        return hash(self.session_id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SessionId):
            return NotImplemented
        return self.session_id == other.session_id

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

        Emits a warning when the value has no separator or an empty agent_name
        suffix. Does NOT consult the registry — callers should check the
        registry first.
        """
        if "." not in value:
            logger.warning(
                "SessionId %r has no separator; treating as bare snowflake", value
            )
            agent_name = default_agent_name or "unknown"
        else:
            _snowflake, _, suffix = value.rpartition(".")
            agent_name = suffix or default_agent_name or "unknown"
            if not suffix:
                logger.warning("SessionId %r has empty agent_name suffix", value)
        return cls(
            session_id=value,
            agent_name=agent_name,
        )
```

### 3.2 `SessionIdFactory`

Location: `framework/core/session_id.py`

Generates new `SessionId` instances. The snowflake is shortened via
`base58(sha256(raw)[:12])` (≈17 chars, collision-safe for session volumes).

```python
class SessionIdFactory:
    def __init__(self) -> None:
        ...

    def create(
        self,
        agent_name: str,
        *,
        parent_session_id: SessionId | str | None = None,
        external_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionId:
        raw = external_id or self._generate_snowflake()
        encoded = self._encode(raw)          # base58(sha256(raw)[:12])
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
```

- `external_id` is the IM-provided id (e.g. QQ group id). It forms the snowflake
  part only, never the complete session_id.
- `agent_name` suffix equals the bound agent (no aliasing).

### 3.3 `now_ms()`

`int(time.time() * 1000)`. Existing float-second timestamps (`AgentPool.SessionMeta`)
are migrated to ms integers as part of this refactor.

## 4. Persistence Layer

### 4.1 `SessionStore` ABC

Location: `framework/core/session_store.py`

```python
class SessionStore(ABC):
    @abstractmethod
    async def save(self, session: SessionId) -> None: ...

    @abstractmethod
    async def get(self, session_id: str) -> SessionId | None: ...

    @abstractmethod
    async def delete(self, session_id: str) -> None: ...

    @abstractmethod
    async def list_sessions(self) -> list[SessionId]: ...

    @abstractmethod
    async def get_children(self, parent_session_id: str) -> list[SessionId]: ...
```

No workspace/pool parameters — that is business's concern.

### 4.2 `LocalFileSessionStore`

Location: `framework/core/session_store.py`

Flat directory, one JSON file per session (keyed by the `session_id` string with
minimal path-safe escaping). No workspace/pool awareness.

```python
class LocalFileSessionStore(SessionStore):
    def __init__(self, base_dir: Path) -> None: ...
    def _path_for(self, session_id: str) -> Path:
        return self._base / f"{_safe_name(session_id)}.json"
    async def save(self, session: SessionId) -> None: ...
    async def get(self, session_id: str) -> SessionId | None: ...
    async def list_sessions(self) -> list[SessionId]: ...
    async def get_children(self, parent_session_id: str) -> list[SessionId]: ...
```

### 4.3 Business `WorkspacePoolSessionStore`

Location: `examples/bot_project/bot/service/session_store.py`

Inherits `LocalFileSessionStore`, overrides `_path_for` to partition by
`<workspace>/<pool>/`. The existing `SessionRelationStore` is folded into this
store (parent-child becomes a field of `SessionId`, no separate `_relations.json`).

## 5. Runtime Cache

### 5.1 `SessionRegistry` ABC + `InMemorySessionRegistry`

Location: `framework/core/session_registry.py`

```python
class SessionRegistry(ABC):
    @abstractmethod
    async def register(self, session: SessionId) -> None: ...
    @abstractmethod
    async def get(self, session_id: str) -> SessionId | None: ...
    @abstractmethod
    async def touch(self, session_id: str) -> None: ...


class InMemorySessionRegistry(SessionRegistry):
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

All operations are `async` and guarded by `asyncio.Lock` for concurrency safety.

## 6. Integration Points

### 6.1 `AgentPool`

- Holds `_session_registry: SessionRegistry` and injects a `SessionStore`.
- `_session_meta` is replaced by the registry: each tracked session is a `SessionId`.
- Provides `default_agent_name` (pool's main agent) to `DefaultMeshRouter`.
- `get_lock(session_id)` continues to key on `str(session_id)` (lock is not the
  object itself).

### 6.2 `DefaultMeshRouter`

- Constructor receives `SessionRegistry` and `default_agent_name` from the pool.
- `route()` consults the registry first; falls back to `SessionId.from_str` only
  when the registry has no record.
- `InputMessage.session_id` is now a `SessionId`; `route` reads
  `session.agent_name` directly.

### 6.3 `AgentCommunicationService`

- On subagent creation, calls `SessionIdFactory.create(...)` with
  `parent_session_id` set to the sender's `SessionId`.
- Registers the new `SessionId` in the registry.

### 6.4 `AgentPipeline`

- `_build_runtime_and_context` uses the `SessionId` passed in via
  `InputMessage.session_id` (no string parsing).
- No longer provides `default_agent_name`; that comes from the pool via the
  router/registry.

### 6.5 Type changes

| Type | Before | After |
|---|---|---|
| `InputMessage.session_id` | `str` | `SessionId` (renamed to `session`) |
| `AgentContext.session_id` | `str` | `SessionId` (renamed to `session`) |
| `AgentContext.comm_kind` | via `AgentSessionMeta` | explicit `comm_kind: AgentCommKind \| None` field on `AgentContext`, set from `descriptor.comm_kind` at pipeline build. **No `getattr`** — type-safe field. |
| `TurnIdentity.session_id` | `str` | `SessionId` (renamed to `session`) |
| `MemoryContext.session_id` | `str` | `SessionId` (renamed to `session`) |
| `OutputMessage.session_id` | `str` | **stays `str`** — it is a wire/boundary payload field consumed by `OutputAdapter`. Construction sites pass `str(session)`. `AgentCommKind` routing reads come off `ctx` instead. |
| `AgentMessageEnvelope.agent_session_id` | `str` | **stays `str`** — wire field; set to `str(session)` at construction. |
| `AgentSessionMeta` | separate frozen dataclass | **deleted**; fields folded into `SessionId` + `AgentContext.comm_kind` |

**`comm_kind` rule:** It is an *agent* property (from `AgentDescriptor.comm_kind`), not a
session property. It lives as an explicit typed field on `AgentContext`, populated once at
pipeline build from the descriptor. It is **not** placed in `SessionId.metadata` (metadata
is `dict[str, Any]`; pulling an enum out of it loses type safety) and is **not** read via
`getattr`/`hasattr`.

### 6.6 Persistence keys

All storage points use the `session_id` string directly (with minimal
`_safe_name` escaping for path safety), since workspace + pool partitioning
already provides isolation:

- `LocalFileInboxServer._safe_dir_name(session_id)`
- `JSONLTranscriptStore._safe_name(session_id)`
- trace/output directories: `trace/{safe_name(session_id)}`

No separate `persistence_key` abstraction.

### 6.8 Broker bridge & orphan sessions

`framework/messaging/broker_bridge.py:65` (`_broker_msg_to_input_message`) and
`_bridge_input` both construct `InputMessage` from broker messages. Under the
new design they must construct a `SessionId` via `SessionIdFactory`:

- Normal path: `session_id = payload.get("session_id") or str(sender)` → wrap with
  `SessionId.from_str(..., default_agent_name=...)` resolved against the registry.
- **Orphan isolation** (agent message missing `conversation_id`, currently
  `f"orphan:{sender.name}:{uuid}"`): the `:`-separated synthetic id does **not**
  match the `{snowflake}.{agent}` format and would trip `from_str`'s "no separator"
  warning. The orphan path must instead call
  `factory.create(agent_name=<default>, external_id=f"orphan:{sender.name}:{uuid}")`,
  producing a well-formed `{encoded}.<agent>` id that registers cleanly.

`broker_bridge._bridge_input` puts `msg.session_id` into broker message payloads as a
string (`"session_id": msg.session_id`, `"conversation_id": msg.session_id`). With
`InputMessage.session` now an object, these become `str(msg.session)`.

`BrokerOutputAdapter.send(message, session_id: str)` and `_bridge_output_topic` consume
`OutputMessage` + a string `session_id`. These stay string-typed at the boundary
(`OutputMessage.session_id` remains `str`; callers pass `str(session)`).

### 6.7 Business layer cleanup

- `bot/webui/server.py`: remove `_parse_session_id` / `_parse_session_parts`;
  use the registry to resolve agent_name and parent.
- `bot/webui/transcript_store.py`: keep `_safe_name`; conversation grouping uses
  `SessionId.parent_session_id` chain instead of string prefix splitting.
- `bot/service/session_relation_store.py`: superseded by `WorkspacePoolSessionStore`
  (parent-child is a `SessionId` field).

## 7. File Change List

### New files
- `framework/core/session_id.py` — `SessionId`, `SessionIdFactory`, `now_ms`
- `framework/core/session_store.py` — `SessionStore` ABC + `LocalFileSessionStore`
- `framework/core/session_registry.py` — `SessionRegistry` ABC + `InMemorySessionRegistry`
- `examples/bot_project/bot/service/session_store.py` — `WorkspacePoolSessionStore`

### Modified files
- `framework/core/types.py` — `InputMessage.session_id: SessionId`
- `framework/core/agent.py` — `AgentContext.session_id: SessionId`; fold `AgentSessionMeta`
- `framework/runtime/models.py` — `TurnIdentity.session_id: SessionId`
- `framework/memory/core/scope.py` — `MemoryContext.session_id: SessionId`
- `framework/multi_agent/session_id.py` — deprecate `DefaultSessionIdStrategy`; keep a
  thin adapter only if needed
- `framework/multi_agent/router.py` — registry-aware routing
- `framework/multi_agent/pool.py` — hold registry/store; replace `_session_meta`
- `framework/multi_agent/communication.py` — `SessionIdFactory` on subagent creation
- `framework/pipeline/pipeline.py` — consume `SessionId` objects
- `framework/multi_agent/inbox/server_local.py` — unchanged (already safe_name based)
- `bot/webui/server.py` — registry-based lookups
- `bot/webui/transcript_store.py` — keep safe_name
- `bot/service/web_ui_service.py` — wire `WorkspacePoolSessionStore`

### Out-of-repo deliverable
- One-shot migration script (temporary, not committed). Scans all workspaces'
  transcript/inbox/trace/output and builds `SessionId` records from the old
  format, writing the new session index. See Section 9 for the mapping rules.

## 8. Data Migration Rules (one-shot script)

The migration script converts old `session_id` formats into `SessionId` records:

| Old format | New `SessionId` |
|---|---|
| `{conv}.{agent}` (main agent) | `session_id` = `{encoded(conv)}.{agent}`; snowflake = encoded conv; `parent_session_id` = None |
| `{conv}.{agent}.{invocation_id}` (subagent) | **snowflake = encoded `invocation_id`**; `session_id` = `{encoded(invocation_id)}.{agent}`; `parent_session_id` = parent main-agent session id |

Key rule for subagents: the old **`invocation_id` segment becomes the new snowflake
part**, not `conv`. This preserves the original per-invocation identity so resumed
subagent sessions keep a stable id.

- `agent_name` is taken from the second segment (the bound agent).
- `parent_session_id` for a subagent is resolved to its conversation's main-agent
  session (`{encoded(conv)}.{main_agent}`).
- `created_at` / `updated_at` are filled from the transcript file mtime (ms epoch).
- Output: one `SessionId` JSON record per session, written into the new session index.

## 9. Frontend & API Impact

### 9.1 Session id in the frontend

The frontend currently displays and tracks sessions using the **conversation prefix**
(the segment before the first `.`), which is a lossy simplification. After the refactor:

- The frontend uses the **complete `session_id`** (e.g. `{encoded}.{agent}`) as the
  canonical session identifier.
- Subagent sessions are no longer inferred by string prefix; they appear as **child
  nodes** of their parent session via `parent_session_id`, forming a tree.

### 9.2 Session list API response

`GET /api/sessions` (and the WebSocket attach payload) returns one entry per
`SessionId`, carrying the fields the frontend needs to build the tree and sort:

```jsonc
{
  "session_id": "5xK9pQ2mN7vRkL3a.reviewer",
  "agent_name": "reviewer",
  "pool": "main",
  "parent_session_id": "5xK9pQ2mN7vRkL3a.main",
  "created_at": 1718300000000,
  "updated_at": 1718300120000,
  "metadata": { "...": "any SessionId.metadata fields" }
}
```

- `parent_session_id` enables tree construction (main-agent session = root).
- `updated_at` (ms epoch) drives sort order and recency display.
- `metadata` is passed through so any `SessionId.metadata` field the frontend needs
  (e.g. labels, status) is available without API churn.

### 9.3 Frontend changes

- Replace conversation-prefix grouping with a **tree built from `parent_session_id`**.
- Use the full `session_id` as the WebSocket attach/forward key (already done in
  the backend; the frontend must stop truncating).
- Sort sessions by `updated_at` descending.

## 10. Risks & Mitigations

1. **Type-signature churn.** Every `session_id: str` becomes `SessionId`, touching many
   adapters and tests. Mitigation: do framework core first, then adapters, then tests.
2. **`__hash__`/`__eq__` on `session_id` only.** Two `SessionId` objects with the same
   string but different `metadata` compare equal and hash identically — intentional, since
   identity is the string.
3. **Registry/cache staleness.** Mitigation: registry always writes through to the store;
   startup loads from store. The store is authoritative.
4. **Concurrency.** `InMemorySessionRegistry` uses `asyncio.Lock`. `asyncio.Lock` is
   not thread-safe; this framework is single-loop asyncio, acceptable.
5. **IM adapter integration.** Adapters must construct `SessionId` via the factory at
   inbound time, passing the IM id as `external_id`. The main-agent name comes from the
   pool, injected into the adapter.
6. **`asyncio.Lock` keying.** `AgentPool.get_lock` keeps `str(session_id)` keying; locks
   are not serialized objects.

## 11. Testing Strategy

- Unit: `SessionId` pydantic round-trip, `from_str` warnings, `touch`, `__hash__`/`__eq__`.
- Unit: `SessionIdFactory` snowflake encoding uniqueness over N generations.
- Unit: `LocalFileSessionStore` save/get/delete/list/children.
- Unit: `InMemorySessionRegistry` concurrent register/get under `asyncio.Lock`.
- Integration: `DefaultMeshRouter` resolves agent via registry; falls back to `from_str`
  with warning when missing.
- Integration: subagent creation registers a new `SessionId` with `parent_session_id`;
  registry+store reflect the tree.
- Integration: WebUI session list uses the store, no string parsing in `server.py`.
