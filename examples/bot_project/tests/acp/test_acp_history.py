"""Bot-side ACP history-source tests (T09 source portion, acp-adapter DESIGN §5.3).

The ACP load/replay path reads native session history as TYPED ``ChatMessage``
facts through the SAME source helpers the control facade uses
(``bot/control/history.py``) — one owner for the pool→MessageStore traversal
and one ``load_all_messages`` call site. The facade's raw eight-field
projection (order/fields/limit) is preserved verbatim and guarded here.

Covered:
- ``read_native_history`` — typed, oldest-first, full-fidelity (tool_calls,
  tool ids, extra fields preserved); soft-deleted records included per the
  ``load_all_messages`` contract;
- ``resolve_pool_message_store`` — the production pool_data → memory_system →
  store_registry traversal, extracted once (shared with the facade injection);
- ``read_pool_session_history`` — resolve + typed read end-to-end;
- facade projection shape preserved (characterization).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bot.control.history import (
    MessageStoreResolutionError,
    load_native_history,
    project_history_messages,
    read_native_history,
    read_pool_session_history,
    resolve_pool_message_store,
)

from modex_agent.core.message import ChatMessage, ToolCall
from modex_agent.memory.stores.scoped_in_memory import InMemoryScopedStorage

# ---------------------------------------------------------------------------
# Sample records (same shapes the SQLite/file stores assemble)
# ---------------------------------------------------------------------------

_RAW_HISTORY: list[dict[str, Any]] = [
    {
        "role": "user",
        "content": "inspect the config",
        "message_id": "m1",
        "created_at": 1_000,
    },
    {
        "role": "assistant",
        "content": None,
        "message_id": "m2",
        "created_at": 2_000,
        "tool_calls": [
            {"tool_name": "read", "arguments": {"path": "bot.yml"}, "call_id": "tc1"}
        ],
    },
    {
        "role": "tool",
        "content": "pool: default",
        "tool_call_id": "tc1",
        "tool_name": "read",
        "name": "read",
        "message_id": "m3",
        "created_at": 3_000,
    },
    {
        "role": "assistant",
        "content": "the config declares one pool",
        "message_id": "m4",
        "created_at": 4_000,
    },
    {
        "role": "user",
        "content": "superseded draft",
        "message_id": "m5",
        "created_at": 5_000,
        "_deleted": True,
    },
]


# ---------------------------------------------------------------------------
# Typed source read (ACP read_history)
# ---------------------------------------------------------------------------


async def test_read_native_history_returns_typed_oldest_first() -> None:
    store = InMemoryScopedStorage()
    await store.save_messages([dict(m) for m in _RAW_HISTORY])

    messages = await read_native_history(store)

    assert [m.role.value for m in messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]
    assert all(isinstance(m, ChatMessage) for m in messages)
    # Tool-call fidelity survives the typed read.
    assistant_call = messages[1].tool_calls
    assert assistant_call is not None and len(assistant_call) == 1
    assert isinstance(assistant_call[0], ToolCall)
    assert assistant_call[0].tool_name == "read"
    assert assistant_call[0].call_id == "tc1"
    tool_msg = messages[2]
    assert tool_msg.tool_call_id == "tc1"
    assert tool_msg.name == "read"
    # Extra fields (message_id) are preserved for stable replay ids.
    assert messages[0].message_id == "m1"  # type: ignore[attr-defined]


async def test_read_native_history_includes_soft_deleted_records() -> None:
    store = InMemoryScopedStorage()
    await store.save_messages([dict(m) for m in _RAW_HISTORY])

    raw = await load_native_history(store)

    assert any(m.get("_deleted") for m in raw)
    messages = await read_native_history(store)
    assert any(getattr(m, "_deleted", False) for m in messages)


async def test_load_native_history_is_the_facade_source_call() -> None:
    """One ``load_all_messages`` call site: helper output IS the store output."""
    store = InMemoryScopedStorage()
    await store.save_messages([dict(m) for m in _RAW_HISTORY])

    assert await load_native_history(store) == await store.load_all_messages()


# ---------------------------------------------------------------------------
# Pool → MessageStore resolution (shared with the facade injection)
# ---------------------------------------------------------------------------


class _StubRegistry:
    def __init__(self, store: Any) -> None:
        self._store = store
        self.calls: list[dict[str, Any]] = []

    async def resolve(self, *, layer: Any, scope: Any, context: Any) -> Any:
        self.calls.append({"layer": layer, "scope": scope, "context": context})
        return SimpleNamespace(messages=self._store)


class _StubMemorySystem:
    def __init__(self, registry: _StubRegistry) -> None:
        self.store_registry = registry


def _resources_with(
    pool_data: dict[str, Any] | None, *, target: Path = Path("/workspace")
) -> SimpleNamespace:
    return SimpleNamespace(pool_data=pool_data or {}, target=target)


async def test_resolve_pool_message_store_traverses_pool_data() -> None:
    store = InMemoryScopedStorage()
    registry = _StubRegistry(store)
    resources = _resources_with(
        {"main": SimpleNamespace(context_manager=SimpleNamespace(
            memory_system=_StubMemorySystem(registry)))}
    )

    resolved = await resolve_pool_message_store(
        resources, pool="main", session_id="inv1.coder"
    )

    assert resolved is store
    assert registry.calls[0]["context"].session_id == "inv1.coder"


async def test_resolve_pool_message_store_error_taxonomy() -> None:
    missing_pool = _resources_with(None)
    with pytest.raises(MessageStoreResolutionError) as exc_none:
        await resolve_pool_message_store(missing_pool, pool="", session_id="s")
    assert exc_none.value.code == "invalid_scope"

    unknown = _resources_with({"other": object()})
    with pytest.raises(MessageStoreResolutionError) as exc_pool:
        await resolve_pool_message_store(unknown, pool="main", session_id="s")
    assert exc_pool.value.code == "pool_not_found"

    no_memory = _resources_with(
        {"main": SimpleNamespace(context_manager=SimpleNamespace(memory_system=None))}
    )
    with pytest.raises(MessageStoreResolutionError) as exc_mem:
        await resolve_pool_message_store(no_memory, pool="main", session_id="s")
    assert exc_mem.value.code == "memory_system_unavailable"


async def test_read_pool_session_history_end_to_end() -> None:
    store = InMemoryScopedStorage()
    await store.save_messages([dict(m) for m in _RAW_HISTORY])
    resources = _resources_with(
        {"main": SimpleNamespace(context_manager=SimpleNamespace(
            memory_system=_StubMemorySystem(_StubRegistry(store))))}
    )

    messages = await read_pool_session_history(
        resources, pool="main", session_id="inv1.coder"
    )

    assert [m.role.value for m in messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]


# ---------------------------------------------------------------------------
# Facade projection preservation (characterization — shape must not drift)
# ---------------------------------------------------------------------------


def test_facade_projection_shape_is_unchanged() -> None:
    items = project_history_messages([dict(m) for m in _RAW_HISTORY], limit=10)

    # Newest-first, capped by limit.
    assert [m.message_id for m in items] == ["m5", "m4", "m3", "m2", "m1"]
    limited = project_history_messages([dict(m) for m in _RAW_HISTORY], limit=2)
    assert [m.message_id for m in limited] == ["m5", "m4"]
    # Eight-field allowlist: internal markers stripped, created_at str(ms).
    first = items[0].model_dump()
    assert first["created_at"] == "5000"
    assert set(first) <= {
        "role",
        "content",
        "tool_calls",
        "tool_call_id",
        "tool_name",
        "name",
        "created_at",
        "message_id",
    }
    # Tool pair survives the raw projection untouched.
    tool_item = items[2]
    assert tool_item.role == "tool"
    assert tool_item.tool_call_id == "tc1"
    assert tool_item.tool_name == "read"
    assistant_item = items[3]
    assert assistant_item.tool_calls == [
        {"tool_name": "read", "arguments": {"path": "bot.yml"}, "call_id": "tc1"}
    ]
