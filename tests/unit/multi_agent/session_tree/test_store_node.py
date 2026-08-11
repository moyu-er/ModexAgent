"""Tests for TreeNodeStore implementations."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from enum import StrEnum
from pathlib import Path
from typing import assert_never

import pytest

from modex_agent.core.scope import RecordScope
from modex_agent.multi_agent.session_tree.models import (
    NodeVersionStatus,
    TreeNodeRecord,
)
from modex_agent.multi_agent.session_tree.store_node import (
    InMemoryTreeNodeStore,
    LocalFileTreeNodeStore,
    SqliteTreeNodeStore,
    TreeNodeStore,
)
from modex_agent.persistence import ConnectionManager, DatabaseKind

NOW = 1_700_000_000_000


class StoreKind(StrEnum):
    MEMORY = "memory"
    FILE = "file"
    SQLITE = "sqlite"


def _node(
    session_id: str = "root-sid",
    *,
    tree_id: str = "tree-1",
    agent_name: str = "main",
    parent_session_id: str | None = None,
) -> TreeNodeRecord:
    return TreeNodeRecord(
        tree_id=tree_id,
        session_id=session_id,
        parent_session_id=parent_session_id,
        agent_name=agent_name,
        version=1,
        parent_version=None,
        status=NodeVersionStatus.RUNNING,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture(params=tuple(StoreKind))
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[TreeNodeStore]:
    kind = StoreKind(request.param)
    match kind:
        case StoreKind.MEMORY:
            yield InMemoryTreeNodeStore()
        case StoreKind.FILE:
            yield LocalFileTreeNodeStore(tmp_path / "tree-nodes")
        case StoreKind.SQLITE:
            connection = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
            await connection.open()
            yield SqliteTreeNodeStore(
                connection,
                RecordScope(workspace_id="workspace-1"),
            )
            await connection.close()
        case unreachable:
            assert_never(unreachable)


def test_tree_node_store_has_five_async_abstract_methods() -> None:
    expected = {
        "create",
        "get",
        "get_or_create",
        "get_tree_sessions",
        "update_version",
    }

    assert TreeNodeStore.__abstractmethods__ == expected
    assert all(inspect.iscoroutinefunction(getattr(TreeNodeStore, name)) for name in expected)


async def test_create_and_get_existing(store: TreeNodeStore) -> None:
    record = _node()

    await store.create(record)

    assert await store.get(record.session_id) == record


async def test_get_nonexistent_returns_none(store: TreeNodeStore) -> None:
    assert await store.get("missing") is None


async def test_get_or_create_creates_new_record(store: TreeNodeStore) -> None:
    record = _node()

    result = await store.get_or_create(record)

    assert result == record
    assert await store.get(record.session_id) == record


async def test_get_or_create_returns_existing_record(store: TreeNodeStore) -> None:
    existing = _node(agent_name="original")
    replacement = _node(agent_name="replacement")
    await store.create(existing)

    result = await store.get_or_create(replacement)

    assert result == existing
    assert await store.get(existing.session_id) == existing


async def test_get_or_create_is_atomic(store: TreeNodeStore) -> None:
    first = _node(agent_name="first")
    second = _node(agent_name="second")

    results = await asyncio.gather(
        store.get_or_create(first),
        store.get_or_create(second),
    )

    assert results[0] == results[1]
    assert await store.get(first.session_id) == results[0]
    assert await store.get_tree_sessions(first.tree_id) == [first.session_id]


async def test_update_version_in_place(store: TreeNodeStore) -> None:
    record = _node()
    await store.create(record)

    await store.update_version(
        record.session_id,
        version=2,
        parent_version=1,
        status=NodeVersionStatus.COMPLETED,
    )

    updated = await store.get(record.session_id)
    assert updated is not None
    assert updated.version == 2
    assert updated.parent_version == 1
    assert updated.status is NodeVersionStatus.COMPLETED
    assert updated.created_at == record.created_at
    assert updated.updated_at > record.updated_at
    assert await store.get_tree_sessions(record.tree_id) == [record.session_id]


async def test_get_tree_sessions_returns_only_requested_tree(store: TreeNodeStore) -> None:
    await store.create(_node("root-sid"))
    await store.create(
        _node(
            "child-sid",
            agent_name="worker",
            parent_session_id="root-sid",
        )
    )
    await store.create(_node("other-sid", tree_id="tree-2"))

    sessions = await store.get_tree_sessions("tree-1")

    assert set(sessions) == {"root-sid", "child-sid"}
