"""Tests for :class:`SqliteArchiveStore` — log + channel log + state + retention."""

from __future__ import annotations

from modex_agent.core.scope import RecordScope
from modex_agent.memory.archive_models import ArchiveChannel, ArchiveWrite
from modex_agent.memory.core.split_stores import MemoryStoreBundle
from modex_agent.memory.layers.archive import ScopedArchiveMemoryManager
from modex_agent.memory.layers.config import ArchiveMemoryConfig
from modex_agent.memory.scope import MemoryContext
from modex_agent.persistence import ConnectionManager
from modex_agent.persistence.adapters.archive_store import SqliteArchiveStore
from modex_agent.persistence.adapters.cursor_store import SqliteCursorStore
from modex_agent.persistence.adapters.kv_store import SqliteKVStore
from modex_agent.persistence.adapters.message_store import SqliteMessageStore


class TestGeneralLog:
    async def test_append_log_assigns_cursor(self, archive_store: SqliteArchiveStore) -> None:
        entry = await archive_store.append_log({"summary": "first"})

        assert entry["cursor"] == 1
        assert entry["archive_id"] == 1
        assert entry["entry_id"] == 1
        assert entry["summary"] == "first"

    async def test_append_log_increments_cursor(self, archive_store: SqliteArchiveStore) -> None:
        e1 = await archive_store.append_log({"summary": "a"})
        e2 = await archive_store.append_log({"summary": "b"})

        assert e1["cursor"] == 1
        assert e2["cursor"] == 2

    async def test_read_logs_returns_all(self, archive_store: SqliteArchiveStore) -> None:
        await archive_store.append_log({"summary": "a"})
        await archive_store.append_log({"summary": "b"})

        logs = await archive_store.read_logs()
        assert len(logs) == 2
        assert [e["summary"] for e in logs] == ["a", "b"]

    async def test_read_logs_since_cursor(self, archive_store: SqliteArchiveStore) -> None:
        await archive_store.append_log({"summary": "a"})
        await archive_store.append_log({"summary": "b"})
        await archive_store.append_log({"summary": "c"})

        logs = await archive_store.read_logs(since_cursor=1)
        assert [e["summary"] for e in logs] == ["b", "c"]

    async def test_read_logs_limit(self, archive_store: SqliteArchiveStore) -> None:
        for i in range(5):
            await archive_store.append_log({"summary": str(i)})

        logs = await archive_store.read_logs(limit=2)
        assert len(logs) == 2

    async def test_save_logs_replaces_all(self, archive_store: SqliteArchiveStore) -> None:
        await archive_store.append_log({"summary": "old"})
        await archive_store.save_logs(
            [
                {"archive_id": 10, "summary": "new1"},
                {"archive_id": 20, "summary": "new2"},
            ]
        )

        logs = await archive_store.read_logs()
        assert len(logs) == 2
        assert [e["archive_id"] for e in logs] == [10, 20]

    async def test_read_logs_empty(self, archive_store: SqliteArchiveStore) -> None:
        assert await archive_store.read_logs() == []


class TestArchiveState:
    async def test_read_state_returns_none_when_unset(
        self, archive_store: SqliteArchiveStore
    ) -> None:
        assert await archive_store.read_archive_state() is None

    async def test_write_and_read_state_roundtrip(self, archive_store: SqliteArchiveStore) -> None:
        await archive_store.write_archive_state({"version": 3, "foo": "bar"})

        state = await archive_store.read_archive_state()
        assert state is not None
        assert state["version"] == 3
        assert state["foo"] == "bar"

    async def test_write_state_with_next_archive_id(
        self, archive_store: SqliteArchiveStore
    ) -> None:
        await archive_store.write_archive_state({"next_archive_id": 10})

        state = await archive_store.read_archive_state()
        assert state is not None
        assert state["next_archive_id"] == 10

    async def test_write_state_overwrites(self, archive_store: SqliteArchiveStore) -> None:
        await archive_store.write_archive_state({"v": 1})
        await archive_store.write_archive_state({"v": 2})

        state = await archive_store.read_archive_state()
        assert state is not None
        assert state["v"] == 2


