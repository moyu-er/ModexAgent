"""MessageStore conformance — same assertions for ``file`` and ``sqlite`` backends.

File: :class:`DefaultScopedStorage` (one instance implementing all four split
store ABCs).
SQLite: :class:`SqliteMessageStore`.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest

from modex_agent.core.scope import MemoryLayerName, RecordScope
from modex_agent.memory.core.split_stores import MessageStore
from modex_agent.memory.stores.scoped_file import DefaultScopedStorage
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters.message_store import SqliteMessageStore


def msg(mid: str, content: str = "x", **extra: object) -> dict[str, Any]:
    """Build a minimal message dict with an id."""
    result: dict[str, Any] = {"id": mid, "role": "user", "content": content}
    result.update(extra)
    return result


@pytest.fixture(params=["file", "sqlite"])
async def message_store(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    scope: RecordScope,
) -> AsyncGenerator[MessageStore]:
    """Parametrized MessageStore — file (DefaultScopedStorage) or sqlite."""
    if request.param == "file":
        yield DefaultScopedStorage(
            tmp_path / "msg_file",
            layer=MemoryLayerName.SESSION,
        )
    else:
        mgr = ConnectionManager(tmp_path / "workspace.db", DatabaseKind.WORKSPACE)
        await mgr.open()
        # ttl=0 so cleanup_expired picks up soft-deleted immediately
        yield SqliteMessageStore(mgr, scope, ttl_seconds=0.0)
        await mgr.close()


class TestMessageStoreConformance:
    """Same behavior on both backends."""

    async def test_load_empty_returns_empty(self, message_store: MessageStore) -> None:
        assert await message_store.load_messages() == []

    async def test_save_then_load_roundtrip(self, message_store: MessageStore) -> None:
        messages = [msg("m1"), msg("m2")]
        await message_store.save_messages(messages)
        loaded = await message_store.load_messages()
        assert len(loaded) == 2
        assert loaded[0]["id"] == "m1"
        assert loaded[1]["id"] == "m2"

    async def test_save_replaces(self, message_store: MessageStore) -> None:
        await message_store.save_messages([msg("m1")])
        await message_store.save_messages([msg("m2"), msg("m3")])
        loaded = await message_store.load_messages()
        assert [m["id"] for m in loaded] == ["m2", "m3"]

    async def test_append_message(self, message_store: MessageStore) -> None:
        await message_store.save_messages([msg("m1")])
        await message_store.append_message(msg("m2"))
        loaded = await message_store.load_messages()
        assert [m["id"] for m in loaded] == ["m1", "m2"]

    async def test_get_revision_empty(self, message_store: MessageStore) -> None:
        rev = await message_store.get_revision()
        assert rev.message_count == 0

    async def test_get_revision_after_save(self, message_store: MessageStore) -> None:
        await message_store.save_messages([msg("m1"), msg("m2")])
        rev = await message_store.get_revision()
        assert rev.message_count == 2

    async def test_delete_message_returns_true(self, message_store: MessageStore) -> None:
        await message_store.save_messages([msg("m1"), msg("m2")])
        assert await message_store.delete_message("m1") is True
        loaded = await message_store.load_messages()
        assert [m["id"] for m in loaded] == ["m2"]

    async def test_delete_missing_returns_false(self, message_store: MessageStore) -> None:
        assert await message_store.delete_message("nope") is False

    async def test_prune_messages(self, message_store: MessageStore) -> None:
        await message_store.save_messages([msg("m1"), msg("m2"), msg("m3")])
        count, pruned = await message_store.prune_messages(1)
        assert count == 2
        assert len(pruned) == 2
        loaded = await message_store.load_messages()
        assert len(loaded) == 1
        assert loaded[0]["id"] == "m3"

    async def test_prune_zero_removes_all(self, message_store: MessageStore) -> None:
        await message_store.save_messages([msg("m1"), msg("m2")])
        count, _ = await message_store.prune_messages(0)
        assert count == 2
        assert await message_store.load_messages() == []


class TestRetainMessages:
    """``retain_messages`` — only active messages are retained; others removed.

    FILE backend: hard-deletes (gone entirely).
    SQLite backend: soft-deletes (visible via ``load_all_messages``).
    Both conform: ``load_messages`` excludes removed messages.
    """

    async def test_retain_keeps_only_specified(self, message_store: MessageStore) -> None:
        await message_store.save_messages([msg("m1"), msg("m2"), msg("m3")])
        rev = await message_store.get_revision()
        result = await message_store.retain_messages([msg("m2")], rev)
        assert result is not None
        loaded = await message_store.load_messages()
        assert len(loaded) == 1
        assert loaded[0]["id"] == "m2"

    async def test_retain_revision_mismatch_returns_none(self, message_store: MessageStore) -> None:
        await message_store.save_messages([msg("m1")])
        stale_rev = type(await message_store.get_revision())(
            message_count=999,
            updated_at=(await message_store.get_revision()).updated_at,
            version=999,
        )
        result = await message_store.retain_messages([], stale_rev)
        assert result is None

    async def test_retain_all_kept_no_change(self, message_store: MessageStore) -> None:
        await message_store.save_messages([msg("m1"), msg("m2")])
        rev = await message_store.get_revision()
        await message_store.retain_messages([msg("m1"), msg("m2")], rev)
        loaded = await message_store.load_messages()
        assert [m["id"] for m in loaded] == ["m1", "m2"]


class TestLoadAllMessages:
    """``load_all_messages`` — returns including soft-deleted (SQLite) or same
    as ``load_messages`` (FILE)."""

    async def test_load_all_equals_load_when_no_pruning(self, message_store: MessageStore) -> None:
        await message_store.save_messages([msg("m1"), msg("m2")])
        active = await message_store.load_messages()
        all_msgs = await message_store.load_all_messages()
        assert len(all_msgs) == len(active)
