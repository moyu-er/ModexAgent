"""Session-tree stores conformance — same assertions for ``file`` and ``sqlite`` backends.

Covers all three session-tree persistence ABCs:
- :class:`SessionTreeStore` — tree lifecycle records.
- :class:`TreeNodeStore` — per-session node records.
- :class:`MessageTrackStore` — routed-message delivery tracks.

File: ``LocalFile*Store`` implementations.
SQLite: ``Sqlite*Store`` implementations (over ``ConnectionManager``).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from modex_agent.core.scope import RecordScope
from modex_agent.multi_agent.session_tree.models import (
    MessageTrack,
    MessageTrackStatus,
    NodeVersionStatus,
    SessionTreeRecord,
    SessionTreeStatus,
    TreeNodeRecord,
)
from modex_agent.multi_agent.session_tree.store_node import (
    LocalFileTreeNodeStore,
    SqliteTreeNodeStore,
    TreeNodeStore,
)
from modex_agent.multi_agent.session_tree.store_track import (
    LocalFileMessageTrackStore,
    MessageTrackStore,
    SqliteMessageTrackStore,
)
from modex_agent.multi_agent.session_tree.store_tree import (
    LocalFileSessionTreeStore,
    SessionTreeStore,
    SqliteSessionTreeStore,
)
from modex_agent.persistence import ConnectionManager, DatabaseKind

_NOW = 1_700_000_000_000
_SCOPE = RecordScope(workspace_id="conformance")


def _tree_record(
    tree_id: str = "tree-1",
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


def _node_record(
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
        created_at=_NOW,
        updated_at=_NOW,
    )


def _track(
    message_id: str = "message-1",
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


@pytest.fixture(params=["file", "sqlite"])
async def tree_store(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> AsyncGenerator[SessionTreeStore]:
    """Parametrized SessionTreeStore — file or sqlite."""
    if request.param == "file":
        yield LocalFileSessionTreeStore(tmp_path / "trees")
        return
    mgr = ConnectionManager(tmp_path / "trees.db", DatabaseKind.WORKSPACE)
    await mgr.open()
    yield SqliteSessionTreeStore(mgr, _SCOPE)
    await mgr.close()


@pytest.fixture(params=["file", "sqlite"])
async def node_store(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> AsyncGenerator[TreeNodeStore]:
    """Parametrized TreeNodeStore — file or sqlite."""
    if request.param == "file":
        yield LocalFileTreeNodeStore(tmp_path / "nodes")
        return
    mgr = ConnectionManager(tmp_path / "nodes.db", DatabaseKind.WORKSPACE)
    await mgr.open()
    yield SqliteTreeNodeStore(mgr, _SCOPE)
    await mgr.close()


@pytest.fixture(params=["file", "sqlite"])
async def track_store(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> AsyncGenerator[MessageTrackStore]:
    """Parametrized MessageTrackStore — file or sqlite."""
    if request.param == "file":
        yield LocalFileMessageTrackStore(tmp_path / "tracks")
        return
    mgr = ConnectionManager(tmp_path / "tracks.db", DatabaseKind.WORKSPACE)
    await mgr.open()
    yield SqliteMessageTrackStore(mgr, _SCOPE)
    await mgr.close()


class TestSessionTreeStoreConformance:
    """Same behavior on both backends."""

    async def test_create_then_get_returns_record(self, tree_store: SessionTreeStore) -> None:
        record = _tree_record("tree-1")
        await tree_store.create(record)
        assert await tree_store.get(record.tree_id) == record

    async def test_get_nonexistent_returns_none(self, tree_store: SessionTreeStore) -> None:
        assert await tree_store.get("missing") is None

    async def test_update_status_sets_terminal_fields(
        self,
        tree_store: SessionTreeStore,
    ) -> None:
        record = _tree_record("tree-1")
        await tree_store.create(record)

        await tree_store.update_status(record.tree_id, SessionTreeStatus.COMPLETED)

        updated = await tree_store.get(record.tree_id)
        assert updated is not None
        assert updated.status is SessionTreeStatus.COMPLETED
        assert updated.updated_at > record.updated_at
        assert updated.completed_at == updated.updated_at

    async def test_update_status_to_active_clears_completed_at(
        self,
        tree_store: SessionTreeStore,
    ) -> None:
        record = _tree_record("tree-1", SessionTreeStatus.COMPLETED)
        record = record.model_copy(update={"completed_at": _NOW})
        await tree_store.create(record)

        await tree_store.update_status(record.tree_id, SessionTreeStatus.ACTIVE)

        updated = await tree_store.get(record.tree_id)
        assert updated is not None
        assert updated.status is SessionTreeStatus.ACTIVE
        assert updated.completed_at is None

    async def test_list_active_returns_only_active_trees(
        self,
        tree_store: SessionTreeStore,
    ) -> None:
        active = _tree_record("tree-active")
        completed = _tree_record("tree-completed", SessionTreeStatus.COMPLETED)
        cancelled = _tree_record("tree-cancelled", SessionTreeStatus.CANCELLED)
        await tree_store.create(active)
        await tree_store.create(completed)
        await tree_store.create(cancelled)

        records = await tree_store.list_active()

        assert records == [active]


class TestTreeNodeStoreConformance:
    """Same behavior on both backends."""

    async def test_create_then_get_returns_record(self, node_store: TreeNodeStore) -> None:
        record = _node_record()
        await node_store.create(record)
        assert await node_store.get(record.session_id) == record

    async def test_get_nonexistent_returns_none(self, node_store: TreeNodeStore) -> None:
        assert await node_store.get("missing") is None

    async def test_get_or_create_creates_new_record(self, node_store: TreeNodeStore) -> None:
        record = _node_record()

        result = await node_store.get_or_create(record)

        assert result == record
        assert await node_store.get(record.session_id) == record

    async def test_get_or_create_returns_existing_record(
        self,
        node_store: TreeNodeStore,
    ) -> None:
        existing = _node_record(agent_name="original")
        replacement = _node_record(agent_name="replacement")
        await node_store.create(existing)

        result = await node_store.get_or_create(replacement)

        assert result == existing
        assert await node_store.get(existing.session_id) == existing

    async def test_update_version_updates_in_place(self, node_store: TreeNodeStore) -> None:
        record = _node_record()
        await node_store.create(record)

        await node_store.update_version(
            record.session_id,
            version=2,
            parent_version=1,
            status=NodeVersionStatus.COMPLETED,
        )

        updated = await node_store.get(record.session_id)
        assert updated is not None
        assert updated.version == 2
        assert updated.parent_version == 1
        assert updated.status is NodeVersionStatus.COMPLETED
        assert updated.created_at == record.created_at
        assert updated.updated_at > record.updated_at

    async def test_get_tree_sessions_returns_only_requested_tree(
        self,
        node_store: TreeNodeStore,
    ) -> None:
        await node_store.create(_node_record("root-sid"))
        await node_store.create(
            _node_record("child-sid", agent_name="worker", parent_session_id="root-sid"),
        )
        await node_store.create(_node_record("other-sid", tree_id="tree-2"))

        sessions = await node_store.get_tree_sessions("tree-1")

        assert set(sessions) == {"root-sid", "child-sid"}

    async def test_get_tree_node_records_returns_full_records(
        self, node_store: TreeNodeStore
    ) -> None:
        await node_store.create(_node_record("root-sid"))
        await node_store.create(
            _node_record("child-sid", agent_name="worker", parent_session_id="root-sid"),
        )
        await node_store.create(_node_record("other-sid", tree_id="tree-2"))

        records = await node_store.get_tree_node_records("tree-1")

        assert len(records) == 2
        assert {r.session_id for r in records} == {"root-sid", "child-sid"}
        child = next(r for r in records if r.session_id == "child-sid")
        assert child.parent_session_id == "root-sid"
        assert child.agent_name == "worker"


class TestMessageTrackStoreConformance:
    """Same behavior on both backends."""

    async def test_create_then_get_returns_track(
        self,
        track_store: MessageTrackStore,
    ) -> None:
        track = _track("message-1")
        await track_store.create(track)
        assert await track_store.get(track.track_id) == track

    async def test_get_nonexistent_returns_none(self, track_store: MessageTrackStore) -> None:
        assert await track_store.get("missing") is None

    async def test_get_by_message_id_requires_matching_tree(
        self,
        track_store: MessageTrackStore,
    ) -> None:
        track = _track("message-1", tree_id="tree-a")
        await track_store.create(track)

        matching = await track_store.get_by_message_id("tree-a", "message-1")
        wrong_tree = await track_store.get_by_message_id("tree-b", "message-1")

        assert matching == track
        assert wrong_tree is None

    async def test_update_status_sets_consumed_at_when_provided(
        self,
        track_store: MessageTrackStore,
    ) -> None:
        track = _track("message-1")
        await track_store.create(track)

        await track_store.update_status(
            track.track_id,
            MessageTrackStatus.CONSUMED,
            consumed_at=_NOW + 1,
        )

        updated = await track_store.get(track.track_id)
        assert updated is not None
        assert updated.status is MessageTrackStatus.CONSUMED
        assert updated.consumed_at == _NOW + 1

    async def test_update_status_preserves_consumed_at_when_omitted(
        self,
        track_store: MessageTrackStore,
    ) -> None:
        track = _track("message-1", consumed_at=_NOW + 1)
        await track_store.create(track)

        await track_store.update_status(track.track_id, MessageTrackStatus.CANCELLED)

        updated = await track_store.get(track.track_id)
        assert updated is not None
        assert updated.status is MessageTrackStatus.CANCELLED
        assert updated.consumed_at == _NOW + 1

    async def test_has_dispatched_reflects_state(
        self,
        track_store: MessageTrackStore,
    ) -> None:
        assert await track_store.has_dispatched("tree-1") is False
        await track_store.create(_track("message-1"))
        assert await track_store.has_dispatched("tree-1") is True

    async def test_list_dispatched_returns_all_and_only_dispatched(
        self,
        track_store: MessageTrackStore,
    ) -> None:
        first = _track("message-a")
        second = _track("message-b")
        consumed = _track("message-c", status=MessageTrackStatus.CONSUMED)
        await track_store.create(second)
        await track_store.create(consumed)
        await track_store.create(first)

        dispatched = await track_store.list_dispatched("tree-1")

        assert dispatched == [first, second]

    async def test_close_tracks_for_session_updates_only_dispatched_targets(
        self,
        track_store: MessageTrackStore,
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
            await track_store.create(track)

        await track_store.close_tracks_for_session("target-1", MessageTrackStatus.CANCELLED)

        first_updated = await track_store.get(target_first.track_id)
        second_updated = await track_store.get(target_second.track_id)
        assert first_updated is not None
        assert second_updated is not None
        assert first_updated.status is MessageTrackStatus.CANCELLED
        assert second_updated.status is MessageTrackStatus.CANCELLED
        assert await track_store.get(already_consumed.track_id) == already_consumed
        assert await track_store.get(other_target.track_id) == other_target

    async def test_close_tracks_for_session_clears_has_dispatched(
        self,
        track_store: MessageTrackStore,
    ) -> None:
        await track_store.create(_track("message-1"))
        assert await track_store.has_dispatched("tree-1") is True

        await track_store.close_tracks_for_session("target-1", MessageTrackStatus.CANCELLED)

        assert await track_store.has_dispatched("tree-1") is False
