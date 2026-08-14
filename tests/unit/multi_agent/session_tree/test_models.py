# tests/unit/multi_agent/session_tree/test_models.py
"""Tests for the session-tree data models (records + enums)."""

import pytest
from pydantic import ValidationError

from modex_agent.multi_agent.session_tree.models import (
    MessageTrack,
    MessageTrackStatus,
    NodeVersionStatus,
    SessionTreeRecord,
    SessionTreeStatus,
    TreeNodeRecord,
)

NOW = 1_700_000_000_000


# ---------------------------------------------------------------------------
# Enum string values
# ---------------------------------------------------------------------------


def test_session_tree_status_values() -> None:
    assert SessionTreeStatus.ACTIVE == "active"
    assert SessionTreeStatus.COMPLETED == "completed"
    assert SessionTreeStatus.CANCELLED == "cancelled"


def test_node_version_status_values() -> None:
    assert NodeVersionStatus.RUNNING == "running"
    assert NodeVersionStatus.COMPLETED == "completed"
    assert NodeVersionStatus.CANCELLED == "cancelled"


def test_message_track_status_values() -> None:
    assert MessageTrackStatus.DISPATCHED == "dispatched"
    assert MessageTrackStatus.CONSUMED == "consumed"
    assert MessageTrackStatus.CANCELLED == "cancelled"


# ---------------------------------------------------------------------------
# SessionTreeRecord
# ---------------------------------------------------------------------------


def test_session_tree_record_construction() -> None:
    record = SessionTreeRecord(
        tree_id="tree-1",
        root_node_session_id="root-sid",
        pool_name="default",
        workspace_root="/ws",
        status=SessionTreeStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
    )
    assert record.tree_id == "tree-1"
    assert record.root_node_session_id == "root-sid"
    assert record.pool_name == "default"
    assert record.workspace_root == "/ws"
    assert record.status is SessionTreeStatus.ACTIVE
    assert record.created_at == NOW
    assert record.updated_at == NOW
    assert record.completed_at is None


def test_session_tree_record_completed_at_set() -> None:
    record = SessionTreeRecord(
        tree_id="tree-1",
        root_node_session_id="root-sid",
        pool_name="default",
        workspace_root="/ws",
        status=SessionTreeStatus.COMPLETED,
        created_at=NOW,
        updated_at=NOW + 1000,
        completed_at=NOW + 1000,
    )
    assert record.completed_at == NOW + 1000


def test_session_tree_record_frozen() -> None:
    record = SessionTreeRecord(
        tree_id="tree-1",
        root_node_session_id="root-sid",
        pool_name="default",
        workspace_root="/ws",
        status=SessionTreeStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
    )
    with pytest.raises(ValidationError):
        record.tree_id = "tree-2"  # type: ignore[misc]


def test_session_tree_record_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        SessionTreeRecord(
            tree_id="tree-1",
            root_node_session_id="root-sid",
            pool_name="default",
            workspace_root="/ws",
            status=SessionTreeStatus.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
            unknown_field="x",  # type: ignore[call-arg]
        )


def test_session_tree_record_missing_required_field() -> None:
    with pytest.raises(ValidationError):
        SessionTreeRecord(
            tree_id="tree-1",
            root_node_session_id="root-sid",
            pool_name="default",
            workspace_root="/ws",
            created_at=NOW,
            updated_at=NOW,
        )  # type: ignore[call-arg]


def test_session_tree_record_status_from_string() -> None:
    record = SessionTreeRecord(
        tree_id="tree-1",
        root_node_session_id="root-sid",
        pool_name="default",
        workspace_root="/ws",
        status="active",  # type: ignore[arg-type]
        created_at=NOW,
        updated_at=NOW,
    )
    assert record.status is SessionTreeStatus.ACTIVE


# ---------------------------------------------------------------------------
# TreeNodeRecord
# ---------------------------------------------------------------------------


def test_tree_node_record_root_construction() -> None:
    node = TreeNodeRecord(
        tree_id="tree-1",
        session_id="root-sid",
        parent_session_id=None,
        agent_name="main",
        version=1,
        parent_version=None,
        status=NodeVersionStatus.RUNNING,
        created_at=NOW,
        updated_at=NOW,
    )
    assert node.tree_id == "tree-1"
    assert node.session_id == "root-sid"
    assert node.parent_session_id is None
    assert node.agent_name == "main"
    assert node.version == 1
    assert node.parent_version is None
    assert node.status is NodeVersionStatus.RUNNING


