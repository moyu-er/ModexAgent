"""Regression: ``ScopedSessionMemoryManager.add_messages`` must preserve
soft-deleted tombstones on the SQLite backend.

The per-row state machine (``normal -> pinned -> soft_deleted -> DELETE``)
requires that appending new messages does NOT physically remove soft-deleted
rows.  Physical removal is reserved for ``cleanup_expired`` (TTL) and
``delete_session_rows`` (session GC).  Previously ``_add_messages_locked``
used ``load_messages()`` (which filters soft-deleted) + ``save_messages()``
(hard DELETE + re-INSERT), destroying every tombstone on each append.
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from modex_agent.core.scope import RecordScope
from modex_agent.memory.core.split_stores import MemoryStoreBundle
from modex_agent.memory.layers.session import ScopedSessionMemoryManager
from modex_agent.memory.scope import MemoryContext
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters.cursor_store import SqliteCursorStore
from modex_agent.persistence.adapters.kv_store import SqliteKVStore
from modex_agent.persistence.adapters.message_store import SqliteMessageStore


class _PoolScopedRecordScope(RecordScope):
    """Test-only RecordScope subclass with pool dimension (ADR-0028)."""

    pool: str | None = None


def _msg(mid: str, content: str = "x") -> dict[str, object]:
    return {"id": mid, "role": "user", "content": content}


@pytest.fixture
async def sqlite_session(
    tmp_path: Path,
) -> AsyncGenerator[tuple[ScopedSessionMemoryManager, SqliteMessageStore]]:
    scope = _PoolScopedRecordScope(pool="default", session_id="s1", agent_id="main")
    mgr = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await mgr.open()
    message_store = SqliteMessageStore(mgr, scope, ttl_seconds=0.0)
    bundle = MemoryStoreBundle(
        messages=message_store,
        kv=SqliteKVStore(mgr, scope),
        cursors=SqliteCursorStore(mgr, scope),
    )

    async def factory(_ctx: MemoryContext) -> MemoryStoreBundle:
        return bundle

    session_mgr = ScopedSessionMemoryManager(factory)
    yield session_mgr, message_store
    await mgr.close()


class TestAddMessagesPreservesSoftDeletedTombstones:
    async def test_tombstones_survive_append_after_retain(
        self,
        sqlite_session: tuple[ScopedSessionMemoryManager, SqliteMessageStore],
    ) -> None:
        session_mgr, message_store = sqlite_session
        ctx = MemoryContext(session_id="s1")

        await message_store.save_messages(
            [_msg("m1"), _msg("m2"), _msg("m3"), _msg("m4"), _msg("m5")]
        )

        rev = await message_store.get_revision()
        await message_store.retain_messages([_msg("m4"), _msg("m5")], rev)

        assert [m["id"] for m in await message_store.load_messages()] == ["m4", "m5"]
        assert len(await message_store.load_all_messages()) == 5

        await session_mgr.add_messages(ctx, [_msg("m6")])

        assert [m["id"] for m in await message_store.load_messages()] == [
            "m4",
            "m5",
            "m6",
        ]

        all_after = await message_store.load_all_messages()
        assert len(all_after) == 6, (
            f"Expected 6 (3 tombstones + 3 active), got {len(all_after)}; "
            f"tombstones physically deleted by add_messages"
        )
        deleted = {m["id"] for m in all_after if m.get("_deleted") is True}
        assert deleted == {"m1", "m2", "m3"}

        rev_after = await message_store.get_revision()
        assert rev_after.message_count == 3

    async def test_tombstones_survive_multiple_append_rounds(
        self,
        sqlite_session: tuple[ScopedSessionMemoryManager, SqliteMessageStore],
    ) -> None:
        session_mgr, message_store = sqlite_session
        ctx = MemoryContext(session_id="s1")

        await message_store.save_messages([_msg("m1"), _msg("m2"), _msg("m3")])
        rev = await message_store.get_revision()
        await message_store.retain_messages([_msg("m3")], rev)

        await session_mgr.add_messages(ctx, [_msg("m4")])
        await session_mgr.add_messages(ctx, [_msg("m5")])

        all_msgs = await message_store.load_all_messages()
        assert len(all_msgs) == 5
        deleted_ids = {m["id"] for m in all_msgs if m.get("_deleted") is True}
        assert deleted_ids == {"m1", "m2"}
        assert [m["id"] for m in await message_store.load_messages()] == [
            "m3",
            "m4",
            "m5",
        ]

    async def test_tombstones_still_reaped_by_cleanup_expired(
        self,
        sqlite_session: tuple[ScopedSessionMemoryManager, SqliteMessageStore],
    ) -> None:
        session_mgr, message_store = sqlite_session
        ctx = MemoryContext(session_id="s1")

        await message_store.save_messages([_msg("m1"), _msg("m2"), _msg("m3")])
        rev = await message_store.get_revision()
        await message_store.retain_messages([_msg("m3")], rev)

        await session_mgr.add_messages(ctx, [_msg("m4")])
        assert len(await message_store.load_all_messages()) == 4

        time.sleep(0.01)
        removed = await message_store.cleanup_expired()
        # m1/m2 soft-deleted tombstones + the stale superseded copy of m3.
        assert removed == 3
        assert len(await message_store.load_all_messages()) == 2

    async def test_tombstones_survive_replace_messages(
        self,
        sqlite_session: tuple[ScopedSessionMemoryManager, SqliteMessageStore],
    ) -> None:
        session_mgr, message_store = sqlite_session
        ctx = MemoryContext(session_id="s1")

        await message_store.save_messages([_msg("m1"), _msg("m2"), _msg("m3")])
        rev = await message_store.get_revision()
        await message_store.retain_messages([_msg("m3")], rev)
        assert len(await message_store.load_all_messages()) == 3

        await session_mgr.replace_messages(ctx, [_msg("m3"), _msg("m5")])

        assert [m["id"] for m in await message_store.load_messages()] == ["m3", "m5"]
        all_after = await message_store.load_all_messages()
        assert len(all_after) == 4, (
            f"Expected 4 (2 tombstones + 2 active), got {len(all_after)}; "
            f"tombstones physically deleted by replace_messages"
        )
        deleted = {m["id"] for m in all_after if m.get("_deleted") is True}
        assert deleted == {"m1", "m2"}

    async def test_tombstones_survive_replace_messages_if_revision(
        self,
        sqlite_session: tuple[ScopedSessionMemoryManager, SqliteMessageStore],
    ) -> None:
        session_mgr, message_store = sqlite_session
        ctx = MemoryContext(session_id="s1")

        await message_store.save_messages([_msg("m1"), _msg("m2"), _msg("m3"), _msg("m4")])
        rev = await message_store.get_revision()
        await message_store.retain_messages([_msg("m3"), _msg("m4")], rev)
        assert len(await message_store.load_all_messages()) == 4

        rev2 = await message_store.get_revision()
        result = await session_mgr.replace_messages_if_revision(
            ctx, [_msg("m3"), _msg("m4"), _msg("m5")], rev2
        )
        assert result is not None

        assert [m["id"] for m in await message_store.load_messages()] == [
            "m3",
            "m4",
            "m5",
        ]
        all_after = await message_store.load_all_messages()
        assert len(all_after) == 5, (
            f"Expected 5 (2 tombstones + 3 active), got {len(all_after)}; "
            f"tombstones physically deleted by replace_messages_if_revision"
        )
        deleted = {m["id"] for m in all_after if m.get("_deleted") is True}
        assert deleted == {"m1", "m2"}
