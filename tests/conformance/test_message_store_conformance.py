"""MessageStore conformance for in-memory, file, and SQLite backends.

In-memory: :class:`InMemoryScopedStorage`.
File: :class:`DefaultScopedStorage` (one instance implementing all four split
store ABCs).
SQLite: :class:`SqliteMessageStore`.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest

from modex_agent.core.message import MessageRole
from modex_agent.core.scope import RecordScope
from modex_agent.memory.core.split_stores import MessageStore
from modex_agent.memory.scope import MemoryLayerName
from modex_agent.memory.stores.scoped_file import DefaultScopedStorage
from modex_agent.memory.stores.scoped_in_memory import InMemoryScopedStorage
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters.message_store import SqliteMessageStore


def msg(mid: str, content: str = "x", **extra: object) -> dict[str, Any]:
    """Build a minimal message dict with an id."""
    result: dict[str, Any] = {"id": mid, "role": "user", "content": content}
    result.update(extra)
    return result


@pytest.fixture(params=["memory", "file", "sqlite"])
async def message_store(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    scope: RecordScope,
) -> AsyncGenerator[MessageStore]:
    """Parametrized MessageStore for all supported backends."""
    if request.param == "memory":
        yield InMemoryScopedStorage()
        return
    if request.param == "file":
        yield DefaultScopedStorage(
            tmp_path / "msg_file",
            layer=MemoryLayerName.SESSION,
        )
        return
    mgr = ConnectionManager(tmp_path / "workspace.db", DatabaseKind.WORKSPACE)
    await mgr.open()
    # ttl=0 so cleanup_expired picks up soft-deleted immediately
    yield SqliteMessageStore(mgr, scope, ttl_seconds=0.0)
    await mgr.close()


class TestMessageStoreConformance:
    """Same behavior on every backend."""

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

    async def test_append_accepts_every_message_role(self, message_store: MessageStore) -> None:
        """Every ``MessageRole`` member round-trips on both backends.

        Pins the SQLite ``memory_session_messages.role`` CHECK constraint
        against the canonical enum: the intake writer persists
        ``system_reminder`` records, and a role missing from the CHECK fails
        with ``sqlite3.IntegrityError`` at append time.
        """
        for index, role in enumerate(MessageRole):
            await message_store.append_message(msg(f"r{index}", role=str(role)))
        loaded = await message_store.load_messages()
        assert [m["role"] for m in loaded] == [str(role) for role in MessageRole]

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
    """``retain_messages`` — the active set becomes exactly the keep list, in order.

    Removed rows are not physically deleted:
    - pruned messages (absent from keep): FILE hard-deletes, SQLite soft-deletes
      (visible via ``load_all_messages``).
    - stale copies of kept messages: SQLite marks them ``superseded``
      (invisible to every read path); FILE rewrites the file wholesale.
    New keep entries (e.g. a compact summary) are inserted at their position
    in the keep list on both backends.
    """

    async def test_retain_keeps_only_specified(self, message_store: MessageStore) -> None:
        await message_store.save_messages([msg("m1"), msg("m2"), msg("m3")])
        rev = await message_store.get_revision()
        result = await message_store.retain_messages([msg("m2")], rev)
        assert result is not None
        loaded = await message_store.load_messages()
        assert len(loaded) == 1
        assert loaded[0]["id"] == "m2"

    async def test_retain_inserts_new_head_message(self, message_store: MessageStore) -> None:
        """A keep entry that did not exist (e.g. a compact summary) is inserted
        at its position in the keep list — the session-cleanup pattern."""
        await message_store.save_messages([msg("m1"), msg("m2"), msg("m3")])
        rev = await message_store.get_revision()
        new_head = {"id": "compact-1", "role": "compact", "content": "summary"}
        result = await message_store.retain_messages([new_head, msg("m3")], rev)
        assert result is not None
        loaded = await message_store.load_messages()
        assert [m["id"] for m in loaded] == ["compact-1", "m3"]
        assert loaded[0]["role"] == "compact"

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
    """``load_all_messages`` history filtering and limiting contract."""

    async def test_load_all_equals_load_when_no_pruning(self, message_store: MessageStore) -> None:
        await message_store.save_messages([msg("m1"), msg("m2")])
        active = await message_store.load_messages()
        all_msgs = await message_store.load_all_messages()
        assert len(all_msgs) == len(active)

    async def test_load_all_excludes_compact(self, message_store: MessageStore) -> None:
        await message_store.save_messages(
            [msg("normal"), msg("compact", role=str(MessageRole.COMPACT))]
        )

        loaded = await message_store.load_all_messages()

        assert [message["id"] for message in loaded] == ["normal"]

    async def test_load_all_limit_returns_last_non_compact_messages(
        self,
        message_store: MessageStore,
    ) -> None:
        messages = [
            msg(
                f"m{index}",
                role=(
                    str(MessageRole.COMPACT)
                    if index == 8
                    else str(MessageRole.USER)
                ),
            )
            for index in range(10)
        ]
        await message_store.save_messages(messages)

        loaded = await message_store.load_all_messages(limit=3)

        assert [message["id"] for message in loaded] == ["m6", "m7", "m9"]

    async def test_load_messages_includes_compact(self, message_store: MessageStore) -> None:
        await message_store.save_messages(
            [msg("normal"), msg("compact", role=str(MessageRole.COMPACT))]
        )

        loaded = await message_store.load_messages()

        assert [message["id"] for message in loaded] == ["normal", "compact"]

    async def test_load_all_limit_exceeds_total_returns_all(self, message_store: MessageStore) -> None:
        await message_store.save_messages([msg("m1"), msg("m2"), msg("m3")])

        loaded = await message_store.load_all_messages(limit=100)

        assert [message["id"] for message in loaded] == ["m1", "m2", "m3"]

    async def test_load_all_all_compact_returns_empty(self, message_store: MessageStore) -> None:
        await message_store.save_messages(
            [msg("c1", role=str(MessageRole.COMPACT)), msg("c2", role=str(MessageRole.COMPACT))]
        )

        loaded = await message_store.load_all_messages()

        assert loaded == []