def test_tree_node_record_child_with_parent_version() -> None:
    node = TreeNodeRecord(
        tree_id="tree-1",
        session_id="child-sid",
        parent_session_id="root-sid",
        agent_name="worker",
        version=1,
        parent_version=3,
        status=NodeVersionStatus.COMPLETED,
        created_at=NOW,
        updated_at=NOW + 500,
    )
    assert node.parent_session_id == "root-sid"
    assert node.parent_version == 3
    assert node.status is NodeVersionStatus.COMPLETED


def test_tree_node_record_frozen() -> None:
    node = TreeNodeRecord(
        tree_id="tree-1",
        session_id="root-sid",
        parent_session_id=None,
        agent_name="main",
        version=1,
        parent_version=None,
        status=NodeVersionStatus.RUNNING,
        created_at=NOW,
        updated_at=NOW,
    )
    with pytest.raises(ValidationError):
        node.version = 2  # type: ignore[misc]


def test_tree_node_record_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        TreeNodeRecord(
            tree_id="tree-1",
            session_id="root-sid",
            parent_session_id=None,
            agent_name="main",
            version=1,
            parent_version=None,
            status=NodeVersionStatus.RUNNING,
            created_at=NOW,
            updated_at=NOW,
            extra="x",  # type: ignore[call-arg]
        )


def test_tree_node_record_missing_required_field() -> None:
    with pytest.raises(ValidationError):
        TreeNodeRecord(
            tree_id="tree-1",
            session_id="root-sid",
            parent_session_id=None,
            agent_name="main",
            version=1,
            parent_version=None,
            created_at=NOW,
            updated_at=NOW,
        )  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# MessageTrack
# ---------------------------------------------------------------------------


def test_message_track_construction() -> None:
    track = MessageTrack(
        track_id="msg-1",
        tree_id="tree-1",
        message_id="msg-1",
        message_type="agent_message",
        invocation_id="inv-1",
        target_session_id="child-sid",
        source_session_id="root-sid",
        status=MessageTrackStatus.DISPATCHED,
        dispatched_at=NOW,
    )
    assert track.track_id == "msg-1"
    assert track.message_id == "msg-1"
    assert track.tree_id == "tree-1"
    assert track.message_type == "agent_message"
    assert track.invocation_id == "inv-1"
    assert track.target_session_id == "child-sid"
    assert track.source_session_id == "root-sid"
    assert track.status is MessageTrackStatus.DISPATCHED
    assert track.dispatched_at == NOW
    assert track.consumed_at is None


def test_message_track_consumed() -> None:
    track = MessageTrack(
        track_id="msg-1",
        tree_id="tree-1",
        message_id="msg-1",
        message_type="agent_message",
        invocation_id=None,
        target_session_id="child-sid",
        source_session_id="root-sid",
        status=MessageTrackStatus.CONSUMED,
        dispatched_at=NOW,
        consumed_at=NOW + 100,
    )
    assert track.invocation_id is None
    assert track.consumed_at == NOW + 100
    assert track.status is MessageTrackStatus.CONSUMED


def test_message_track_frozen() -> None:
    track = MessageTrack(
        track_id="msg-1",
        tree_id="tree-1",
        message_id="msg-1",
        message_type="agent_message",
        invocation_id=None,
        target_session_id="child-sid",
        source_session_id="root-sid",
        status=MessageTrackStatus.DISPATCHED,
        dispatched_at=NOW,
    )
    with pytest.raises(ValidationError):
        track.status = MessageTrackStatus.CONSUMED  # type: ignore[misc]


def test_message_track_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        MessageTrack(
            track_id="msg-1",
            tree_id="tree-1",
            message_id="msg-1",
            message_type="agent_message",
            invocation_id=None,
            target_session_id="child-sid",
            source_session_id="root-sid",
            status=MessageTrackStatus.DISPATCHED,
            dispatched_at=NOW,
            unknown="x",  # type: ignore[call-arg]
        )


def test_message_track_missing_required_field() -> None:
    with pytest.raises(ValidationError):
        MessageTrack(
            track_id="msg-1",
            tree_id="tree-1",
            message_type="agent_message",
            invocation_id=None,
            target_session_id="child-sid",
            source_session_id="root-sid",
            status=MessageTrackStatus.DISPATCHED,
            dispatched_at=NOW,
        )  # type: ignore[call-arg]


def test_message_track_status_from_string() -> None:
    track = MessageTrack(
        track_id="msg-1",
        tree_id="tree-1",
        message_id="msg-1",
        message_type="agent_message",
        invocation_id=None,
        target_session_id="child-sid",
        source_session_id="root-sid",
        status="dispatched",  # type: ignore[arg-type]
        dispatched_at=NOW,
    )
    assert track.status is MessageTrackStatus.DISPATCHED
