"""Phase 2 integration tests: SessionTreeManager end-to-end behavior.

12 scenarios verifying the full session-tree lifecycle with REAL
SessionTreeManager + InMemory stores + real LocalAgentMessageBus +
real InMemoryInboxMQ. The only non-production component is _StubPoller
(a real InboxPoller subclass that needs no AgentPool — the manager
only calls signal_wakeup on it).

Each test constructs a fresh manager with fresh InMemory stores for
isolation. The on_consumed callback is wired to the real consumer
(production pattern from todo 19), so bus.consume triggers the real
on_consumed path.
"""

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

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Real helpers — no mocks for tree internals
# ---------------------------------------------------------------------------


class _StubPoller(InboxPoller):
    """Real InboxPoller subclass that needs no AgentPool.

    The manager only calls ``signal_wakeup``; this subclass provides it
    without the full pool wiring (which would require an agent stack).
    """

    def __init__(self) -> None:
        self.signaled = False

    def signal_wakeup(self) -> None:
        self.signaled = True


def _make_manager() -> tuple[SessionTreeManager, InMemoryInboxServer]:
    server = InMemoryInboxServer()
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
    consumer.set_on_consumed(manager.on_consumed)
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


# ---------------------------------------------------------------------------
# (a) quiesce after subagent completes
# ---------------------------------------------------------------------------


async def test_quiesce_after_subagent_completes() -> None:
    manager, _ = _make_manager()
    await _setup_tree(manager)
    await _add_subagent(manager)

    task_env = _envelope(AgentMessageType.TASK_REQUEST, message_id="m1", target_sid="inv.sub")
    await manager.deliver("inv.sub", task_env)
    assert not await manager.is_quiesced("t1")

    result_env = _envelope(
        AgentMessageType.AGENT_RESULT,
        message_id="m2",
        target_sid="root.main",
        parent_sid="inv.sub",
    )
    await manager.deliver("root.main", result_env)
    assert not await manager.is_quiesced("t1")

    await manager._bus.consume("root.main", limit=10)
    assert await manager.is_quiesced("t1")


# ---------------------------------------------------------------------------
# (b) not quiesced while subagent running
# ---------------------------------------------------------------------------


async def test_not_quiesced_while_subagent_running() -> None:
    manager, _ = _make_manager()
    await _setup_tree(manager)
    await _add_subagent(manager)

    task_env = _envelope(AgentMessageType.TASK_REQUEST, message_id="m1", target_sid="inv.sub")
    await manager.deliver("inv.sub", task_env)
    assert not await manager.is_quiesced("t1")


# ---------------------------------------------------------------------------
# (c) fold-in consume does not trigger on_dispatch_start
# ---------------------------------------------------------------------------


async def test_fold_in_no_new_version() -> None:
    manager, _ = _make_manager()
    await _setup_tree(manager)

    await manager.on_dispatch_start("root.main")
    node = await manager._node_store.get("root.main")
    assert node is not None
    version_before = node.version
    assert version_before == 2

    env = _envelope(
        AgentMessageType.AGENT_MESSAGE,
        message_id="m3",
        target_sid="root.main",
        parent_sid="",
        invocation_id=None,
    )
    await manager.deliver("root.main", env)
    assert "root.main" in manager._pending_input

    await manager._bus.consume("root.main", limit=10)
    assert "root.main" not in manager._pending_input

    node_after = await manager._node_store.get("root.main")
    assert node_after is not None
    assert node_after.version == version_before


# ---------------------------------------------------------------------------
# (d) error exit sets node COMPLETED (no success parameter)
# ---------------------------------------------------------------------------


