from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.core.scope import MemoryLayerName
from modex_agent.core.types import MessageRole
from modex_agent.memory.stores.dir_archive import DirArchiveStorage
from modex_agent.memory.stores.scoped_file import DefaultScopedStorage
from modex_agent.memory.stores.scoped_in_memory import InMemoryScopedStorage


def _messages_with_compact() -> list[dict[str, str]]:
    messages = [
        {
            "id": f"m{index}",
            "role": str(MessageRole.USER),
            "content": str(index),
        }
        for index in range(5)
    ]
    messages.append(
        {
            "id": "compact",
            "role": str(MessageRole.COMPACT),
            "content": "summary",
        }
    )
    return messages


async def test_default_scoped_load_all_filters_compact_and_applies_limit(
    tmp_path: Path,
) -> None:
    store = DefaultScopedStorage(tmp_path, layer=MemoryLayerName.SESSION)
    messages = _messages_with_compact()
    await store.save_messages(messages)

    assert await store.load_all_messages() == messages[:5]
    assert await store.load_all_messages(limit=3) == messages[2:5]
    assert await store.load_all_messages(limit=0) == []
    assert await store.load_messages() == messages


async def test_in_memory_load_all_filters_compact_and_applies_limit() -> None:
    store = InMemoryScopedStorage()
    messages = _messages_with_compact()
    await store.save_messages(messages)

    assert await store.load_all_messages() == messages[:5]
    assert await store.load_all_messages(limit=3) == messages[2:5]
    assert await store.load_all_messages(limit=0) == []
    assert await store.load_messages() == messages


async def test_default_scoped_load_all_rejects_negative_limit(
    tmp_path: Path,
) -> None:
    store = DefaultScopedStorage(tmp_path, layer=MemoryLayerName.SESSION)

    with pytest.raises(ValueError, match="limit must be non-negative"):
        await store.load_all_messages(limit=-1)


async def test_in_memory_load_all_rejects_negative_limit() -> None:
    store = InMemoryScopedStorage()

    with pytest.raises(ValueError, match="limit must be non-negative"):
        await store.load_all_messages(limit=-1)


async def test_dir_archive_load_all_rejects_negative_limit(tmp_path: Path) -> None:
    store = DirArchiveStorage(tmp_path)

    with pytest.raises(ValueError, match="limit must be non-negative"):
        await store.load_all_messages(limit=-1)


async def test_dir_archive_load_all_accepts_limit(tmp_path: Path) -> None:
    store = DirArchiveStorage(tmp_path)

    assert await store.load_all_messages(limit=0) == []
    assert await store.load_all_messages(limit=3) == []
