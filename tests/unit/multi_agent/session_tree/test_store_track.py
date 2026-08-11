"""Unit tests for message-track stores."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from enum import StrEnum
from pathlib import Path
from typing import assert_never

import pytest

from modex_agent.core.scope import RecordScope
from modex_agent.multi_agent.session_tree.models import MessageTrack, MessageTrackStatus
from modex_agent.multi_agent.session_tree.store_track import (
    InMemoryMessageTrackStore,
    LocalFileMessageTrackStore,
    MessageTrackStore,
    SqliteMessageTrackStore,
)
from modex_agent.persistence import ConnectionManager, DatabaseKind

_NOW = 1_700_000_000_000


class _Backend(StrEnum):
    MEMORY = "memory"
    FILE = "file"
    SQLITE = "sqlite"


def _track(
    message_id: str,
    *,
    tree_id: str = "tree-1",
    target_session_id: str = "target-1",
    status: MessageTrackStatus = MessageTrackStatus.DISPATCHED,
    consumed_at: int | None = None,
) -> MessageTrack:
    return MessageTrack(
        track_id=message_id,
        tree_id=tree_id,
        message_id=message_id,
        message_type="task_request",
        invocation_id="invocation-1",
        target_session_id=target_session_id,
        source_session_id="source-1",
        status=status,
        dispatched_at=_NOW,
        consumed_at=consumed_at,
    )


@pytest.fixture(params=tuple(_Backend), ids=tuple(_Backend))
async def store(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> AsyncIterator[MessageTrackStore]:
    backend = _Backend(request.param)
    match backend:
        case _Backend.MEMORY:
            yield InMemoryMessageTrackStore()
        case _Backend.FILE:
            yield LocalFileMessageTrackStore(tmp_path / "tracks")
        case _Backend.SQLITE:
            connection = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
            await connection.open()
            try:
                yield SqliteMessageTrackStore(
                    connection,
                    RecordScope(workspace_id="workspace-a"),
                )
            finally:
                await connection.close()
        case unreachable:
            assert_never(unreachable)


def test_message_track_store_declares_seven_async_abstract_methods() -> None:
    assert MessageTrackStore.__abstractmethods__ == {
        "close_tracks_for_session",
        "create",
        "get",
        "get_by_message_id",
        "has_dispatched",
        "list_dispatched",
        "update_status",
    }
    assert all(
        inspect.iscoroutinefunction(method)
        for method in (
            MessageTrackStore.create,
            MessageTrackStore.get,
            MessageTrackStore.get_by_message_id,
            MessageTrackStore.update_status,
            MessageTrackStore.has_dispatched,
            MessageTrackStore.list_dispatched,
            MessageTrackStore.close_tracks_for_session,
        )
    )


@pytest.mark.parametrize(
    "implementation",
    [InMemoryMessageTrackStore, LocalFileMessageTrackStore, SqliteMessageTrackStore],
)
def test_implementation_fulfils_abstract_contract(
    implementation: type[MessageTrackStore],
) -> None:
    assert implementation.__abstractmethods__ == frozenset()


async def test_create_then_get_returns_track(store: MessageTrackStore) -> None:
    track = _track("message-1")

    await store.create(track)

    assert await store.get(track.track_id) == track


async def test_get_nonexistent_returns_none(store: MessageTrackStore) -> None:
    assert await store.get("missing") is None


async def test_get_by_message_id_requires_matching_tree(
    store: MessageTrackStore,
) -> None:
    track = _track("message-1", tree_id="tree-a")
    await store.create(track)

    matching = await store.get_by_message_id("tree-a", "message-1")
    wrong_tree = await store.get_by_message_id("tree-b", "message-1")

    assert matching == track
    assert wrong_tree is None


async def test_update_status_sets_consumed_at_when_provided(
    store: MessageTrackStore,
) -> None:
    track = _track("message-1")
    await store.create(track)

    await store.update_status(
        track.track_id,
        MessageTrackStatus.CONSUMED,
        consumed_at=_NOW + 1,
    )

    updated = await store.get(track.track_id)
    assert updated is not None
    assert updated.status is MessageTrackStatus.CONSUMED
    assert updated.consumed_at == _NOW + 1


async def test_update_status_preserves_consumed_at_when_omitted(
    store: MessageTrackStore,
) -> None:
    track = _track("message-1", consumed_at=_NOW + 1)
    await store.create(track)

    await store.update_status(track.track_id, MessageTrackStatus.CANCELLED)

    updated = await store.get(track.track_id)
    assert updated is not None
    assert updated.status is MessageTrackStatus.CANCELLED
    assert updated.consumed_at == _NOW + 1


async def test_has_dispatched_changes_after_session_close(
    store: MessageTrackStore,
) -> None:
    assert await store.has_dispatched("tree-1") is False
    await store.create(_track("message-1"))
    assert await store.has_dispatched("tree-1") is True

    await store.close_tracks_for_session("target-1", MessageTrackStatus.CANCELLED)

    assert await store.has_dispatched("tree-1") is False


async def test_list_dispatched_returns_all_and_only_dispatched(
    store: MessageTrackStore,
) -> None:
    first = _track("message-a")
    second = _track("message-b")
    consumed = _track("message-c", status=MessageTrackStatus.CONSUMED)
    await store.create(second)
    await store.create(consumed)
    await store.create(first)

    dispatched = await store.list_dispatched("tree-1")

    assert dispatched == [first, second]


async def test_close_tracks_for_session_updates_only_dispatched_targets(
    store: MessageTrackStore,
) -> None:
    target_first = _track("message-a")
    target_second = _track("message-b")
    already_consumed = _track(
        "message-c",
        status=MessageTrackStatus.CONSUMED,
        consumed_at=_NOW + 1,
    )
    other_target = _track("message-d", target_session_id="target-2")
    for track in (target_first, target_second, already_consumed, other_target):
        await store.create(track)

    await store.close_tracks_for_session("target-1", MessageTrackStatus.CANCELLED)

    first_updated = await store.get(target_first.track_id)
    second_updated = await store.get(target_second.track_id)
    assert first_updated is not None
    assert second_updated is not None
    assert first_updated.status is MessageTrackStatus.CANCELLED
    assert second_updated.status is MessageTrackStatus.CANCELLED
    assert await store.get(already_consumed.track_id) == already_consumed
    assert await store.get(other_target.track_id) == other_target


async def test_local_file_store_round_trips_across_instances(tmp_path: Path) -> None:
    root = tmp_path / "tracks"
    track = _track("message-1")
    await LocalFileMessageTrackStore(root).create(track)

    restored = await LocalFileMessageTrackStore(root).get(track.track_id)

    assert restored == track


async def test_sqlite_store_writes_canonical_scope_keys(tmp_path: Path) -> None:
    connection = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await connection.open()
    scope = RecordScope(workspace_id="workspace-a")
    track = _track("message-1")
    sqlite_store = SqliteMessageTrackStore(connection, scope)

    await sqlite_store.create(track)

    row = await connection.query_one(
        "SELECT scope_key, owner_scope_key FROM message_tracks WHERE track_id = ?",
        (track.track_id,),
    )
    assert row is not None
    assert row["owner_scope_key"] == scope.canonical()
    assert row["scope_key"] == scope.model_copy(
        update={"session_id": track.target_session_id}
    ).canonical()
    await connection.close()
