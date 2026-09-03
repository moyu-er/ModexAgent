from __future__ import annotations

import json
from pathlib import Path

import pytest

from modex_agent.memory.registry import DefaultMemoryStoreRegistry
from modex_agent.memory.scope import (
    MemoryContext,
    MemoryLayerName,
    ScopeRecord,
    SessionScope,
    UserScope,
    scope_path_key,
)
from modex_agent.memory.stores import DefaultScopedStorage, InMemoryScopedStorage


@pytest.mark.asyncio
async def test_file_registry_resolves_one_storage_per_layer_scope(
    tmp_path: Path,
) -> None:
    registry = DefaultMemoryStoreRegistry(tmp_path)
    context = MemoryContext(session_id="s1", user_id="u1")

    session_bundle = await registry.resolve(
        layer=MemoryLayerName.SESSION,
        scope=SessionScope(),
        context=context,
    )
    same_session_bundle = await registry.resolve(
        layer=MemoryLayerName.SESSION,
        scope=SessionScope(),
        context=context,
    )
    archive_bundle = await registry.resolve(
        layer=MemoryLayerName.ARCHIVE,
        scope=UserScope(),
        context=context,
    )

    assert session_bundle.messages is same_session_bundle.messages
    assert session_bundle.messages is not archive_bundle.messages


@pytest.mark.asyncio
async def test_file_registry_lists_records_by_layer_role_and_file(
    tmp_path: Path,
) -> None:
    registry = DefaultMemoryStoreRegistry(tmp_path)
    main_context = MemoryContext(session_id="main", agent_id="main")
    subagent_context = MemoryContext(session_id="subagent", agent_id="subagent")

    main_bundle = await registry.resolve(
        layer=MemoryLayerName.SESSION,
        scope=SessionScope(),
        context=main_context,
    )
    await main_bundle.messages.save_messages([{"role": "user", "content": "hello"}])
    await registry.resolve(
        layer=MemoryLayerName.SESSION,
        scope=SessionScope(),
        context=subagent_context,
    )

    default_records = await registry.list_records(layer=MemoryLayerName.SESSION)
    all_records = await registry.list_records(layer=MemoryLayerName.SESSION, agent_roles=None)
    message_records = await registry.list_records(
        layer=MemoryLayerName.SESSION,
        has_file="messages",
        agent_roles=None,
    )

    main_key = scope_path_key(SessionScope(), main_context)
    subagent_key = scope_path_key(SessionScope(), subagent_context)
    assert [record.scope_key for record in default_records] == [main_key]
    assert {record.scope_key for record in all_records} == {main_key, subagent_key}
    assert [record.scope_key for record in message_records] == [main_key]


@pytest.mark.asyncio
async def test_file_registry_scope_metadata_uses_model_json_shape(tmp_path: Path) -> None:
    registry = DefaultMemoryStoreRegistry(tmp_path)
    context = MemoryContext(session_id="s1", user_id="u1")

    await registry.resolve(
        layer=MemoryLayerName.SESSION,
        scope=SessionScope(),
        context=context,
    )

    metadata_path = tmp_path / "session" / "s1" / ".scope.json"
    data = json.loads(metadata_path.read_text(encoding="utf-8"))
    record = ScopeRecord.model_validate(data)
    assert data["context"] == context.model_dump(mode="json")
    assert data["layer"] == "session"
    assert data["agent_role"] == "main"
    assert record.context == context


def test_public_storage_exports_only_scoped_storage() -> None:
    import modex_agent.memory.stores as stores

    assert stores.DefaultScopedStorage is DefaultScopedStorage
    assert stores.InMemoryScopedStorage is InMemoryScopedStorage
    assert not hasattr(stores, "FileStorage")
    assert not hasattr(stores, "InMemoryStorage")
