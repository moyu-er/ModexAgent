from __future__ import annotations

import inspect

import pytest

from framework.memory.core.scope import MemoryContext, MemoryLayerName, SessionScope, UserScope
from framework.memory.core.storage import MemoryStorage
from framework.memory.registry import InMemoryStoreRegistry
from framework.memory.stores import DefaultScopedStorage, InMemoryScopedStorage


@pytest.mark.asyncio
async def test_in_memory_registry_resolves_one_storage_per_layer_scope() -> None:
    registry = InMemoryStoreRegistry()
    context = MemoryContext(session_id="s1", user_id="u1")

    session_storage = await registry.resolve(
        layer=MemoryLayerName.SESSION,
        scope=SessionScope(),
        context=context,
    )
    same_session_storage = await registry.resolve(
        layer=MemoryLayerName.SESSION,
        scope=SessionScope(),
        context=context,
    )
    archive_storage = await registry.resolve(
        layer=MemoryLayerName.ARCHIVE,
        scope=UserScope(),
        context=context,
    )

    assert session_storage is same_session_storage
    assert session_storage is not archive_storage
    assert session_storage.get_lock() is not archive_storage.get_lock()


@pytest.mark.asyncio
async def test_in_memory_registry_lists_records_by_layer_role_and_file() -> None:
    registry = InMemoryStoreRegistry()
    main_context = MemoryContext(session_id="main", agent_id="main")
    peer_context = MemoryContext(session_id="peer", agent_id="peer")

    main_storage = await registry.resolve(
        layer=MemoryLayerName.SESSION,
        scope=SessionScope(),
        context=main_context,
    )
    await main_storage.save_messages([{"role": "user", "content": "hello"}])
    await registry.resolve(
        layer=MemoryLayerName.SESSION,
        scope=SessionScope(),
        context=peer_context,
    )

    default_records = await registry.list_records(layer=MemoryLayerName.SESSION)
    all_records = await registry.list_records(layer=MemoryLayerName.SESSION, agent_roles=None)
    message_records = await registry.list_records(
        layer=MemoryLayerName.SESSION,
        has_file="messages",
        agent_roles=None,
    )

    assert [record.scope_key for record in default_records] == ["main"]
    assert {record.scope_key for record in all_records} == {"main", "peer"}
    assert [record.scope_key for record in message_records] == ["main"]


def test_memory_storage_contract_is_scoped() -> None:
    scoped_methods = [
        "get",
        "set",
        "delete",
        "list_keys",
        "load_messages",
        "save_messages",
        "append_message",
        "append_log",
        "read_logs",
        "save_logs",
        "get_last_cursor",
        "set_last_cursor",
    ]

    for method_name in scoped_methods:
        signature = inspect.signature(getattr(MemoryStorage, method_name))
        assert "scope_key" not in signature.parameters, method_name

    assert "lock_key" not in inspect.signature(MemoryStorage.get_lock).parameters
    assert not hasattr(MemoryStorage, "list_scope_records")
    assert not hasattr(MemoryStorage, "ensure_scope_metadata")


def test_public_storage_exports_only_scoped_storage() -> None:
    import framework.memory.stores as stores

    assert stores.DefaultScopedStorage is DefaultScopedStorage
    assert stores.InMemoryScopedStorage is InMemoryScopedStorage
    assert not hasattr(stores, "FileStorage")
    assert not hasattr(stores, "InMemoryStorage")
