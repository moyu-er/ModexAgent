from __future__ import annotations

from pathlib import Path

import pytest

from framework.memory.core.scope import MemoryContext, MemoryLayerName, SessionScope, UserScope
from framework.memory.registry import DefaultMemoryStoreRegistry


@pytest.mark.asyncio
async def test_default_registry_uses_layer_first_file_layout(tmp_path: Path) -> None:
    registry = DefaultMemoryStoreRegistry(tmp_path)
    context = MemoryContext(session_id="s1", user_id="u1", agent_id="main")

    session_storage = await registry.resolve(
        layer=MemoryLayerName.SESSION,
        scope=SessionScope(),
        context=context,
    )
    archive_storage = await registry.resolve(
        layer=MemoryLayerName.ARCHIVE,
        scope=UserScope(),
        context=context,
    )

    await session_storage.append_message({"role": "user", "content": "hello"})
    stored_entry = await archive_storage.append_log({"summary": "compressed"})

    assert (tmp_path / "session" / "s1" / "messages.jsonl").exists()
    assert (tmp_path / "archive" / "u1" / "archive.jsonl").exists()
    assert stored_entry["cursor"] == 1
    assert stored_entry["entry_id"] == 1


@pytest.mark.asyncio
async def test_default_registry_evicts_cached_scoped_storage(tmp_path: Path) -> None:
    registry = DefaultMemoryStoreRegistry(tmp_path)
    context = MemoryContext(session_id="s1")

    first = await registry.resolve(
        layer=MemoryLayerName.SESSION,
        scope=SessionScope(),
        context=context,
    )
    await registry.evict(layer=MemoryLayerName.SESSION, scope=SessionScope())
    second = await registry.resolve(
        layer=MemoryLayerName.SESSION,
        scope=SessionScope(),
        context=context,
    )

    assert first is not second
