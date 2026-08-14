"""F3 verification: SessionTree real-persistence message flow against REAL Sqlite stores.

Verifies the full SessionTreeManager message lifecycle against real SQLite
persistence (SqliteSessionTreeStore + SqliteTreeNodeStore + SqliteMessageTrackStore
sharing one ConnectionManager + real workspace migration 001_initial.sql).
The inbox stays InMemoryInboxServer (the point is the TREE stores are Sqlite).

No bot startup; every operation is bounded and the whole test runs under 60s.
The temp state.db lives in the pytest ``tmp_path`` (auto-cleaned).

Steps verified:
  1. Real SQLITE persistence via ConnectionManager + RecordScope
  2. Real workspace migration applied (3 session_tree tables exist in the SQLite file)
  3. Real stack assembled: Sqlite stores + LocalAgentMessageBus + InboxConsumer + poller + manager
  4. Real message flow: deliver EXTERNAL_INPUT -> TASK_REQUEST -> dispatch lifecycle
     -> AGENT_RESULT -> consume -> quiesce + tree COMPLETED
  5. Crash recovery: second manager against SAME DB, recover_tree on clean tree,
     then stale RUNNING node + orphaned DISPATCHED track -> recovered to COMPLETED/CONSUMED
  6. PASS/FAIL summary with actual SQL row counts
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from modex_agent.core.scope import RecordScope
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
    SessionTreeStatus,
    TreeNodeRecord,
)
from modex_agent.multi_agent.session_tree.store_node import SqliteTreeNodeStore
from modex_agent.multi_agent.session_tree.store_track import SqliteMessageTrackStore
from modex_agent.multi_agent.session_tree.store_tree import SqliteSessionTreeStore
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.utils.time import now_ms

pytestmark = pytest.mark.integration

_SCOPE = RecordScope(workspace_id="f3-verify")


# ---------------------------------------------------------------------------
# Helpers (mirror the proven pattern from test_session_tree_integration.py
# but swap InMemory tree stores -> real Sqlite stores sharing one ConnectionManager)
# ---------------------------------------------------------------------------


class _StubPoller(InboxPoller):
    """Real InboxPoller subclass that needs no AgentPool.

    The manager only calls ``signal_wakeup``; this subclass provides it
    without the full pool wiring.
    """

    def __init__(self) -> None:
        self.signaled = False

    def signal_wakeup(self) -> None:
        self.signaled = True


def _make_manager(
    connection: ConnectionManager, scope: RecordScope
) -> tuple[SessionTreeManager, InMemoryInboxServer]:
    server = InMemoryInboxServer()
    producer = InboxProducer(server=server)
    consumer = InboxConsumer(server=server)
    bus = LocalAgentMessageBus(producer=producer, consumer=consumer)
    manager = SessionTreeManager(
        tree_store=SqliteSessionTreeStore(connection, scope),
        node_store=SqliteTreeNodeStore(connection, scope),
        track_store=SqliteMessageTrackStore(connection, scope),
        bus=bus,
        poller=_StubPoller(),
        pool_name="pool1",
        workspace_root="/tmp",
        session_registry=InMemorySessionRegistry(),
    )
    consumer.set_on_consumed(manager.on_consumed)
    return manager, server


def _envelope(
    msg_type: str,
    *,
    message_id: str,
    target_sid: str,
    parent_sid: str | None,
    invocation_id: str | None = None,
    target_name: str = "sub",
) -> AgentMessageEnvelope:
    return AgentMessageEnvelope(
        payload={"content": "test"},
        source=AgentAddress(kind=AddressKind.AGENT, name="main"),
        target=AgentAddress(kind=AddressKind.AGENT, name=target_name),
        message_type=msg_type,
        session_id="conv1",
        agent_session_id=target_sid,
        parent_session_id=parent_sid,
        invocation_id=invocation_id,
        message_id=message_id,
    )


async def _count(conn: ConnectionManager, table: str, owner_key: str) -> int:
    return await conn.query_value(
        f"SELECT COUNT(*) FROM {table} WHERE owner_scope_key = ?",
        int,
        (owner_key,),
    )


async def _count_tracks(
    conn: ConnectionManager, owner_key: str, status: str, message_type: str
) -> int:
    return await conn.query_value(
        "SELECT COUNT(*) FROM message_tracks "
        "WHERE owner_scope_key = ? AND status = ? AND message_type = ?",
        int,
        (owner_key, status, message_type),
    )


# ---------------------------------------------------------------------------
# Verification test — one atomic task, all steps in sequence
# ---------------------------------------------------------------------------


async def test_session_tree_sqlite_real_persistence_flow(tmp_path: Path) -> None:
    """F3: SessionTree real-SQLite persistence end-to-end message flow.

    All operations are local SQLite (no network, no bot startup). Wrapped in a
    60s timeout as a safety net; typical runtime is < 2s.
    """
    await asyncio.wait_for(_run_verification(tmp_path), timeout=60.0)


async def _run_verification(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    scope = _SCOPE
    owner_key = scope.canonical()

    root_sid = "conv1.main"
    sub_sid = "inv1.sub"
    tree_id = root_sid

    print("\n=== F3 VERIFICATION: SessionTree Real-SQLite Persistence ===")

    # --- Step 1+3: Setup real SQLite persistence + assemble stack ---
    conn1 = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
    await conn1.open()

    # --- Step 2: Verify 3 session_tree tables exist in the real SQLite file ---
    rows = await conn1.query_all(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('session_trees','tree_nodes','message_tracks') ORDER BY name"
    )
    table_names = {row["name"] for row in rows}
    assert table_names == {
        "session_trees",
        "tree_nodes",
        "message_tracks",
    }, f"Missing tables: {table_names}"
    print("\nSTEP 2: Workspace migration applied (001_initial.sql)")
    print(f"  DB: {db_path}")
    print(f"  Tables: {sorted(table_names)}")
    print("  PASS")

    # --- Step 3: Assemble real stack ---
    manager, _server = _make_manager(conn1, scope)
    assert isinstance(manager._tree_store, SqliteSessionTreeStore)
    assert isinstance(manager._node_store, SqliteTreeNodeStore)
    assert isinstance(manager._track_store, SqliteMessageTrackStore)
    print("\nSTEP 3: Real stack assembled")
    print("  Stores: SqliteSessionTreeStore + SqliteTreeNodeStore + SqliteMessageTrackStore")
    print("  Bus: LocalAgentMessageBus + InboxConsumer + InMemoryInboxServer")
    print("  Poller: _StubPoller (InboxPoller subclass)")
    print("  PASS")

    try:
        # --- Step 4a: deliver(root_sid, EXTERNAL_INPUT) ---
        root_env = _envelope(
            AgentMessageType.EXTERNAL_INPUT,
            message_id="m_ext",
            target_sid=root_sid,
            parent_sid=None,
            target_name="main",
            invocation_id=None,
        )
        await manager.deliver(root_sid, root_env)

        tree_count = await _count(conn1, "session_trees", owner_key)
        node_count = await _count(conn1, "tree_nodes", owner_key)
        track_count = await _count(conn1, "message_tracks", owner_key)
        assert tree_count == 1, f"Expected 1 session_tree, got {tree_count}"
        assert node_count == 1, f"Expected 1 tree_node, got {node_count}"
        assert track_count == 0, f"Expected 0 message_tracks, got {track_count}"
        print("\nSTEP 4a: deliver(root_sid, EXTERNAL_INPUT)")
        print(f"  session_trees rows: {tree_count}")
        print(f"  tree_nodes rows: {node_count}")
        print(f"  message_tracks rows: {track_count}")
        print("  PASS")

        # --- Step 4a.1: consume EXTERNAL_INPUT (real-flow: main agent picks up input) ---
        # Necessary so on_dispatch_end(sub_sid) sees is_quiesced=True -> tree COMPLETED.
        # In production the poller starts a root turn that consumes the inbox.
        batch = await manager._bus.consume(root_sid, limit=10)
        assert len(batch) == 1, f"Expected 1 consumed envelope, got {len(batch)}"
        assert root_sid not in manager._pending_input
        print("\nSTEP 4a.1: consume EXTERNAL_INPUT (clears pending_input)")
        print(f"  consumed: {len(batch)} envelope(s)")
        print("  pending_input: empty")
        print("  PASS")

        # --- Step 4b: deliver(sub_sid, TASK_REQUEST) ---
        task_env = _envelope(
            AgentMessageType.TASK_REQUEST,
            message_id="m_task",
            target_sid=sub_sid,
            parent_sid=root_sid,
            target_name="sub",
            invocation_id="inv1",
        )
        await manager.deliver(sub_sid, task_env)

        node_count = await _count(conn1, "tree_nodes", owner_key)
        track_count = await _count(conn1, "message_tracks", owner_key)
        task_dispatched = await _count_tracks(
            conn1, owner_key, "dispatched", "task_request"
        )
        assert node_count == 2, f"Expected 2 tree_nodes, got {node_count}"
        assert track_count == 1, f"Expected 1 message_track, got {track_count}"
        assert task_dispatched == 1, f"Expected 1 DISPATCHED TASK_REQUEST, got {task_dispatched}"
        print("\nSTEP 4b: deliver(sub_sid, TASK_REQUEST)")
        print(f"  tree_nodes rows: {node_count}")
        print(f"  message_tracks rows: {track_count}")
        print(f"  DISPATCHED TASK_REQUEST: {task_dispatched}")
        print("  PASS")

        # --- Step 4c: on_dispatch_start + on_dispatch_end (sub_sid) ---
        sub_node_before = await manager._node_store.get(sub_sid)
        assert sub_node_before is not None
        version_before = sub_node_before.version

        await manager.on_dispatch_start(sub_sid)
        sub_node_running = await manager._node_store.get(sub_sid)
        assert sub_node_running is not None
        assert sub_node_running.status == NodeVersionStatus.RUNNING
        assert sub_node_running.version == version_before + 1
        assert sub_sid in manager._running

        await manager.on_dispatch_end(sub_sid)
        sub_node_done = await manager._node_store.get(sub_sid)
        assert sub_node_done is not None
        assert sub_node_done.status == NodeVersionStatus.COMPLETED
        assert sub_sid not in manager._running

        task_track = await manager._track_store.get("m_task")
        assert task_track is not None
        assert task_track.status == MessageTrackStatus.CONSUMED

        tree = await manager._tree_store.get(tree_id)
        assert tree is not None
        assert tree.status == SessionTreeStatus.COMPLETED

        print("\nSTEP 4c: on_dispatch_start + on_dispatch_end (sub_sid)")
        print(f"  sub node version: {version_before} -> {sub_node_done.version}")
        print("  sub node status: RUNNING -> COMPLETED")
        print(f"  TASK_REQUEST track: {task_track.status.value}")
        print(f"  tree status: {tree.status.value}")
        print("  PASS")

        # --- Step 4d: deliver(root_sid, AGENT_RESULT) ---
        # invocation_id="inv1" matches the TASK_REQUEST's invocation_id.
        # NOTE: With the task's step ordering (on_dispatch_end in 4c BEFORE
        # deliver(AGENT_RESULT) in 4d), the TASK_REQUEST track is already
        # CONSUMED (closed by on_dispatch_end.close_tracks_for_session).
        # So deliver(AGENT_RESULT)'s list_dispatched loop finds no DISPATCHED
        # TASK_REQUEST to match — it creates the AGENT_RESULT track with
        # source_session_id="". In production, AGENT_RESULT is delivered
        # BEFORE on_dispatch_end (via SubagentAutoSendHook on FINALLY_GRAPH),
        # so the invocation_id matching path does fire there.
        result_env = _envelope(
            AgentMessageType.AGENT_RESULT,
            message_id="m_result",
            target_sid=root_sid,
            parent_sid=root_sid,
            target_name="main",
            invocation_id="inv1",
        )
        await manager.deliver(root_sid, result_env)

        task_track = await manager._track_store.get("m_task")
        assert task_track is not None
        assert task_track.status == MessageTrackStatus.CONSUMED

        result_track = await manager._track_store.get("m_result")
        assert result_track is not None
        assert result_track.status == MessageTrackStatus.DISPATCHED
        assert result_track.message_type == "agent_result"

        result_dispatched = await _count_tracks(
            conn1, owner_key, "dispatched", "agent_result"
        )
        assert result_dispatched == 1

        print("\nSTEP 4d: deliver(root_sid, AGENT_RESULT)")
        print(f"  TASK_REQUEST track: {task_track.status.value} (closed)")
        print(f"  AGENT_RESULT track: {result_track.status.value}")
        print(f"  DISPATCHED AGENT_RESULT count: {result_dispatched}")
        print("  PASS")

        # --- Step 4e: consume via bus (fires on_consumed) ---
        batch = await manager._bus.consume(root_sid, limit=10)
        assert len(batch) == 1, f"Expected 1 consumed AGENT_RESULT, got {len(batch)}"

        result_track = await manager._track_store.get("m_result")
        assert result_track is not None
        assert result_track.status == MessageTrackStatus.CONSUMED

        print("\nSTEP 4e: consume via bus (fires on_consumed)")
        print(f"  consumed: {len(batch)} envelope(s)")
        print(f"  AGENT_RESULT track: {result_track.status.value}")
        print("  PASS")

        # --- Step 4f: is_quiesced + tree status ---
        quiesced = await manager.is_quiesced(tree_id)
        assert quiesced is True, "Tree should be quiesced"

        tree = await manager._tree_store.get(tree_id)
        assert tree is not None
        assert tree.status == SessionTreeStatus.COMPLETED

        print("\nSTEP 4f: is_quiesced + tree status")
        print(f"  is_quiesced: {quiesced}")
        print(f"  tree status: {tree.status.value}")
        print("  PASS")

        # === Final row-count snapshot ===
        final_trees = await _count(conn1, "session_trees", owner_key)
        final_nodes = await _count(conn1, "tree_nodes", owner_key)
        final_tracks = await _count(conn1, "message_tracks", owner_key)
        print("\n  Final DB row counts:")
        print(f"    session_trees: {final_trees}")
        print(f"    tree_nodes: {final_nodes}")
        print(f"    message_tracks: {final_tracks}")

        # --- Step 5: Crash recovery ---
        print("\nSTEP 5: Crash recovery")

        # Close first connection (simulates process restart)
        await conn1.close()

        # Open second connection to SAME DB file (simulates restart)
        conn2 = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
        await conn2.open()
        manager2, _server2 = _make_manager(conn2, scope)

        try:
            # 5a: recover_tree on clean tree (no error)
            await manager2.recover_tree(tree_id)

            tree = await manager2._tree_store.get(tree_id)
            assert tree is not None
            assert tree.status == SessionTreeStatus.COMPLETED

            print("\n  5a: recover_tree on clean tree")
            print(f"    tree status: {tree.status.value}")
            print("    PASS")

            # 5b: manually insert stale RUNNING node + orphaned DISPATCHED track
            crash_sid = "crash.sub"
            crash_node = TreeNodeRecord(
                tree_id=tree_id,
                session_id=crash_sid,
                parent_session_id=root_sid,
                agent_name="crashsub",
                version=1,
                parent_version=0,
                status=NodeVersionStatus.RUNNING,
                created_at=now_ms(),
                updated_at=now_ms(),
            )
            crash_track = MessageTrack(
                track_id="crash_track",
                tree_id=tree_id,
                message_id="crash_msg",
                message_type=AgentMessageType.TASK_REQUEST.value,
                invocation_id="crash_inv",
                target_session_id=crash_sid,
                source_session_id=root_sid,
                status=MessageTrackStatus.DISPATCHED,
                dispatched_at=now_ms(),
            )
            await manager2._node_store.create(crash_node)
            await manager2._track_store.create(crash_track)

            # Verify stale state before recovery
            stale_node = await manager2._node_store.get(crash_sid)
            assert stale_node is not None
            assert stale_node.status == NodeVersionStatus.RUNNING
            stale_track = await manager2._track_store.get("crash_track")
            assert stale_track is not None
            assert stale_track.status == MessageTrackStatus.DISPATCHED

            # recover_tree fixes stale state
            await manager2.recover_tree(tree_id)

            fixed_node = await manager2._node_store.get(crash_sid)
            assert fixed_node is not None
            assert fixed_node.status == NodeVersionStatus.COMPLETED, (
                f"Expected COMPLETED, got {fixed_node.status}"
            )

            fixed_track = await manager2._track_store.get("crash_track")
            assert fixed_track is not None
            assert fixed_track.status == MessageTrackStatus.CONSUMED, (
                f"Expected CONSUMED, got {fixed_track.status}"
            )

            print("\n  5b: recover_tree on stale RUNNING node + orphaned DISPATCHED track")
            print(f"    stale node: RUNNING -> {fixed_node.status.value}")
            print(f"    orphaned track: DISPATCHED -> {fixed_track.status.value}")
            print("    PASS")

        finally:
            await conn2.close()

    finally:
        await conn1.close()

    print("\n=== ALL STEPS PASSED ===")
