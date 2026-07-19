"""Tests for DB-backed ``MemoryStoreBundle`` field independence and
``WorkspacePersistenceManager`` lifecycle.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.core.scope import RecordScope
from modex_agent.memory.core.split_stores import MemoryStoreBundle
from modex_agent.persistence.adapters.archive_store import SqliteArchiveStore
from modex_agent.persistence.adapters.cursor_store import SqliteCursorStore
from modex_agent.persistence.adapters.kv_store import SqliteKVStore
from modex_agent.persistence.adapters.message_store import SqliteMessageStore
from modex_agent.persistence.managers.workspace import WorkspacePersistenceManager


class _PoolScopedRecordScope(RecordScope):
    """Framework-test-local ``RecordScope`` subclass adding the pool dimension.

    Framework tests need to construct pool-scoped scope_keys (matching the
    bot's ``BotRecordScope`` canonical JSON). ``BotRecordScope`` lives in the
    examples layer and cannot be imported by framework tests (ADR-0028
    layering); this local subclass mirrors its ``pool`` field so the tests
    can construct compatible scope_keys without crossing the boundary.
    """

    pool: str | None = None


@pytest.fixture
def scope() -> RecordScope:
    return _PoolScopedRecordScope(pool="default", session_id="s1", agent_id="main")


class TestBundleFieldAreIndependent:
    async def test_bundle_has_four_distinct_adapter_instances(
        self, tmp_path: Path, scope: RecordScope
    ) -> None:
        mgr = WorkspacePersistenceManager(tmp_path / "state.db")
        await mgr.open()
        try:
            bundle = mgr.create_bundle(scope)

            assert isinstance(bundle, MemoryStoreBundle)
            assert isinstance(bundle.messages, SqliteMessageStore)
            assert isinstance(bundle.kv, SqliteKVStore)
            assert isinstance(bundle.cursors, SqliteCursorStore)
            assert isinstance(bundle.archive, SqliteArchiveStore)

            # Four independent instances (unlike file backend where all alias one).
            assert bundle.messages is not bundle.kv
            assert bundle.kv is not bundle.cursors
            assert bundle.cursors is not bundle.archive
            assert bundle.messages is not bundle.archive
        finally:
            await mgr.close()

    async def test_bundle_without_archive(self, tmp_path: Path, scope: RecordScope) -> None:
        mgr = WorkspacePersistenceManager(tmp_path / "state.db")
        await mgr.open()
        try:
            bundle = mgr.create_bundle(scope, with_archive=False)

            assert bundle.archive is None
            assert isinstance(bundle.messages, SqliteMessageStore)
        finally:
            await mgr.close()

    async def test_writing_messages_does_not_affect_kv(
        self, tmp_path: Path, scope: RecordScope
    ) -> None:
        """Bundle field independence: messages writes don't leak into kv."""
        mgr = WorkspacePersistenceManager(tmp_path / "state.db")
        await mgr.open()
        try:
            bundle = mgr.create_bundle(scope)

            await bundle.messages.append_message({"id": "m0", "role": "user", "content": "hi"})
            # KV should be unaffected.
            assert await bundle.kv.get("id") is None
            assert await bundle.kv.list_keys() == []

            # And vice versa.
            await bundle.kv.set("key1", "value1")
            messages = await bundle.messages.load_messages()
            assert len(messages) == 1
            assert messages[0]["id"] == "m0"
        finally:
            await mgr.close()

    async def test_cursors_independent_from_archive(
        self, tmp_path: Path, scope: RecordScope
    ) -> None:
        mgr = WorkspacePersistenceManager(tmp_path / "state.db")
        await mgr.open()
        try:
            bundle = mgr.create_bundle(scope)
            assert bundle.archive is not None

            await bundle.cursors.set_last_cursor("default", 42)
            await bundle.archive.append_log({"summary": "entry"})

            assert await bundle.cursors.get_last_cursor("default") == 42
            logs = await bundle.archive.read_logs()
            assert len(logs) == 1
        finally:
            await mgr.close()


class TestManagerLifecycle:
    async def test_open_close_roundtrip(self, tmp_path: Path) -> None:
        mgr = WorkspacePersistenceManager(tmp_path / "state.db")

        await mgr.open()
        # The connection should be usable.
        value = await mgr.connection.query_value("SELECT 1", int)
        assert value == 1
        await mgr.close()

    async def test_reopen_preserves_data(self, tmp_path: Path, scope: RecordScope) -> None:
        mgr = WorkspacePersistenceManager(tmp_path / "state.db")
        await mgr.open()
        try:
            bundle = mgr.create_bundle(scope)
            await bundle.kv.set("persisted", "yes")
        finally:
            await mgr.close()

        # Reopen and verify.
        mgr2 = WorkspacePersistenceManager(tmp_path / "state.db")
        await mgr2.open()
        try:
            bundle2 = mgr2.create_bundle(scope)
            assert await bundle2.kv.get("persisted") == "yes"
        finally:
            await mgr2.close()

    async def test_connection_property_returns_manager(self, tmp_path: Path) -> None:
        mgr = WorkspacePersistenceManager(tmp_path / "state.db")
        assert mgr.connection is mgr.connection  # stable identity