class TestChannelLog:
    async def test_append_channel_log_with_explicit_archive_id(
        self, archive_store: SqliteArchiveStore
    ) -> None:
        entry = await archive_store.append_channel_log(
            "context",
            {
                "archive_id": 5,
                "summary": "hello",
            },
        )

        assert entry["archive_id"] == 5
        assert entry["channel"] == "context"
        assert entry["cursor"] == 5
        assert entry["summary"] == "hello"

    async def test_append_channel_log_auto_archive_id(
        self, archive_store: SqliteArchiveStore
    ) -> None:
        entry = await archive_store.append_channel_log("context", {"summary": "auto"})

        assert entry["archive_id"] == 1
        assert entry["channel"] == "context"

    async def test_read_channel_logs(self, archive_store: SqliteArchiveStore) -> None:
        await archive_store.append_channel_log("context", {"archive_id": 1, "summary": "a"})
        await archive_store.append_channel_log("context", {"archive_id": 2, "summary": "b"})
        await archive_store.append_channel_log("core", {"archive_id": 1, "summary": "k"})

        logs = await archive_store.read_channel_logs("context")
        assert len(logs) == 2
        assert [e["summary"] for e in logs] == ["a", "b"]

    async def test_read_channel_logs_since_archive_id(
        self, archive_store: SqliteArchiveStore
    ) -> None:
        await archive_store.append_channel_log("context", {"archive_id": 1, "summary": "a"})
        await archive_store.append_channel_log("context", {"archive_id": 2, "summary": "b"})
        await archive_store.append_channel_log("context", {"archive_id": 3, "summary": "c"})

        logs = await archive_store.read_channel_logs("context", since_archive_id=1)
        assert [e["archive_id"] for e in logs] == [2, 3]

    async def test_read_channel_logs_limit(self, archive_store: SqliteArchiveStore) -> None:
        for i in range(1, 6):
            await archive_store.append_channel_log("context", {"archive_id": i, "summary": str(i)})

        logs = await archive_store.read_channel_logs("context", limit=2)
        assert len(logs) == 2

    async def test_read_channel_logs_empty(self, archive_store: SqliteArchiveStore) -> None:
        assert await archive_store.read_channel_logs("context") == []

    async def test_save_channel_logs_replaces(self, archive_store: SqliteArchiveStore) -> None:
        await archive_store.append_channel_log("context", {"archive_id": 1, "summary": "old"})
        await archive_store.save_channel_logs(
            "context",
            [
                {"archive_id": 10, "summary": "new1"},
                {"archive_id": 20, "summary": "new2"},
            ],
        )

        logs = await archive_store.read_channel_logs("context")
        assert len(logs) == 2
        assert [e["archive_id"] for e in logs] == [10, 20]

    async def test_channels_are_independent(self, archive_store: SqliteArchiveStore) -> None:
        await archive_store.append_channel_log("context", {"archive_id": 1, "summary": "c1"})
        await archive_store.append_channel_log("core", {"archive_id": 1, "summary": "k1"})

        context_logs = await archive_store.read_channel_logs("context")
        core_logs = await archive_store.read_channel_logs("core")
        assert len(context_logs) == 1
        assert len(core_logs) == 1
        assert context_logs[0]["summary"] == "c1"
        assert core_logs[0]["summary"] == "k1"

    async def test_same_archive_id_different_channels(
        self, archive_store: SqliteArchiveStore
    ) -> None:
        """UNIQUE (scope_key, archive_id, channel) allows same id across channels."""
        await archive_store.append_channel_log("context", {"archive_id": 1, "summary": "c"})
        await archive_store.append_channel_log("core", {"archive_id": 1, "summary": "k"})

        context = await archive_store.read_channel_logs("context")
        core = await archive_store.read_channel_logs("core")
        assert context[0]["archive_id"] == 1
        assert core[0]["archive_id"] == 1