async def test_error_exit_completed() -> None:
    manager, _ = _make_manager()
    await _setup_tree(manager)
    await _add_subagent(manager)

    task_env = _envelope(AgentMessageType.TASK_REQUEST, message_id="m1", target_sid="inv.sub")
    await manager.deliver("inv.sub", task_env)

    await manager.on_dispatch_start("inv.sub")
    assert "inv.sub" in manager._running

    await manager.on_dispatch_end("inv.sub")
    assert "inv.sub" not in manager._running

    node = await manager._node_store.get("inv.sub")
    assert node is not None
    assert node.status == NodeVersionStatus.COMPLETED

    track = await manager._track_store.get("m1")
    assert track is not None
    assert track.status == MessageTrackStatus.CONSUMED


# ---------------------------------------------------------------------------
# (e) peer message creates no track, no sender quiesce impact
# ---------------------------------------------------------------------------


async def test_peer_message_no_track_no_sender_impact() -> None:
    manager, _ = _make_manager()
    await _setup_tree(manager)

    assert await manager.is_quiesced("t1")

    env = _envelope(
        AgentMessageType.AGENT_MESSAGE,
        message_id="m3",
        target_sid="other.session",
        parent_sid="",
        invocation_id=None,
    )
    await manager.deliver("other.session", env)

    assert len(await manager._track_store.list_dispatched("t1")) == 0
    assert await manager.is_quiesced("t1")


# ---------------------------------------------------------------------------
# (f) wait_quiesce blocks then returns after AGENT_RESULT consumed
# ---------------------------------------------------------------------------


async def test_wait_quiesce_blocks_then_returns() -> None:
    manager, _ = _make_manager()
    await _setup_tree(manager)
    await _add_subagent(manager)

    task_env = _envelope(AgentMessageType.TASK_REQUEST, message_id="m1", target_sid="inv.sub")
    await manager.deliver("inv.sub", task_env)

    wait_task = asyncio.create_task(manager.wait_quiesce("t1"))
    await asyncio.sleep(0.1)
    assert not wait_task.done()

    result_env = _envelope(
        AgentMessageType.AGENT_RESULT,
        message_id="m2",
        target_sid="root.main",
        parent_sid="inv.sub",
    )
    await manager.deliver("root.main", result_env)
    await manager._bus.consume("root.main", limit=10)

    result = await asyncio.wait_for(wait_task, timeout=5.0)
    assert result is None


# ---------------------------------------------------------------------------
# (g) EXTERNAL_INPUT blocks quiesce until dispatch starts
# ---------------------------------------------------------------------------


async def test_pending_input_blocks_quiesce_external() -> None:
    manager, _ = _make_manager()
    await _setup_tree(manager)

    env = _envelope(
        AgentMessageType.EXTERNAL_INPUT,
        message_id="m3",
        target_sid="root.main",
        parent_sid="",
        invocation_id=None,
    )
    await manager.deliver("root.main", env)
    assert "root.main" in manager._pending_input
    assert not await manager.is_quiesced("t1")

    await manager.on_dispatch_start("root.main")
    assert "root.main" not in manager._pending_input

    await manager.on_dispatch_end("root.main")
    assert await manager.is_quiesced("t1")


# ---------------------------------------------------------------------------
# (h) AGENT_MESSAGE to receiver blocks quiesce until consumed
# ---------------------------------------------------------------------------


async def test_pending_input_blocks_quiesce_agent_message() -> None:
    manager, _ = _make_manager()
    await _setup_tree(manager)

    env = _envelope(
        AgentMessageType.AGENT_MESSAGE,
        message_id="m3",
        target_sid="root.main",
        parent_sid="",
        invocation_id=None,
    )
    await manager.deliver("root.main", env)
    assert "root.main" in manager._pending_input
    assert not await manager.is_quiesced("t1")

    await manager._bus.consume("root.main", limit=10)
    assert "root.main" not in manager._pending_input
    assert await manager.is_quiesced("t1")


# ---------------------------------------------------------------------------
# (i) cross-pool peer message routes to receiver's tree/bus, not sender's
# ---------------------------------------------------------------------------


