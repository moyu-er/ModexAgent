from __future__ import annotations

import time

import pytest

from framework.core.types import MessageRole
from framework.memory.core.layers import MemoryLayerSet, PendingPrunedInputMemoryManager
from framework.memory.core.scope import MemoryContext, MemoryLayerName, SessionScope
from framework.memory.layers.config import PendingPrunedInputMemoryConfig
from framework.memory.layers.factory import MemoryLayerFactory
from framework.memory.layers.pending import (
    PendingPrunedInputEntry,
    ScopedPendingPrunedInputMemoryManager,
)
from framework.memory.registry.in_memory import InMemoryStoreRegistry


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

        async def clear(self, context):
            return None

    class DummySession:
        pass

    pending = DummyPending()
    layer_set = MemoryLayerSet(session=DummySession(), pending=pending)  # type: ignore[arg-type]
    assert layer_set.pending is pending


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