class TestRetention:
    async def test_prune_to_max_deletes_oldest(self, archive_store: SqliteArchiveStore) -> None:
        for i in range(1, 6):
            await archive_store.append_channel_log("context", {"archive_id": i, "summary": str(i)})

        deleted = await archive_store.prune_to_max(3)

        assert deleted == 2  # archive_ids 1 and 2 (2 rows)
        remaining = await archive_store.read_channel_logs("context")
        assert [e["archive_id"] for e in remaining] == [3, 4, 5]

    async def test_prune_to_max_noop_when_under_limit(
        self, archive_store: SqliteArchiveStore
    ) -> None:
        await archive_store.append_channel_log("context", {"archive_id": 1, "summary": "a"})

        deleted = await archive_store.prune_to_max(10)

        assert deleted == 0

    async def test_prune_to_max_zero_deletes_all(self, archive_store: SqliteArchiveStore) -> None:
        await archive_store.append_channel_log("context", {"archive_id": 1, "summary": "a"})
        await archive_store.append_channel_log("context", {"archive_id": 2, "summary": "b"})

        deleted = await archive_store.prune_to_max(0)

        assert deleted == 2
        assert await archive_store.read_channel_logs("context") == []

    async def test_cleanup_empty_dirs_is_noop(self, archive_store: SqliteArchiveStore) -> None:
        result = await archive_store.cleanup_empty_dirs()
        assert result == 0


class TestCreatedAtRoundTrip:
    """Regression for bug I2: created_at must survive SQLite read-back."""

    async def test_read_back_created_at_stays_int_ms(
        self, archive_store: SqliteArchiveStore
    ) -> None:
        created_ms = 1735689600000  # 2025-01-01T00:00:00Z in epoch ms
        await archive_store.append_channel_log(
            "context",
            {"archive_id": 1, "summary": "a", "created_at": created_ms},
        )

        logs = await archive_store.read_channel_logs("context")

        assert isinstance(logs[0]["created_at"], int)
        assert logs[0]["created_at"] == created_ms

    async def test_manager_read_paths_parse_created_at(
        self, connection: ConnectionManager, scope: RecordScope
    ) -> None:
        bundle = MemoryStoreBundle(
            messages=SqliteMessageStore(connection, scope, ttl_seconds=0.0),
            kv=SqliteKVStore(connection, scope),
            cursors=SqliteCursorStore(connection, scope),
            archive=SqliteArchiveStore(connection, scope),
        )

        async def factory(_context: MemoryContext) -> MemoryStoreBundle:
            return bundle

        manager = ScopedArchiveMemoryManager(factory, ArchiveMemoryConfig())
        ctx = MemoryContext(session_id="s1", user_id="u1")
        await manager.append_bundle(
            ctx,
            (
                ArchiveWrite(channel=ArchiveChannel.CONTEXT, summary="timestamp probe entry"),
                ArchiveWrite(channel=ArchiveChannel.CORE, summary="knowledge entry"),
            ),
        )

        recent = await manager.get_recent(ctx, limit=5, channel=ArchiveChannel.CONTEXT)
        found = await manager.search(ctx, "timestamp probe", limit=5)
        unprocessed = await manager.get_unprocessed(ctx, "knowledge")

        assert len(recent) == 1
        assert len(found) == 1
        assert len(unprocessed.entries) == 1
        for entry in (*recent, *found, *unprocessed.entries):
            assert entry.created_at is not None
            assert entry.created_at.year > 2020


class TestArchiveScopeIsolation:
    async def test_separate_scopes_are_isolated(
        self, connection: ConnectionManager, scope: RecordScope, other_scope: RecordScope
    ) -> None:
        store_a = SqliteArchiveStore(connection, scope)
        store_b = SqliteArchiveStore(connection, other_scope)

        await store_a.append_log({"summary": "a"})
        await store_b.append_log({"summary": "b"})

        logs_a = await store_a.read_logs()
        logs_b = await store_b.read_logs()
        assert len(logs_a) == 1
        assert len(logs_b) == 1
        assert logs_a[0]["summary"] == "a"
        assert logs_b[0]["summary"] == "b"
