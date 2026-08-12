"""Unit tests for SessionTreeManager — 8 async methods + pending_input + recover_tree."""

from __future__ import annotations

import asyncio

import pytest

from modex_agent.core.session_registry import InMemorySessionRegistry
from modex_agent.messaging.broker import AddressKind
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.envelope import AgentMessageEnvelope
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.inbox.server_memory import InMemoryInboxServer
from modex_agent.multi_agent.inbox_poller import InboxPoller
from modex_agent.multi_agent.message_type import AgentMessageType
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.session_tree.models import (
    MessageTrack,
    MessageTrackStatus,
    NodeVersionStatus,
    SessionTreeRecord,
    SessionTreeStatus,
    TreeNodeRecord,
)
from modex_agent.multi_agent.session_tree.store_node import InMemoryTreeNodeStore
from modex_agent.multi_agent.session_tree.store_track import InMemoryMessageTrackStore
from modex_agent.multi_agent.session_tree.store_tree import InMemorySessionTreeStore
from modex_agent.utils.time import now_ms


class _StubPoller(InboxPoller):
    def __init__(self) -> None:
        self.signaled = False

    def signal_wakeup(self) -> None:
        self.signaled = True


class _RaisingProducer(InboxProducer):
    async def send(self, session_id: str, envelope: AgentMessageEnvelope) -> bool:
        raise RuntimeError("boom")


class _FlakyProducer(InboxProducer):
    """Succeeds on the first send, fails on every subsequent send."""

    def __init__(self, server: InMemoryInboxServer) -> None:
        super().__init__(server=server)
        self._calls = 0

    async def send(self, session_id: str, envelope: AgentMessageEnvelope) -> bool:
        self._calls += 1
        if self._calls > 1:
            raise RuntimeError("boom")
        return await super().send(session_id, envelope)


def _envelope(
    msg_type: str,
    *,
    message_id: str = "m1",
    target_sid: str = "inv.sub",
    parent_sid: str | None = "root.main",
    invocation_id: str | None = "inv1",
) -> AgentMessageEnvelope:
    return AgentMessageEnvelope(
        payload={"content": "test"},
        source=AgentAddress(kind=AddressKind.AGENT, name="main"),
        target=AgentAddress(kind=AddressKind.AGENT, name="sub"),
        message_type=msg_type,
        session_id="conv1",
        agent_session_id=target_sid,
        parent_session_id=parent_sid,
        invocation_id=invocation_id,
        message_id=message_id,
    )


def _make_manager(*, failing: bool = False, flaky: bool = False) -> tuple[SessionTreeManager, InMemoryInboxServer]:
    server = InMemoryInboxServer()
    if failing:
        producer: InboxProducer = _RaisingProducer(server)
    elif flaky:
        producer = _FlakyProducer(server)
    else:
        producer = InboxProducer(server=server)
    consumer = InboxConsumer(server=server)
    bus = LocalAgentMessageBus(producer=producer, consumer=consumer)
    manager = SessionTreeManager(
        tree_store=InMemorySessionTreeStore(),
        node_store=InMemoryTreeNodeStore(),
        track_store=InMemoryMessageTrackStore(),
        bus=bus,
        poller=_StubPoller(),
        pool_name="pool1",
        workspace_root="/tmp",
        session_registry=InMemorySessionRegistry(),
    )
    return manager, server


async def _setup_tree(
    manager: SessionTreeManager,
    *,
    tree_id: str = "t1",
    root_sid: str = "root.main",
) -> None:
    tree = SessionTreeRecord(
        tree_id=tree_id,
        root_node_session_id=root_sid,
        pool_name="pool1",
        workspace_root="/tmp",
        status=SessionTreeStatus.ACTIVE,
        created_at=now_ms(),
        updated_at=now_ms(),
    )
    await manager._tree_store.create(tree)
    await manager._node_store.create(
        TreeNodeRecord(
            tree_id=tree_id,
            session_id=root_sid,
            parent_session_id=None,
            agent_name="main",
            version=1,
            parent_version=None,
            status=NodeVersionStatus.COMPLETED,
            created_at=now_ms(),
            updated_at=now_ms(),
        )
    )


