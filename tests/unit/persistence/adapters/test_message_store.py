"""Tests for :class:`SqliteMessageStore` — state machine + revision + TTL."""

from __future__ import annotations

import time

import pytest

from modex_agent.core.message import MessageRole
from modex_agent.core.scope import RecordScope
from modex_agent.persistence import ConnectionManager
from modex_agent.persistence.adapters.message_store import SqliteMessageStore

from .conftest import msg


class TestMessageLoadSaveAppend:
    async def test_load_empty_returns_empty_list(self, message_store: SqliteMessageStore) -> None:
        assert await message_store.load_messages() == []

    async def test_append_and_load_preserves_order(self, message_store: SqliteMessageStore) -> None:
        for i in range(3):
            await message_store.append_message(msg(f"m{i}", str(i)))

        loaded = await message_store.load_messages()
        assert [m["id"] for m in loaded] == ["m0", "m1", "m2"]

    async def test_save_replaces_all_messages(self, message_store: SqliteMessageStore) -> None:
        await message_store.append_message(msg("old"))
        await message_store.save_messages([msg("a"), msg("b")])

        loaded = await message_store.load_messages()
        assert [m["id"] for m in loaded] == ["a", "b"]

    async def test_append_returns_increasing_revision(
        self, message_store: SqliteMessageStore
    ) -> None:
        rev0 = await message_store.append_message(msg("m0"))
        rev1 = await message_store.append_message(msg("m1"))

        assert rev0.message_count == 1
        assert rev1.message_count == 2
        assert rev1.version > rev0.version

    async def test_get_revision_reflects_active_count(
        self, message_store: SqliteMessageStore
    ) -> None:
        await message_store.append_message(msg("m0"))
        await message_store.append_message(msg("m1"))

        rev = await message_store.get_revision()
        assert rev.message_count == 2

    async def test_message_id_field_alias(self, message_store: SqliteMessageStore) -> None:
        """Messages using ``message_id`` instead of ``id`` are matched by pin/delete."""
        await message_store.append_message({"message_id": "alt0", "role": "user", "content": "y"})

        await message_store.pin_message("alt0")
        loaded = await message_store.load_messages()
        assert loaded[0].get("_pinned") is True


class TestLoadAllMessages:
    async def test_load_all_excludes_compact(
        self, message_store: SqliteMessageStore
    ) -> None:
        await message_store.append_message(msg("before"))
        await message_store.append_message(msg("compact", role=MessageRole.COMPACT))
        await message_store.append_message(msg("after"))

        loaded = await message_store.load_all_messages()

        assert [message["id"] for message in loaded] == ["before", "after"]

    async def test_load_messages_still_includes_compact(
        self, message_store: SqliteMessageStore
    ) -> None:
        await message_store.append_message(msg("compact", role=MessageRole.COMPACT))

        loaded = await message_store.load_messages()

        assert [message["role"] for message in loaded] == [str(MessageRole.COMPACT)]

    async def test_load_all_limit_returns_latest_non_compact_chronologically(
        self, message_store: SqliteMessageStore
    ) -> None:
        await message_store.append_message(msg("m0"))
        await message_store.append_message(msg("compact-0", role=MessageRole.COMPACT))
        await message_store.append_message(msg("m1"))
        await message_store.append_message(msg("compact-1", role=MessageRole.COMPACT))
        await message_store.append_message(msg("m2"))
        await message_store.prune_messages(1)

        loaded = await message_store.load_all_messages(limit=2)

        assert [message["id"] for message in loaded] == ["m1", "m2"]
        assert loaded[0].get("_deleted") is True

    async def test_load_all_limit_zero_returns_empty(
        self, message_store: SqliteMessageStore
    ) -> None:
        await message_store.append_message(msg("m0"))

        loaded = await message_store.load_all_messages(limit=0)

        assert loaded == []

    async def test_load_all_negative_limit_raises(
        self, message_store: SqliteMessageStore
    ) -> None:
        with pytest.raises(ValueError, match="^limit must be non-negative$"):
            await message_store.load_all_messages(limit=-1)


