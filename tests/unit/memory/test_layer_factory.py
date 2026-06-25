from __future__ import annotations

import pytest

from modex_agent.memory.core.consolidation import MemoryUpdate
from modex_agent.memory.core.layers import MemoryLayerSet
from modex_agent.memory.core.models import ArchiveEntry
from modex_agent.core.scope import MemoryContext
from modex_agent.memory.layers import (
    MemoryLayerConfigSet,
    MemoryLayerFactory,
    SessionMemoryConfig,
)
from modex_agent.memory.registry import InMemoryStoreRegistry


@pytest.mark.asyncio
async def test_single_user_factory_builds_fieldized_layers_with_scoped_storage() -> None:
    registry = InMemoryStoreRegistry()
    layers = MemoryLayerFactory.single_user(
        registry=registry,
        config=MemoryLayerConfigSet(),
    )
    context = MemoryContext(session_id="s1", user_id="u1")

    assert isinstance(layers, MemoryLayerSet)
    assert layers.archive is not None
    assert layers.knowledge is not None

    await layers.session.add_messages(context, [{"role": "user", "content": "hello"}])
    messages = await layers.session.get_recent_messages(context)
    stored_archive = await layers.archive.append(
        context,
        ArchiveEntry(summary="summary", metadata={"source": "test"}),
    )
    await layers.knowledge.ensure_defaults(context, {"memory": "seed"})

    assert [message.content for message in messages] == ["hello"]
    assert stored_archive.entry_id == 1
    assert (await layers.knowledge.get_file(context, "memory")) == "seed"

    records = await registry.list_records(agent_roles=None)
    assert {(str(record.layer), record.scope_key) for record in records} == {
        ("session", "s1"),
        ("archive", "u1"),
        ("knowledge", "u1"),
    }


@pytest.mark.asyncio
async def test_session_only_factory_excludes_archive_and_knowledge() -> None:
    registry = InMemoryStoreRegistry()
    layers = MemoryLayerFactory.session_only(
        registry=registry,
        config=SessionMemoryConfig(max_messages=2),
    )
    context = MemoryContext(session_id="s1")

    await layers.session.add_messages(
        context,
        [
            {"role": "user", "content": "one"},
            {"role": "user", "content": "two"},
            {"role": "user", "content": "three"},
        ],
    )

    assert layers.archive is None
    assert layers.knowledge is None
    assert [message.content for message in await layers.session.get_recent_messages(context)] == [
        "two",
        "three",
    ]


@pytest.mark.asyncio
async def test_factory_knowledge_layer_applies_memory_update() -> None:
    registry = InMemoryStoreRegistry()
    layers = MemoryLayerFactory.single_user(
        registry=registry,
        config=MemoryLayerConfigSet(),
    )
    context = MemoryContext(user_id="u1")

    assert layers.knowledge is not None
    result = await layers.knowledge.apply_update(
        context,
        MemoryUpdate(file_name="MEMORY.md", content="remember this", mode="append"),
    )

    assert result == "remember this"
    assert await layers.knowledge.get_file(context, "MEMORY.md") == "remember this"