async def test_cross_pool_peer_uses_receiver_tree() -> None:
    sender_mgr, _ = _make_manager()
    receiver_mgr, _ = _make_manager()
    await _setup_tree(receiver_mgr, tree_id="rt1", root_sid="recv.mainB")

    env = _envelope(
        AgentMessageType.AGENT_MESSAGE,
        message_id="peer1",
        target_sid="recv.mainB",
        parent_sid="",
        invocation_id=None,
    )

    await receiver_mgr.deliver("recv.mainB", env)

    assert await receiver_mgr._bus.contains_pending("recv.mainB", "peer1")
    assert not await sender_mgr._bus.contains_pending("recv.mainB", "peer1")

    assert "recv.mainB" in receiver_mgr._pending_input
    assert "recv.mainB" not in sender_mgr._pending_input


# ---------------------------------------------------------------------------
# (j) recover_tree marks stale terminal, rebuilds pending_input
# ---------------------------------------------------------------------------


async def test_recover_tree_marks_stale_terminal() -> None:
    manager, _ = _make_manager()
    tree_id = "t1"
    root_sid = "root.main"
    sub_sid = "inv.sub"

    await manager._tree_store.create(
        SessionTreeRecord(
            tree_id=tree_id,
            root_node_session_id=root_sid,
            pool_name="pool1",
            workspace_root="/tmp",
            status=SessionTreeStatus.ACTIVE,
            created_at=now_ms(),
            updated_at=now_ms(),
        )
    )
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
    await manager._node_store.create(
        TreeNodeRecord(
            tree_id=tree_id,
            session_id=sub_sid,
            parent_session_id=root_sid,
            agent_name="sub",
            version=2,
            parent_version=1,
            status=NodeVersionStatus.RUNNING,
            created_at=now_ms(),
            updated_at=now_ms(),
        )
    )
    await manager._track_store.create(
        MessageTrack(
            track_id="m1",
            tree_id=tree_id,
            message_id="m1",
            message_type=AgentMessageType.TASK_REQUEST.value,
            invocation_id="inv1",
            target_session_id=sub_sid,
            source_session_id=root_sid,
            status=MessageTrackStatus.DISPATCHED,
            dispatched_at=now_ms(),
        )
    )

    agent_msg_env = _envelope(
        AgentMessageType.AGENT_MESSAGE,
        message_id="m3",
        target_sid=root_sid,
        parent_sid="",
        invocation_id=None,
    )
    await manager._bus.send(root_sid, agent_msg_env)

    assert not manager._running
    assert not manager._pending_input

    await manager.recover_tree(tree_id)

    sub_node = await manager._node_store.get(sub_sid)
    assert sub_node is not None
    assert sub_node.status == NodeVersionStatus.COMPLETED

    task_track = await manager._track_store.get("m1")
    assert task_track is not None
    assert task_track.status == MessageTrackStatus.CONSUMED

    assert not manager._running
    assert root_sid in manager._pending_input
    assert sub_sid not in manager._pending_input


# ---------------------------------------------------------------------------
# (k) duplicate deliver creates no duplicate tracks (track_id == message_id)
# ---------------------------------------------------------------------------


async def test_delivery_idempotent_duplicate() -> None:
    manager, _ = _make_manager()
    await _setup_tree(manager)
    await _add_subagent(manager)

    env = _envelope(AgentMessageType.TASK_REQUEST, message_id="m1", target_sid="inv.sub")

    await manager.deliver("inv.sub", env)
    dispatched = await manager._track_store.list_dispatched("t1")
    assert len(dispatched) == 1
    assert dispatched[0].track_id == "m1"

    await manager.deliver("inv.sub", env)
    dispatched_after = await manager._track_store.list_dispatched("t1")
    assert len(dispatched_after) <= 1

    track = await manager._track_store.get("m1")
    assert track is not None
    assert track.track_id == track.message_id == "m1"


# ---------------------------------------------------------------------------
# (l) eviction cancels tracks and tree if root
# ---------------------------------------------------------------------------