class TestMessageStateMachine:
    async def test_pin_marks_pinned(self, message_store: SqliteMessageStore) -> None:
        await message_store.append_message(msg("m0"))

        await message_store.pin_message("m0")

        loaded = await message_store.load_messages()
        assert loaded[0].get("_pinned") is True

    async def test_pin_unknown_id_is_noop(self, message_store: SqliteMessageStore) -> None:
        await message_store.append_message(msg("m0"))

        await message_store.pin_message("nonexistent")

        loaded = await message_store.load_messages()
        assert "_pinned" not in loaded[0]

    async def test_unpin_removes_pin(self, message_store: SqliteMessageStore) -> None:
        await message_store.append_message(msg("m0"))
        await message_store.pin_message("m0")

        await message_store.unpin_message("m0")

        loaded = await message_store.load_messages()
        assert "_pinned" not in loaded[0]

    async def test_unpin_not_pinned_is_noop(self, message_store: SqliteMessageStore) -> None:
        await message_store.append_message(msg("m0"))

        await message_store.unpin_message("m0")

        loaded = await message_store.load_messages()
        assert "_pinned" not in loaded[0]

    async def test_delete_removes_message(self, message_store: SqliteMessageStore) -> None:
        for i in range(3):
            await message_store.append_message(msg(f"m{i}"))

        deleted = await message_store.delete_message("m1")

        assert deleted is True
        loaded = await message_store.load_messages()
        assert [m["id"] for m in loaded] == ["m0", "m2"]

    async def test_delete_unknown_returns_false(self, message_store: SqliteMessageStore) -> None:
        await message_store.append_message(msg("m0"))

        deleted = await message_store.delete_message("nonexistent")

        assert deleted is False
        assert len(await message_store.load_messages()) == 1


class TestPruneMessages:
    async def test_prune_returns_pruned_content_and_trims(
        self, message_store: SqliteMessageStore
    ) -> None:
        for i in range(5):
            await message_store.append_message(msg(f"m{i}", str(i)))

        count, pruned = await message_store.prune_messages(3)

        assert count == 2
        assert [m["id"] for m in pruned] == ["m0", "m1"]
        remaining = await message_store.load_messages()
        assert [m["id"] for m in remaining] == ["m2", "m3", "m4"]

    async def test_prune_noop_when_under_limit(self, message_store: SqliteMessageStore) -> None:
        await message_store.append_message(msg("m0"))
        await message_store.append_message(msg("m1"))

        count, pruned = await message_store.prune_messages(10)

        assert count == 0
        assert pruned == []
        assert len(await message_store.load_messages()) == 2

    async def test_prune_pinned_survive(self, message_store: SqliteMessageStore) -> None:
        for i in range(5):
            await message_store.append_message(msg(f"m{i}", str(i)))
        await message_store.pin_message("m0")

        count, pruned = await message_store.prune_messages(3)

        assert count == 1
        assert [m["id"] for m in pruned] == ["m1"]
        remaining = await message_store.load_messages()
        assert "m0" in [m["id"] for m in remaining]

    async def test_prune_zero_keeps_only_pinned(self, message_store: SqliteMessageStore) -> None:
        for i in range(3):
            await message_store.append_message(msg(f"m{i}", str(i)))
        await message_store.pin_message("m1")

        count, pruned = await message_store.prune_messages(0)

        assert count == 2
        assert {m["id"] for m in pruned} == {"m0", "m2"}
        remaining = await message_store.load_messages()
        assert [m["id"] for m in remaining] == ["m1"]

    async def test_pruned_messages_not_in_load(self, message_store: SqliteMessageStore) -> None:
        """Soft-deleted messages are hidden from load_messages."""
        for i in range(5):
            await message_store.append_message(msg(f"m{i}"))

        await message_store.prune_messages(3)

        loaded = await message_store.load_messages()
        assert len(loaded) == 3