async def _add_subagent(
    manager: SessionTreeManager,
    *,
    tree_id: str = "t1",
    session_id: str = "inv.sub",
    parent_sid: str = "root.main",
    status: NodeVersionStatus = NodeVersionStatus.COMPLETED,
) -> None:
    await manager._node_store.create(
        TreeNodeRecord(
            tree_id=tree_id,
            session_id=session_id,
            parent_session_id=parent_sid,
            agent_name="sub",
            version=1,
            parent_version=None,
            status=status,
            created_at=now_ms(),
            updated_at=now_ms(),
        )
    )


class TestDeliver:
    async def test_task_request_creates_track(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        await _add_subagent(manager)
        env = _envelope(AgentMessageType.TASK_REQUEST, target_sid="inv.sub")
        await manager.deliver("inv.sub", env)
        track = await manager._track_store.get_by_message_id("t1", "m1")
        assert track is not None
        assert track.status is MessageTrackStatus.DISPATCHED
        assert track.message_type == AgentMessageType.TASK_REQUEST.value
        assert track.source_session_id == "root.main"

    async def test_task_request_tree_id_from_parent_when_node_missing(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        env = _envelope(AgentMessageType.TASK_REQUEST, target_sid="inv.sub", parent_sid="root.main")
        await manager.deliver("inv.sub", env)
        track = await manager._track_store.get_by_message_id("t1", "m1")
        assert track is not None
        assert track.tree_id == "t1"

    async def test_external_input_no_track_pending(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        await _add_subagent(manager)
        env = _envelope(AgentMessageType.EXTERNAL_INPUT, target_sid="inv.sub")
        await manager.deliver("inv.sub", env)
        track = await manager._track_store.get_by_message_id("t1", "m1")
        assert track is None
        assert "inv.sub" in manager._pending_input

    async def test_external_input_track_consume_creates_dispatched_track(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        await _add_subagent(manager)
        env = _envelope(AgentMessageType.EXTERNAL_INPUT, target_sid="inv.sub")

        await manager.deliver("inv.sub", env, track_consume=True)

        track = await manager._track_store.get_by_message_id("t1", "m1")
        assert track is not None
        assert track.status is MessageTrackStatus.DISPATCHED
        assert track.message_type is AgentMessageType.EXTERNAL_INPUT

    async def test_agent_message_no_track_pending(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        await _add_subagent(manager)
        env = _envelope(AgentMessageType.AGENT_MESSAGE, target_sid="inv.sub")
        await manager.deliver("inv.sub", env)
        track = await manager._track_store.get_by_message_id("t1", "m1")
        assert track is None
        assert "inv.sub" in manager._pending_input

    async def test_agent_result_closes_task_request_creates_track(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        await _add_subagent(manager)
        task_env = _envelope(
            AgentMessageType.TASK_REQUEST, message_id="m_task", target_sid="inv.sub"
        )
        await manager.deliver("inv.sub", task_env)
        result_env = _envelope(
            AgentMessageType.AGENT_RESULT,
            message_id="m_result",
            target_sid="root.main",
            parent_sid="root.main",
            invocation_id="inv1",
        )
        await manager.deliver("root.main", result_env)
        task_track = await manager._track_store.get("m_task")
        assert task_track is not None
        assert task_track.status is MessageTrackStatus.CONSUMED
        result_track = await manager._track_store.get("m_result")
        assert result_track is not None
        assert result_track.status is MessageTrackStatus.DISPATCHED
        assert result_track.source_session_id == "inv.sub"

    async def test_task_request_send_fails_cancels_track(self) -> None:
        manager, _ = _make_manager(failing=True)
        await _setup_tree(manager)
        await _add_subagent(manager)
        env = _envelope(AgentMessageType.TASK_REQUEST, target_sid="inv.sub")
        await manager.deliver("inv.sub", env)
        track = await manager._track_store.get("m1")
        assert track is not None
        assert track.status is MessageTrackStatus.CANCELLED

    async def test_external_input_send_fails_discards_pending(self) -> None:
        manager, _ = _make_manager(failing=True)
        await _setup_tree(manager)
        await _add_subagent(manager)
        env = _envelope(AgentMessageType.EXTERNAL_INPUT, target_sid="inv.sub")
        await manager.deliver("inv.sub", env)
        assert "inv.sub" not in manager._pending_input

    async def test_agent_result_send_fails_reopens_task_request(self) -> None:
        manager, _ = _make_manager(flaky=True)
        await _setup_tree(manager)
        await _add_subagent(manager)
        task_env = _envelope(
            AgentMessageType.TASK_REQUEST, message_id="m_task", target_sid="inv.sub"
        )
        await manager.deliver("inv.sub", task_env)
        result_env = _envelope(
            AgentMessageType.AGENT_RESULT,
            message_id="m_result",
            target_sid="root.main",
            invocation_id="inv1",
        )
        await manager.deliver("root.main", result_env)
        task_track = await manager._track_store.get("m_task")
        assert task_track is not None
        assert task_track.status is MessageTrackStatus.DISPATCHED
        result_track = await manager._track_store.get("m_result")
        assert result_track is not None
        assert result_track.status is MessageTrackStatus.CANCELLED


class TestOnConsumed:
    async def test_agent_result_closes_track(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        await _add_subagent(manager)
        result_env = _envelope(
            AgentMessageType.AGENT_RESULT,
            message_id="m_result",
            target_sid="root.main",
        )
        await manager.deliver("root.main", result_env)
        track = await manager._track_store.get("m_result")
        assert track is not None
        assert track.status is MessageTrackStatus.DISPATCHED
        await manager.on_consumed("root.main", result_env)
        track = await manager._track_store.get("m_result")
        assert track is not None
        assert track.status is MessageTrackStatus.CONSUMED

    async def test_task_request_does_not_close(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        await _add_subagent(manager)
        env = _envelope(AgentMessageType.TASK_REQUEST, target_sid="inv.sub")
        await manager.deliver("inv.sub", env)
        await manager.on_consumed("inv.sub", env)
        track = await manager._track_store.get("m1")
        assert track is not None
        assert track.status is MessageTrackStatus.DISPATCHED

    async def test_external_input_clears_pending(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        env = _envelope(AgentMessageType.EXTERNAL_INPUT, target_sid="inv.sub")
        await manager.deliver("inv.sub", env)
        assert "inv.sub" in manager._pending_input
        await manager.on_consumed("inv.sub", env)
        assert "inv.sub" not in manager._pending_input

    async def test_agent_message_clears_pending(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        env = _envelope(AgentMessageType.AGENT_MESSAGE, target_sid="inv.sub")
        await manager.deliver("inv.sub", env)
        assert "inv.sub" in manager._pending_input
        await manager.on_consumed("inv.sub", env)
        assert "inv.sub" not in manager._pending_input


class TestDispatchLifecycle:
    async def test_on_dispatch_start(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        await _add_subagent(manager, status=NodeVersionStatus.COMPLETED)
        manager._pending_input.add("inv.sub")
        await manager.on_dispatch_start("inv.sub")
        assert "inv.sub" not in manager._pending_input
        assert "inv.sub" in manager._running
        node = await manager._node_store.get("inv.sub")
        assert node is not None
        assert node.status is NodeVersionStatus.RUNNING
        assert node.version == 2
        tree = await manager._tree_store.get("t1")
        assert tree is not None
        assert tree.status is SessionTreeStatus.ACTIVE

    async def test_on_dispatch_end_closes_tracks_node_completed(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        await _add_subagent(manager)
        env = _envelope(AgentMessageType.TASK_REQUEST, target_sid="inv.sub")
        await manager.deliver("inv.sub", env)
        await manager.on_dispatch_start("inv.sub")
        await manager.on_dispatch_end("inv.sub")
        assert "inv.sub" not in manager._running
        node = await manager._node_store.get("inv.sub")
        assert node is not None
        assert node.status is NodeVersionStatus.COMPLETED
        track = await manager._track_store.get("m1")
        assert track is not None
        assert track.status is MessageTrackStatus.CONSUMED

    async def test_on_dispatch_end_tree_completed_when_quiesced(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        await _add_subagent(manager)
        env = _envelope(AgentMessageType.TASK_REQUEST, target_sid="inv.sub")
        await manager.deliver("inv.sub", env)
        await manager.on_dispatch_start("inv.sub")
        await manager.on_dispatch_end("inv.sub")
        tree = await manager._tree_store.get("t1")
        assert tree is not None
        assert tree.status is SessionTreeStatus.COMPLETED


class TestIsQuiesced:
    async def test_false_when_dispatched(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        await _add_subagent(manager)
        env = _envelope(AgentMessageType.TASK_REQUEST, target_sid="inv.sub")
        await manager.deliver("inv.sub", env)
        assert await manager.is_quiesced("t1") is False

    async def test_false_when_running(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        await _add_subagent(manager)
        manager._running.add("inv.sub")
        assert await manager.is_quiesced("t1") is False

    async def test_false_when_pending(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        await _add_subagent(manager)
        manager._pending_input.add("inv.sub")
        assert await manager.is_quiesced("t1") is False

    async def test_true_when_empty(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        assert await manager.is_quiesced("t1") is True


class TestWaitQuiesce:
    async def test_returns_none_when_already_quiesced(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        result = await manager.wait_quiesce("t1")
        assert result is None

    async def test_blocks_while_tree_is_not_quiesced(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        await _add_subagent(manager)
        manager._running.add("inv.sub")

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(manager.wait_quiesce("t1"), timeout=0.05)

    async def test_wakes_on_signal(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        await _add_subagent(manager)
        manager._running.add("inv.sub")
        task = asyncio.create_task(manager.wait_quiesce("t1"))
        await asyncio.sleep(0.02)
        manager._running.discard("inv.sub")
        manager._signal("t1")
        result = await asyncio.wait_for(task, timeout=1.0)
        assert result is None


class TestOnSessionEvicted:
    async def test_cancels_tracks_clears_sets(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        await _add_subagent(manager)
        env = _envelope(AgentMessageType.TASK_REQUEST, target_sid="inv.sub")
        await manager.deliver("inv.sub", env)
        manager._running.add("inv.sub")
        manager._pending_input.add("inv.sub")
        await manager.on_session_evicted("inv.sub")
        assert "inv.sub" not in manager._running
        assert "inv.sub" not in manager._pending_input
        track = await manager._track_store.get("m1")
        assert track is not None
        assert track.status is MessageTrackStatus.CANCELLED

    async def test_root_eviction_cancels_tree(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        await manager.on_session_evicted("root.main")
        tree = await manager._tree_store.get("t1")
        assert tree is not None
        assert tree.status is SessionTreeStatus.CANCELLED


class TestRecoverTree:
    async def test_consumed_agent_result_marked_consumed(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        await _add_subagent(manager)
        await manager._track_store.create(
            MessageTrack(
                track_id="m1",
                tree_id="t1",
                message_id="m1",
                message_type=AgentMessageType.AGENT_RESULT.value,
                invocation_id="inv1",
                target_session_id="root.main",
                source_session_id="inv.sub",
                status=MessageTrackStatus.DISPATCHED,
                dispatched_at=now_ms(),
            )
        )
        await manager.recover_tree("t1")
        track = await manager._track_store.get("m1")
        assert track is not None
        assert track.status is MessageTrackStatus.CONSUMED

    async def test_pending_track_stays_dispatched(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        await _add_subagent(manager)
        env = _envelope(AgentMessageType.TASK_REQUEST, target_sid="inv.sub", message_id="m1")
        await manager.deliver("inv.sub", env)
        await manager.recover_tree("t1")
        track = await manager._track_store.get("m1")
        assert track is not None
        assert track.status is MessageTrackStatus.DISPATCHED

    async def test_consumed_task_request_completed_node(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        await _add_subagent(manager, status=NodeVersionStatus.COMPLETED)
        await manager._track_store.create(
            MessageTrack(
                track_id="m1",
                tree_id="t1",
                message_id="m1",
                message_type=AgentMessageType.TASK_REQUEST.value,
                invocation_id="inv1",
                target_session_id="inv.sub",
                source_session_id="root.main",
                status=MessageTrackStatus.DISPATCHED,
                dispatched_at=now_ms(),
            )
        )
        await manager.recover_tree("t1")
        track = await manager._track_store.get("m1")
        assert track is not None
        assert track.status is MessageTrackStatus.CONSUMED

    async def test_consumed_task_request_stale_running_node(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        await _add_subagent(manager, status=NodeVersionStatus.RUNNING)
        await manager._track_store.create(
            MessageTrack(
                track_id="m1",
                tree_id="t1",
                message_id="m1",
                message_type=AgentMessageType.TASK_REQUEST.value,
                invocation_id="inv1",
                target_session_id="inv.sub",
                source_session_id="root.main",
                status=MessageTrackStatus.DISPATCHED,
                dispatched_at=now_ms(),
            )
        )
        await manager.recover_tree("t1")
        track = await manager._track_store.get("m1")
        assert track is not None
        assert track.status is MessageTrackStatus.CONSUMED
        node = await manager._node_store.get("inv.sub")
        assert node is not None
        assert node.status is NodeVersionStatus.COMPLETED

    async def test_stale_running_node_without_tracks(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        await _add_subagent(manager, status=NodeVersionStatus.RUNNING)
        await manager.recover_tree("t1")
        node = await manager._node_store.get("inv.sub")
        assert node is not None
        assert node.status is NodeVersionStatus.COMPLETED

    async def test_rebuilds_pending_input(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        await _add_subagent(manager)
        env = _envelope(AgentMessageType.EXTERNAL_INPUT, target_sid="inv.sub", message_id="m_ext")
        await manager.deliver("inv.sub", env)
        manager._pending_input.clear()
        await manager.recover_tree("t1")
        assert "inv.sub" in manager._pending_input

    async def test_does_not_reconstruct_running(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        await _add_subagent(manager, status=NodeVersionStatus.RUNNING)
        await manager.recover_tree("t1")
        assert "inv.sub" not in manager._running


class TestQuiesceIntegration:
    async def test_agent_message_blocks_quiesce(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        await _add_subagent(manager)
        env = _envelope(AgentMessageType.AGENT_MESSAGE, target_sid="inv.sub")
        await manager.deliver("inv.sub", env)
        assert await manager.is_quiesced("t1") is False
        await manager.on_consumed("inv.sub", env)
        assert await manager.is_quiesced("t1") is True


class TestEnsureNode:
    async def test_external_input_creates_root_tree_and_node(self) -> None:
        manager, _ = _make_manager()
        env = _envelope(AgentMessageType.EXTERNAL_INPUT, target_sid="conv1.main", parent_sid=None)
        await manager.deliver("conv1.main", env)
        tree = await manager._tree_store.get("conv1.main")
        assert tree is not None
        assert tree.status is SessionTreeStatus.ACTIVE
        assert tree.root_node_session_id == "conv1.main"
        node = await manager._node_store.get("conv1.main")
        assert node is not None
        assert node.tree_id == "conv1.main"
        assert node.parent_session_id is None

    async def test_tree_id_for_session_returns_existing_tree_id(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)

        tree_id = await manager.tree_id_for_session("root.main")

        assert tree_id == "t1"

    async def test_tree_id_for_session_returns_none_for_unknown_session(self) -> None:
        manager, _ = _make_manager()

        tree_id = await manager.tree_id_for_session("unknown.main")

        assert tree_id is None

    async def test_task_request_creates_child_node_in_parent_tree(self) -> None:
        manager, _ = _make_manager()
        root_env = _envelope(
            AgentMessageType.EXTERNAL_INPUT, target_sid="conv1.main", parent_sid=None
        )
        await manager.deliver("conv1.main", root_env)
        task_env = _envelope(
            AgentMessageType.TASK_REQUEST,
            message_id="m_task",
            target_sid="inv.sub",
            parent_sid="conv1.main",
        )
        await manager.deliver("inv.sub", task_env)
        child = await manager._node_store.get("inv.sub")
        assert child is not None
        assert child.tree_id == "conv1.main"
        assert child.parent_session_id == "conv1.main"

    async def test_full_lifecycle_empty_stores(self) -> None:
        manager, _ = _make_manager()
        root_env = _envelope(
            AgentMessageType.EXTERNAL_INPUT,
            message_id="m_ext",
            target_sid="conv1.main",
            parent_sid=None,
        )
        await manager.deliver("conv1.main", root_env)
        assert await manager.is_quiesced("conv1.main") is False
        await manager.on_consumed("conv1.main", root_env)
        assert await manager.is_quiesced("conv1.main") is True
        task_env = _envelope(
            AgentMessageType.TASK_REQUEST,
            message_id="m_task",
            target_sid="inv.sub",
            parent_sid="conv1.main",
        )
        await manager.deliver("inv.sub", task_env)
        assert await manager.is_quiesced("conv1.main") is False
        await manager.on_dispatch_start("inv.sub")
        assert "inv.sub" in manager._running
        await manager.on_dispatch_end("inv.sub")
        assert "inv.sub" not in manager._running
        result_env = _envelope(
            AgentMessageType.AGENT_RESULT,
            message_id="m_result",
            target_sid="conv1.main",
            parent_sid="conv1.main",
            invocation_id="inv1",
        )
        await manager.deliver("conv1.main", result_env)
        await manager.on_consumed("conv1.main", result_env)
        assert await manager.is_quiesced("conv1.main") is True
        tree = await manager._tree_store.get("conv1.main")
        assert tree is not None
        assert tree.status is SessionTreeStatus.COMPLETED


class TestDeliverDedup:
    async def test_duplicate_task_request_cancels_track(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        await _add_subagent(manager)
        env = _envelope(AgentMessageType.TASK_REQUEST, message_id="m_dup", target_sid="inv.sub")
        await manager.deliver("inv.sub", env)
        track = await manager._track_store.get("m_dup")
        assert track is not None
        assert track.status is MessageTrackStatus.DISPATCHED
        await manager.deliver("inv.sub", env)
        track = await manager._track_store.get("m_dup")
        assert track is not None
        assert track.status is MessageTrackStatus.CANCELLED

    async def test_duplicate_agent_result_reopens_task_request(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        await _add_subagent(manager)
        task_env = _envelope(
            AgentMessageType.TASK_REQUEST, message_id="m_task", target_sid="inv.sub"
        )
        await manager.deliver("inv.sub", task_env)
        result_env = _envelope(
            AgentMessageType.AGENT_RESULT,
            message_id="m_result",
            target_sid="root.main",
            parent_sid="root.main",
            invocation_id="inv1",
        )
        await manager.deliver("root.main", result_env)
        task_track = await manager._track_store.get("m_task")
        assert task_track is not None
        assert task_track.status is MessageTrackStatus.CONSUMED
        result_track = await manager._track_store.get("m_result")
        assert result_track is not None
        assert result_track.status is MessageTrackStatus.DISPATCHED
        await manager.deliver("root.main", result_env)
        task_track = await manager._track_store.get("m_task")
        assert task_track is not None
        assert task_track.status is MessageTrackStatus.CONSUMED
        result_track = await manager._track_store.get("m_result")
        assert result_track is not None
        assert result_track.status is MessageTrackStatus.CANCELLED

    async def test_duplicate_external_input_discards_pending(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        await _add_subagent(manager)
        env = _envelope(AgentMessageType.EXTERNAL_INPUT, message_id="m_ext", target_sid="inv.sub")
        await manager.deliver("inv.sub", env)
        assert "inv.sub" in manager._pending_input
        await manager.deliver("inv.sub", env)
        assert "inv.sub" not in manager._pending_input


# ---------------------------------------------------------------------------
# _maybe_bind_session + SessionBindingStore integration
# ---------------------------------------------------------------------------


def _make_manager_with_binding() -> tuple[SessionTreeManager, InMemoryInboxServer, InMemorySessionBindingStore]:
    from modex_agent.multi_agent.session_tree.session_binding import (
        InMemorySessionBindingStore,
    )

    server = InMemoryInboxServer()
    producer = InboxProducer(server=server)
    consumer = InboxConsumer(server=server)
    bus = LocalAgentMessageBus(producer=producer, consumer=consumer)
    binding_store = InMemorySessionBindingStore()
    manager = SessionTreeManager(
        tree_store=InMemorySessionTreeStore(),
        node_store=InMemoryTreeNodeStore(),
        track_store=InMemoryMessageTrackStore(),
        bus=bus,
        poller=_StubPoller(),
        pool_name="pool1",
        workspace_root="/tmp",
        session_registry=InMemorySessionRegistry(),
        binding_store=binding_store,
    )
    return manager, server, binding_store


def _envelope_with_gid(
    msg_type: str,
    *,
    graph_instance_id: int | None = 42,
    target_sid: str = "inv.sub",
    message_id: str = "m1",
) -> AgentMessageEnvelope:
    return AgentMessageEnvelope(
        payload={"content": "test"},
        source=AgentAddress(kind=AddressKind.AGENT, name="main"),
        target=AgentAddress(kind=AddressKind.AGENT, name="sub"),
        message_type=msg_type,
        session_id="conv1",
        agent_session_id=target_sid,
        parent_session_id="root.main",
        invocation_id="inv1",
        message_id=message_id,
        metadata={"graph_instance_id": graph_instance_id} if graph_instance_id is not None else {},
    )


class TestMaybeBindSession:

    async def test_deliver_auto_creates_binding_from_envelope_metadata(self) -> None:
        manager, _, binding_store = _make_manager_with_binding()
        await _setup_tree(manager)
        await _add_subagent(manager)
        env = _envelope_with_gid(AgentMessageType.EXTERNAL_INPUT, target_sid="inv.sub")
        await manager.deliver("inv.sub", env)
        binding = binding_store.get("inv.sub")
        assert binding is not None
        assert binding.task_id == 42
        assert binding.graph_node_name is None
        assert binding.is_node_execution is False

    async def test_deliver_does_not_overwrite_existing_binding(self) -> None:
        from modex_agent.multi_agent.session_tree.session_binding import (
            SessionBinding,
        )
        from modex_agent.pipeline.turn_context_config import GraphTurnArtifacts
        from tests.unit.pipeline.test_turn_runner import _StubTool

        manager, _, binding_store = _make_manager_with_binding()
        await _setup_tree(manager)
        await _add_subagent(manager)
        binding_store.bind(
            "inv.sub",
            SessionBinding(
                task_id=42,
                graph_node_name="my_node",
                is_node_execution=True,
                graph_artifacts=GraphTurnArtifacts(
                    deliver_tool=_StubTool(),
                    topology_section="## topology",
                    node_description="desc",
                    knowledge_config=None,
                ),
            ),
        )
        env = _envelope_with_gid(AgentMessageType.EXTERNAL_INPUT, target_sid="inv.sub")
        await manager.deliver("inv.sub", env)
        binding = binding_store.get("inv.sub")
        assert binding is not None
        assert binding.graph_node_name == "my_node"
        assert binding.is_node_execution is True

    async def test_deliver_without_graph_instance_id_does_not_bind(self) -> None:
        manager, _, binding_store = _make_manager_with_binding()
        await _setup_tree(manager)
        await _add_subagent(manager)
        env = _envelope_with_gid(
            AgentMessageType.EXTERNAL_INPUT, graph_instance_id=None, target_sid="inv.sub"
        )
        await manager.deliver("inv.sub", env)
        assert binding_store.get("inv.sub") is None

    async def test_deliver_without_binding_store_does_not_crash(self) -> None:
        manager, _ = _make_manager()
        await _setup_tree(manager)
        await _add_subagent(manager)
        env = _envelope_with_gid(AgentMessageType.EXTERNAL_INPUT, target_sid="inv.sub")
        await manager.deliver("inv.sub", env)

    async def test_conflicting_task_id_raises(self) -> None:
        from modex_agent.multi_agent.session_tree.session_binding import (
            SessionBinding,
        )

        manager, _, binding_store = _make_manager_with_binding()
        await _setup_tree(manager)
        await _add_subagent(manager)
        binding_store.bind("inv.sub", SessionBinding(task_id=42))
        env = _envelope_with_gid(
            AgentMessageType.EXTERNAL_INPUT, graph_instance_id=99, target_sid="inv.sub"
        )
        with pytest.raises(ValueError, match="Concurrent graph instances"):
            await manager.deliver("inv.sub", env)

    async def test_on_session_evicted_unbinds(self) -> None:
        from modex_agent.multi_agent.session_tree.session_binding import (
            SessionBinding,
        )

        manager, _, binding_store = _make_manager_with_binding()
        await _setup_tree(manager)
        binding_store.bind("root.main", SessionBinding(task_id=42))
        assert binding_store.get("root.main") is not None
        await manager.on_session_evicted("root.main")
        assert binding_store.get("root.main") is None