async def test_eviction_calls_tree_cleanup() -> None:
    manager, _ = _make_manager()
    await _setup_tree(manager)

    task_env = _envelope(
        AgentMessageType.TASK_REQUEST,
        message_id="m1",
        target_sid="root.main",
        parent_sid="",
    )
    await manager.deliver("root.main", task_env)
    await manager.on_dispatch_start("root.main")
    assert "root.main" in manager._running

    await manager.on_session_evicted("root.main")

    assert "root.main" not in manager._running
    assert "root.main" not in manager._pending_input

    track = await manager._track_store.get("m1")
    assert track is not None
    assert track.status == MessageTrackStatus.CANCELLED

    tree = await manager._tree_store.get("t1")
    assert tree is not None
    assert tree.status == SessionTreeStatus.CANCELLED


# ---------------------------------------------------------------------------
# F1 action item 4: empty-store → full lifecycle integration test
# ---------------------------------------------------------------------------


async def test_empty_stores_full_lifecycle() -> None:
    """Start from EMPTY stores → tree + nodes created on demand → quiesced.

    Verifies the _ensure_node creation path end-to-end:
    1. deliver EXTERNAL_INPUT to root → SessionTreeRecord + root node created
    2. deliver TASK_REQUEST to new subagent → child node created in same tree
    3. AGENT_RESULT → parent consume via bus.consume → is_quiesced True
    """
    manager, server = _make_manager()

    # 1. Root: EXTERNAL_INPUT creates tree + root node
    root_env = _envelope(
        AgentMessageType.EXTERNAL_INPUT,
        message_id="m_ext",
        target_sid="conv1.main",
        parent_sid=None,
    )
    await manager.deliver("conv1.main", root_env)
    tree = await manager._tree_store.get("conv1.main")
    assert tree is not None, "SessionTreeRecord must be created from EXTERNAL_INPUT"
    assert tree.root_node_session_id == "conv1.main"
    assert tree.status == SessionTreeStatus.ACTIVE
    root_node = await manager._node_store.get("conv1.main")
    assert root_node is not None, "root TreeNodeRecord must be created"
    assert root_node.tree_id == "conv1.main"
    assert root_node.parent_session_id is None
    assert "conv1.main" in manager._pending_input

    # Consume the external input → pending cleared
    await manager.on_consumed("conv1.main", root_env)
    assert "conv1.main" not in manager._pending_input
    assert await manager.is_quiesced("conv1.main") is True

    # 2. Child: TASK_REQUEST creates subagent node in parent's tree
    task_env = _envelope(
        AgentMessageType.TASK_REQUEST,
        message_id="m_task",
        target_sid="inv.sub",
        parent_sid="conv1.main",
    )
    await manager.deliver("inv.sub", task_env)
    child_node = await manager._node_store.get("inv.sub")
    assert child_node is not None, "child TreeNodeRecord must be created"
    assert child_node.tree_id == "conv1.main", "child must be in parent's tree"
    assert child_node.parent_session_id == "conv1.main"
    assert await manager.is_quiesced("conv1.main") is False, "dispatched track blocks quiesce"

    # Dispatch lifecycle
    await manager.on_dispatch_start("inv.sub")
    assert "inv.sub" in manager._running
    await manager.on_dispatch_end("inv.sub")
    assert "inv.sub" not in manager._running

    # 3. AGENT_RESULT → parent consume → quiesced
    result_env = _envelope(
        AgentMessageType.AGENT_RESULT,
        message_id="m_result",
        target_sid="conv1.main",
        parent_sid="conv1.main",
        invocation_id="inv1",
    )
    await manager.deliver("conv1.main", result_env)

    # Consume the result via the real bus → triggers on_consumed callback
    batch = await manager._bus.consume("conv1.main", limit=10)
    assert len(batch) > 0, "result envelope must be consumable from the inbox"

    assert await manager.is_quiesced("conv1.main") is True, "tree must be quiesced after result consumed"
    tree = await manager._tree_store.get("conv1.main")
    assert tree is not None
    assert tree.status == SessionTreeStatus.COMPLETED