class TestCleanupExpired:
    async def test_cleanup_expired_physically_deletes_soft_deleted(
        self, connection: ConnectionManager, scope: RecordScope
    ) -> None:
        """With ttl_seconds=0, soft-deleted messages are immediately expired."""
        store = SqliteMessageStore(connection, scope, ttl_seconds=0.0)
        for i in range(5):
            await store.append_message(msg(f"m{i}"))
        await store.prune_messages(3)

        # Give a tiny delay so time.time() advances past updated_at.
        time.sleep(0.01)
        removed = await store.cleanup_expired()

        assert removed == 2
        # After cleanup, the rows are physically gone.
        count = await connection.query_value(
            "SELECT COUNT(*) FROM memory_session_messages WHERE scope_key = ?",
            int,
            (scope.canonical(),),
        )
        assert count == 3  # only the 3 kept messages remain

    async def test_cleanup_expired_noop_without_soft_deleted(
        self, message_store: SqliteMessageStore
    ) -> None:
        await message_store.append_message(msg("m0"))

        removed = await message_store.cleanup_expired()

        assert removed == 0

    async def test_cleanup_expired_respects_ttl(
        self, connection: ConnectionManager, scope: RecordScope
    ) -> None:
        """With a long TTL, recently soft-deleted messages are not removed."""
        store = SqliteMessageStore(connection, scope, ttl_seconds=3600.0)
        for i in range(3):
            await store.append_message(msg(f"m{i}"))
        await store.prune_messages(1)

        removed = await store.cleanup_expired()

        assert removed == 0  # TTL not yet expired

    async def test_prune_bumps_updated_at_via_trigger(
        self, connection: ConnectionManager, scope: RecordScope
    ) -> None:
        """prune_messages SET state='soft_deleted' without touching updated_at;
        the ADR-0029 trigger must bump updated_at. Uses a 1.1s sleep to cross
        the strftime('%s','now') second boundary (trigger has second granularity)."""
        store = SqliteMessageStore(connection, scope, ttl_seconds=0.0)
        await store.append_message(msg("m0"))

        pre_prune_updated_at = await connection.query_value(
            "SELECT updated_at FROM memory_session_messages WHERE scope_key = ?",
            int,
            (scope.canonical(),),
        )
        time.sleep(1.1)
        await store.prune_messages(0)

        post_prune_updated_at = await connection.query_value(
            "SELECT updated_at FROM memory_session_messages WHERE scope_key = ?",
            int,
            (scope.canonical(),),
        )

        assert post_prune_updated_at > pre_prune_updated_at, (
            "trigger must bump updated_at when state is UPDATEd without explicit updated_at"
        )


class TestScopeIsolation:
    async def test_separate_scopes_are_isolated(
        self, connection: ConnectionManager, scope: RecordScope, other_scope: RecordScope
    ) -> None:
        store_a = SqliteMessageStore(connection, scope)
        store_b = SqliteMessageStore(connection, other_scope)

        await store_a.append_message(msg("a1"))
        await store_b.append_message(msg("b1"))

        assert [m["id"] for m in await store_a.load_messages()] == ["a1"]
        assert [m["id"] for m in await store_b.load_messages()] == ["b1"]


