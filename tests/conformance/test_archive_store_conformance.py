"""ArchiveStore conformance — same assertions for ``file`` and ``sqlite`` backends.

File: :class:`DefaultScopedStorage`.
SQLite: :class:`SqliteArchiveStore`.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from modex_agent.core.scope import MemoryLayerName, RecordScope
from modex_agent.memory.core.split_stores import ArchiveStore
from modex_agent.memory.stores.scoped_file import DefaultScopedStorage
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters.archive_store import SqliteArchiveStore


@pytest.fixture(params=["file", "sqlite"])
async def archive_store(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    scope: RecordScope,
) -> AsyncGenerator[ArchiveStore]:
    """Parametrized ArchiveStore — file (DefaultScopedStorage) or sqlite."""
    if request.param == "file":
        yield DefaultScopedStorage(
            tmp_path / "archive_file",
            layer=MemoryLayerName.ARCHIVE,
        )
    else:
        mgr = ConnectionManager(tmp_path / "workspace.db", DatabaseKind.WORKSPACE)
        await mgr.open()
        yield SqliteArchiveStore(mgr, scope)
        await mgr.close()


class TestArchiveStoreConformance:
    """Same behavior on both backends."""

    async def test_read_archive_state_empty_returns_none(self, archive_store: ArchiveStore) -> None:
        assert await archive_store.read_archive_state() is None

    async def test_write_then_read_archive_state(self, archive_store: ArchiveStore) -> None:
        state = {"last_run": "2026-01-01", "count": 42}
        await archive_store.write_archive_state(state)
        result = await archive_store.read_archive_state()
        assert result is not None
        # both backends preserve user keys (SQLite also adds next_archive_id)
        assert result["last_run"] == "2026-01-01"
        assert result["count"] == 42

    async def test_append_log_assigns_cursor(self, archive_store: ArchiveStore) -> None:
        entry = await archive_store.append_log({"summary": "first"})
        assert entry["cursor"] >= 1
        assert entry["summary"] == "first"

    async def test_read_logs_empty(self, archive_store: ArchiveStore) -> None:
        assert await archive_store.read_logs() == []

    async def test_append_then_read_logs(self, archive_store: ArchiveStore) -> None:
        await archive_store.append_log({"summary": "a"})
        await archive_store.append_log({"summary": "b"})
        logs = await archive_store.read_logs()
        assert [e["summary"] for e in logs] == ["a", "b"]

    async def test_read_logs_since_cursor(self, archive_store: ArchiveStore) -> None:
        e1 = await archive_store.append_log({"summary": "a"})
        await archive_store.append_log({"summary": "b"})
        logs = await archive_store.read_logs(since_cursor=e1["cursor"])
        assert [e["summary"] for e in logs] == ["b"]

    async def test_save_logs_replaces(self, archive_store: ArchiveStore) -> None:
        await archive_store.append_log({"summary": "old"})
        # save_logs requires entries with archive_id/cursor so both backends
        # can find them via read_logs(since_cursor=0) which filters > 0.
        await archive_store.save_logs(
            [
                {"summary": "new1", "archive_id": 1, "cursor": 1},
                {"summary": "new2", "archive_id": 2, "cursor": 2},
            ]
        )
        logs = await archive_store.read_logs()
        assert [e["summary"] for e in logs] == ["new1", "new2"]

    async def test_append_channel_log(self, archive_store: ArchiveStore) -> None:
        entry = await archive_store.append_channel_log(
            "default", {"summary": "ch1", "archive_id": 1}
        )
        assert entry["summary"] == "ch1"
        assert entry["cursor"] >= 1

    async def test_read_channel_logs(self, archive_store: ArchiveStore) -> None:
        await archive_store.append_channel_log("default", {"summary": "a", "archive_id": 1})
        await archive_store.append_channel_log("default", {"summary": "b", "archive_id": 2})
        logs = await archive_store.read_channel_logs("default")
        assert [e["summary"] for e in logs] == ["a", "b"]

    async def test_save_channel_logs_replaces(self, archive_store: ArchiveStore) -> None:
        await archive_store.append_channel_log("default", {"summary": "old", "archive_id": 1})
        await archive_store.save_channel_logs("default", [{"summary": "new", "archive_id": 1}])
        logs = await archive_store.read_channel_logs("default")
        assert [e["summary"] for e in logs] == ["new"]

    async def test_channels_are_isolated(self, archive_store: ArchiveStore) -> None:
        # The file backend only separates predefined ArchiveChannel values
        # (context, core); custom names share one file. Use the
        # predefined names so both backends exhibit isolation.
        await archive_store.append_channel_log("context", {"summary": "ctx1", "archive_id": 1})
        await archive_store.append_channel_log("core", {"summary": "cor1", "archive_id": 1})
        ctx_logs = await archive_store.read_channel_logs("context")
        core_logs = await archive_store.read_channel_logs("core")
        assert [e["summary"] for e in ctx_logs] == ["ctx1"]
        assert [e["summary"] for e in core_logs] == ["cor1"]
