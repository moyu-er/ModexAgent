"""Unit tests for session-tree stores."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from enum import StrEnum
from pathlib import Path
from typing import assert_never

import pytest

from modex_agent.core.scope import RecordScope
from modex_agent.multi_agent.session_tree.models import (
    SessionTreeRecord,
    SessionTreeStatus,
)
from modex_agent.multi_agent.session_tree.store_tree import (
    InMemorySessionTreeStore,
    LocalFileSessionTreeStore,
    SessionTreeStore,
    SqliteSessionTreeStore,
)
from modex_agent.persistence import ConnectionManager, DatabaseKind

_NOW = 1_700_000_000_000


class _Backend(StrEnum):
    MEMORY = "memory"
    FILE = "file"
    SQLITE = "sqlite"


def _record(
    tree_id: str,
    status: SessionTreeStatus = SessionTreeStatus.ACTIVE,
) -> SessionTreeRecord:
    return SessionTreeRecord(
        tree_id=tree_id,
        root_node_session_id=f"root-{tree_id}",
        pool_name="main",
        workspace_root="/workspace",
        status=status,
        created_at=_NOW,
        updated_at=_NOW,
    )


@pytest.fixture(params=tuple(_Backend), ids=tuple(_Backend))
async def store(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> AsyncIterator[SessionTreeStore]:
    backend = _Backend(request.param)
    match backend:
        case _Backend.MEMORY:
            yield InMemorySessionTreeStore()
        case _Backend.FILE:
            yield LocalFileSessionTreeStore(tmp_path / "trees")
        case _Backend.SQLITE:
            connection = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
            await connection.open()
            try:
                yield SqliteSessionTreeStore(
                    connection,
                    RecordScope(workspace_id="workspace-a"),
                )
            finally:
                await connection.close()
        case unreachable:
            assert_never(unreachable)


def test_session_tree_store_declares_four_async_abstract_methods() -> None:
    assert SessionTreeStore.__abstractmethods__ == {
        "create",
        "get",
        "list_active",
        "update_status",
    }
    assert all(
        inspect.iscoroutinefunction(method)
        for method in (
            SessionTreeStore.create,
            SessionTreeStore.get,
            SessionTreeStore.update_status,
            SessionTreeStore.list_active,
        )
    )


@pytest.mark.parametrize(
    "implementation",
    [InMemorySessionTreeStore, LocalFileSessionTreeStore, SqliteSessionTreeStore],
)
def test_implementation_fulfils_abstract_contract(
    implementation: type[SessionTreeStore],
) -> None:
    assert implementation.__abstractmethods__ == frozenset()


async def test_create_then_get_returns_record(store: SessionTreeStore) -> None:
    record = _record("tree-1")

    await store.create(record)

    assert await store.get(record.tree_id) == record


async def test_get_nonexistent_returns_none(store: SessionTreeStore) -> None:
    assert await store.get("missing") is None


async def test_update_status_updates_status_and_timestamp(store: SessionTreeStore) -> None:
    record = _record("tree-1")
    await store.create(record)

    await store.update_status(record.tree_id, SessionTreeStatus.COMPLETED)

    updated = await store.get(record.tree_id)
    assert updated is not None
    assert updated.status is SessionTreeStatus.COMPLETED
    assert updated.updated_at > record.updated_at
    assert updated.completed_at == updated.updated_at


async def test_list_active_excludes_terminal_trees(store: SessionTreeStore) -> None:
    active = _record("tree-active")
    completed = _record("tree-completed", SessionTreeStatus.COMPLETED)
    cancelled = _record("tree-cancelled", SessionTreeStatus.CANCELLED)
    await store.create(active)
    await store.create(completed)
    await store.create(cancelled)

    records = await store.list_active()

    assert records == [active]


async def test_local_file_store_round_trips_across_instances(tmp_path: Path) -> None:
    root = tmp_path / "trees"
    record = _record("tree-1")
    await LocalFileSessionTreeStore(root).create(record)

    restored = await LocalFileSessionTreeStore(root).get(record.tree_id)

    assert restored == record


async def test_sqlite_store_writes_canonical_scope_keys(tmp_path: Path) -> None:
    connection = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await connection.open()
    scope = RecordScope(workspace_id="workspace-a")
    record = _record("tree-1")
    sqlite_store = SqliteSessionTreeStore(connection, scope)

    await sqlite_store.create(record)

    row = await connection.query_one(
        "SELECT scope_key, owner_scope_key FROM session_trees WHERE tree_id = ?",
        (record.tree_id,),
    )
    assert row is not None
    assert row["owner_scope_key"] == scope.canonical()
    assert row["scope_key"] == scope.model_copy(
        update={"session_id": record.root_node_session_id}
    ).canonical()
    await connection.close()