class TestSoftDeleteRetain:
    """``retain_messages`` soft-deletes — pruned rows survive in
    ``load_all_messages`` with ``_deleted: True`` marker."""

    async def test_retain_soft_deletes_excluded_from_load(
        self, message_store: SqliteMessageStore
    ) -> None:
        await message_store.save_messages([msg("m1"), msg("m2"), msg("m3")])
        rev = await message_store.get_revision()
        await message_store.retain_messages([msg("m2")], rev)

        active = await message_store.load_messages()
        assert [m["id"] for m in active] == ["m2"]

    async def test_retain_preserves_soft_deleted_in_load_all(
        self, message_store: SqliteMessageStore
    ) -> None:
        await message_store.save_messages([msg("m1"), msg("m2"), msg("m3")])
        rev = await message_store.get_revision()
        await message_store.retain_messages([msg("m2")], rev)

        all_msgs = await message_store.load_all_messages()
        assert len(all_msgs) == 3
        ids = {m["id"]: m for m in all_msgs}
        assert ids["m1"].get("_deleted") is True
        assert ids["m3"].get("_deleted") is True
        assert ids["m2"].get("_deleted") is None

    async def test_retain_bumps_revision(self, message_store: SqliteMessageStore) -> None:
        await message_store.save_messages([msg("m1"), msg("m2")])
        rev_before = await message_store.get_revision()
        await message_store.retain_messages([msg("m1")], rev_before)
        rev_after = await message_store.get_revision()
        assert rev_after.version > rev_before.version
        assert rev_after.message_count == 1

    async def test_retain_no_op_when_all_kept(self, message_store: SqliteMessageStore) -> None:
        await message_store.save_messages([msg("m1"), msg("m2")])
        rev = await message_store.get_revision()
        result = await message_store.retain_messages([msg("m1"), msg("m2")], rev)
        assert result is not None
        loaded = await message_store.load_all_messages()
        assert [m["id"] for m in loaded] == ["m1", "m2"]

    async def test_load_all_includes_prune_soft_deleted(
        self, message_store: SqliteMessageStore
    ) -> None:
        for i in range(5):
            await message_store.append_message(msg(f"m{i}"))
        await message_store.prune_messages(2)

        active = await message_store.load_messages()
        assert len(active) == 2

        all_msgs = await message_store.load_all_messages()
        assert len(all_msgs) == 5
        deleted = [m for m in all_msgs if m.get("_deleted") is True]
        assert len(deleted) == 3


class TestRetainSupersede:
    """``retain_messages`` re-inserts the keep list with fresh rows; the stale
    copies of kept messages become ``superseded`` — invisible to every read
    path and TTL-purged like soft-deleted rows."""

    async def test_kept_message_appears_once_in_load_all(
        self, message_store: SqliteMessageStore
    ) -> None:
        """Superseded copies never surface — no duplicates in the history view."""
        await message_store.save_messages([msg("m1"), msg("m2"), msg("m3")])
        rev = await message_store.get_revision()
        await message_store.retain_messages([msg("m2")], rev)

        all_msgs = await message_store.load_all_messages()
        ids = [m["id"] for m in all_msgs]
        assert sorted(ids) == ["m1", "m2", "m3"]
        by_id = {m["id"]: m for m in all_msgs}
        assert by_id["m1"].get("_deleted") is True
        assert by_id["m3"].get("_deleted") is True
        assert by_id["m2"].get("_deleted") is None

    async def test_retain_reinsert_preserves_created_at(
        self, message_store: SqliteMessageStore
    ) -> None:
        """Re-inserted kept messages keep their original creation time
        (callers pass loaded dicts, which carry ``created_at``)."""
        await message_store.append_message(msg("m1"))
        await message_store.append_message(msg("m2"))
        loaded_before = await message_store.load_messages()
        created_before = {m["id"]: m["created_at"] for m in loaded_before}
        rev = await message_store.get_revision()
        await message_store.retain_messages([loaded_before[1]], rev)

        loaded = await message_store.load_messages()
        assert [m["id"] for m in loaded] == ["m2"]
        assert loaded[0]["created_at"] == created_before["m2"]

    async def test_cleanup_expired_purges_superseded(
        self, connection: ConnectionManager, scope: RecordScope
    ) -> None:
        """With ttl_seconds=0, superseded rows are physically purged like
        soft-deleted ones."""
        store = SqliteMessageStore(connection, scope, ttl_seconds=0.0)
        await store.save_messages([msg("m1"), msg("m2"), msg("m3")])
        rev = await store.get_revision()
        await store.retain_messages([msg("m2")], rev)

        # Give a tiny delay so time.time() advances past updated_at.
        time.sleep(0.01)
        removed = await store.cleanup_expired()

        assert removed == 3  # m1/m3 soft-deleted + stale superseded copy of m2
        count = await connection.query_value(
            "SELECT COUNT(*) FROM memory_session_messages WHERE scope_key = ?",
            int,
            (scope.canonical(),),
        )
        assert count == 1  # only the re-inserted active m2 remains
