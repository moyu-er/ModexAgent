"""Real node/pool/poller/tree cancellation; only pipeline execution is stubbed."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from bot.graph.agent_node import BotAgentNode
from bot.graph.knowledge_config import KnowledgeNodeConfig
from bot.workspace.handle import WorkspaceResolverCell

from modex_agent.agents.agent_node import SessionStrategy
from modex_agent.core.agent import AgentContext
from modex_agent.core.message import ChatMessage
from modex_agent.core.scope import RecordScope
from modex_agent.core.session_id import SessionInfo
from modex_agent.hook.builtin.inbox_flush import InboxFlushHook
from modex_agent.memory.context import InMemoryContextManager
from modex_agent.memory.history import ListMessageHistory
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.messaging.models import InputMessage
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.descriptor import AgentDescriptor, AgentInstance
from modex_agent.multi_agent.envelope import AgentMessageEnvelope
from modex_agent.multi_agent.factory import DefaultAgentFactory
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.inbox.server_local import LocalFileInboxMQ
from modex_agent.multi_agent.inbox_poller import InboxPoller
from modex_agent.multi_agent.message_type import AgentMessageType
from modex_agent.multi_agent.pool import AgentPool
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.session_tree.models import (
    MessageTrackStatus,
    NodeVersionStatus,
    SessionTreeMetadata,
    SessionTreeStatus,
)
from modex_agent.multi_agent.session_tree.session_binding import InMemorySessionBindingStore
from modex_agent.multi_agent.session_tree.store_node import (
    LocalFileTreeNodeStore,
    SqliteTreeNodeStore,
)
from modex_agent.multi_agent.session_tree.store_track import (
    LocalFileMessageTrackStore,
    SqliteMessageTrackStore,
)
from modex_agent.multi_agent.session_tree.store_tree import (
    LocalFileSessionTreeStore,
    SqliteSessionTreeStore,
)
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters.file_session_store import LocalFileSessionStore
from modex_agent.persistence.adapters.inbox_mq import SqliteInboxMQ
from modex_agent.persistence.adapters.session_store import SqliteSessionStore
from modex_agent.persistence.session_registry import InMemorySessionRegistry
from modex_agent.tools.manager import InMemoryToolManager
from modex_graph import (
    DefaultGraphState,
    Graph,
    GraphContext,
    GraphNode,
    GraphPayload,
    GraphRuntime,
)
from modex_graph.persistence import (
    InMemoryNodeStateStore,
    NullDeliverStoreFactory,
    NullGraphInstanceStore,
)
from modex_graph.persistence.persistence_coordinator import GraphPersistenceCoordinator


class _PipelineBoundary:
    def __init__(self, bindings: InMemorySessionBindingStore) -> None:
        self.bindings = bindings
        self.started: asyncio.Queue[InputMessage] = asyncio.Queue()
        self.calls: list[InputMessage] = []
        self.cancelled: list[str] = []
        self.cleanup_bindings: list[int | None] = []
        self.cleanup_started = asyncio.Event()
        self.cleanup_finished = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.cleanup_release.set()
        self.block = True
        self.release = asyncio.Event()
        self.agent = self
        self.on_message: Callable[[InputMessage], Awaitable[None]] | None = None

    async def process_message(self, message: InputMessage) -> None:
        self.calls.append(message)
        self.started.put_nowait(message)
        try:
            if self.on_message is not None:
                await self.on_message(message)
            if self.block:
                await self.release.wait()
        except asyncio.CancelledError:
            sid = message.session.session_id
            self.cancelled.append(sid)
            binding = self.bindings.get(sid)
            self.cleanup_bindings.append(binding.task_id if binding else None)
            self.cleanup_started.set()
            await self.cleanup_release.wait()
            self.cleanup_finished.set()
            # Native runners may translate task cancellation to a normal return.
            return

    async def stop(self) -> None:
        pass


@pytest.fixture(params=["file", "sqlite"])
async def backend_connection(
    request: pytest.FixtureRequest, tmp_path: Path,
) -> AsyncIterator[ConnectionManager | None]:
    if request.param == "file":
        yield None
        return
    connection = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await connection.open()
    try:
        yield connection
    finally:
        raw_connection = connection._require_connection()
        try:
            await connection.close()
        finally:
            # Preserve checkpoint failures while still reaping the test's DB
            # worker; otherwise a failed teardown keeps the test process alive.
            await raw_connection.close()


class _Runtime:
    def __init__(
        self, root: Path, connection: ConnectionManager | None,
        strategy: SessionStrategy = SessionStrategy.CACHED,
    ) -> None:
        scope = RecordScope(workspace_id="graph-pause-tests")
        self.registry = InMemorySessionRegistry(
            LocalFileSessionStore(root / "sessions") if connection is None else SqliteSessionStore(connection)
        )
        self.bindings = InMemorySessionBindingStore()
        self.inbox = (
            LocalFileInboxMQ(root / "inbox") if connection is None
            else SqliteInboxMQ(root / "state.db", scope, connection=connection)
        )
        consumer = InboxConsumer(self.inbox)
        self.consumer = consumer
        self.bus = LocalAgentMessageBus(InboxProducer(self.inbox), consumer)
        self.pool = AgentPool(
            InMemoryMessageBroker(), DefaultAgentFactory(), self.bus, consumer,
            session_registry=self.registry,
        )
        self.poller = InboxPoller(self.pool, interval=0.01)
        self.pool.attach_poller(self.poller)
        self.bus.set_poller(self.poller)
        self.trees = (
            LocalFileSessionTreeStore(root / "trees") if connection is None
            else SqliteSessionTreeStore(connection, scope)
        )
        self.nodes = (
            LocalFileTreeNodeStore(root / "nodes") if connection is None
            else SqliteTreeNodeStore(connection, scope)
        )
        self.tracks = (
            LocalFileMessageTrackStore(root / "tracks") if connection is None
            else SqliteMessageTrackStore(connection, scope)
        )
        self.tree = SessionTreeManager(
            self.trees, self.nodes, self.tracks, self.bus, self.poller,
            "p", str(root), self.registry, self.bindings,
        )
        self.pool.tree = self.tree
        self.poller.attach_tree_manager(self.tree)
        consumer.set_on_consumed(self.tree.on_consumed)
        self.pipeline = _PipelineBoundary(self.bindings)
        resolver = WorkspaceResolverCell()
        resolver.set(SimpleNamespace(pools={"p": SimpleNamespace(  # type: ignore[arg-type]
            pool=self.pool, tree_manager=self.tree, session_binding_store=self.bindings,
        )}))
        self.node = BotAgentNode(
            "main", "p", resolver, session_strategy=strategy,
            knowledge_config=KnowledgeNodeConfig(enabled=False),
        )
        graph = Graph("pause-scope")
        graph.add_node("worker", self.node)
        graph.add_edge(GraphNode.START, "worker")
        graph.add_edge("worker", GraphNode.END)
        self.graph = graph.compile()
        self.ctx = GraphContext(
            state=DefaultGraphState(), runtime=GraphRuntime(), graph_instance_id=71,
            user_input=GraphPayload(content="original"),
            coordinator=GraphPersistenceCoordinator(
                graph_instance_id=71, instance_store=NullGraphInstanceStore(),
                node_state_store=InMemoryNodeStateStore(71),
                default_deliver_store_factory=NullDeliverStoreFactory(),
            ),
        )
        for node in self.graph.nodes.values():
            self.ctx.coordinator.register_node(node.node_id)

    async def initialize(self) -> None:
        await self.registry.load_all()
        for name in ("main", "child", "leaf"):
            descriptor = AgentDescriptor(address=AgentAddress(name=name))
            await self.pool.register_resident(descriptor, AgentInstance(
                descriptor, InMemoryContextManager(), self.pipeline,  # type: ignore[arg-type]
            ))

    def execute(self) -> asyncio.Task[None]:
        return asyncio.create_task(self.node.run(self.ctx, graph=self.graph))

    async def send(self, sid: str, content: str, parent: str | None = None) -> None:
        await self.registry.register(SessionInfo.from_str(sid).model_copy(
            update={"parent_session_id": parent},
        ))
        await self.tree.deliver(sid, AgentMessageEnvelope(
            payload={"content": content}, source=AgentAddress(name="sender"),
            target=AgentAddress(name=SessionInfo.from_str(sid).agent_name),
            agent_session_id=sid, parent_session_id=parent,
            invocation_id=SessionInfo.from_str(sid).session_id_prefix if parent else None,
            message_type=AgentMessageType.EXTERNAL_INPUT,
        ))


async def test_graph_outer_cancel_drains_descendants_without_touching_other_tree(
    tmp_path: Path, backend_connection: ConnectionManager | None,
) -> None:
    runtime = _Runtime(tmp_path, backend_connection)
    await runtime.initialize()
    runtime.pool.start_poller()
    outer = runtime.execute()
    try:
        first = await asyncio.wait_for(runtime.pipeline.started.get(), 3)
        root = first.session.session_id
        await runtime.send("sub.child", "child", root)
        await asyncio.wait_for(runtime.pipeline.started.get(), 3)
        await runtime.send("deep.leaf", "leaf", "sub.child")
        await asyncio.wait_for(runtime.pipeline.started.get(), 3)
        await runtime.send("other.main", "unrelated")
        await asyncio.wait_for(runtime.pipeline.started.get(), 3)

        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(outer, 3)

        assert set(runtime.pipeline.cancelled) == {root, "sub.child", "deep.leaf"}
        assert runtime.pipeline.cleanup_bindings == [71, 71, 71]
        assert set(runtime.poller._inflight) == {"other.main"}
        assert runtime.bindings.get(root) is None
        for sid in (root, "sub.child", "deep.leaf"):
            node = await runtime.nodes.get(sid)
            assert node is not None and node.status == NodeVersionStatus.CANCELLED
        record = await runtime.trees.get(root)
        assert record is not None
        assert record.status == SessionTreeStatus.ACTIVE
        assert await runtime.tree.is_session_paused(root)
        assert record.completed_at is None
        await runtime.tree.wait_quiesce(root)

        await runtime.send(root, "late")
        await runtime.send("late.child", "late descendant", root)
        await runtime.poller._tick()
        assert len(runtime.pipeline.calls) == 4
        assert "other.main" in runtime.poller._inflight
    finally:
        runtime.pipeline.release.set()
        if outer.done() and not outer.cancelled():
            outer.result()
        outer.cancel()
        await asyncio.gather(outer, return_exceptions=True)
        await runtime.pool.shutdown_all()


async def test_graph_resume_reuses_cached_session_and_preserves_consumed_batch(
    tmp_path: Path, backend_connection: ConnectionManager | None,
) -> None:
    runtime = _Runtime(tmp_path, backend_connection)
    await runtime.initialize()
    await runtime.send("batch.main", "first")
    await runtime.send("batch.main", "second")
    await runtime.send("batch.main", "third")
    runtime.pool.start_poller()
    try:
        await asyncio.wait_for(runtime.pipeline.started.get(), 3)
        await runtime.tree.pause_session("batch.main")
        assert len(runtime.pipeline.calls) == 1
        assert not runtime.poller._inflight
        runtime.pipeline.block = False
        await runtime.tree.resume_session("batch.main")
        await asyncio.wait_for(runtime.tree.wait_quiesce("batch.main"), 3)
        assert [m.content for m in runtime.pipeline.calls] == ["first", "first", "second", "third"]
    finally:
        runtime.pipeline.release.set()
        await runtime.pool.shutdown_all()


async def test_graph_resume_queued_task_request_does_not_reinject_acknowledged_root(
    tmp_path: Path, backend_connection: ConnectionManager | None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime(tmp_path, backend_connection)
    await runtime.initialize()
    runtime.pipeline.block = False
    root_delivered = asyncio.Event()
    deliver = runtime.tree.deliver
    child_sid = "queued.child"

    async def observe_deliver(
        sid: str, envelope: AgentMessageEnvelope, *, track_consume: bool = False,
    ) -> None:
        await deliver(sid, envelope, track_consume=track_consume)
        if envelope.message_type == AgentMessageType.EXTERNAL_INPUT:
            root_delivered.set()

    async def delegate(message: InputMessage) -> None:
        if message.session.agent_name == "main":
            await runtime.tree.deliver(child_sid, AgentMessageEnvelope(
                payload={"content": "child task"}, source=AgentAddress(name="main"),
                target=AgentAddress(name="child"), agent_session_id=child_sid,
                parent_session_id=message.session.session_id, invocation_id="queued",
                message_type=AgentMessageType.TASK_REQUEST,
            ))

    monkeypatch.setattr(runtime.tree, "deliver", observe_deliver)
    runtime.pipeline.on_message = delegate
    outer = runtime.execute()
    try:
        await asyncio.wait_for(root_delivered.wait(), 3)
        # Drive one real poller tick so the root finishes before child admission.
        await runtime.poller._tick()
        root_sid, root_turn = next(iter(runtime.poller._inflight.items()))
        await asyncio.wait_for(root_turn, 3)
        root = await runtime.nodes.get(root_sid)
        assert root is not None and root.status == NodeVersionStatus.COMPLETED
        assert not (await runtime.tree.pending_work(root_sid)).pending
        assert not (await runtime.tree.pending_work(child_sid)).pending
        assert await runtime.inbox.count(child_sid) == 1
        tracks = await runtime.tracks.list_dispatched(root_sid)
        assert len(tracks) == 1 and tracks[0].message_type == AgentMessageType.TASK_REQUEST
        assert not await runtime.tree.is_quiesced(root_sid)
        assert not outer.done()

        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(outer, 3)
        assert await runtime.tree.is_quiesced(root_sid)
        assert not runtime.poller._inflight

        runtime.pool.start_poller()
        await asyncio.wait_for(runtime.execute(), 3)
        assert [(message.session.session_id, message.content) for message in runtime.pipeline.calls] == [
            (root_sid, "[Origin Request]:\noriginal"),
            (child_sid, "child task"),
        ]
        assert not runtime.poller._inflight
    finally:
        outer.cancel()
        await asyncio.gather(outer, return_exceptions=True)
        await runtime.pool.shutdown_all()


@pytest.mark.parametrize("strategy", [SessionStrategy.CACHED, SessionStrategy.PER_INVOCATION])
@pytest.mark.parametrize("pause_flag", [True, False])
async def test_graph_restart_holds_queued_work_until_real_node_reentry(
    tmp_path: Path, backend_connection: ConnectionManager | None, strategy: SessionStrategy,
    pause_flag: bool,
) -> None:
    first = _Runtime(tmp_path, backend_connection, strategy)
    await first.initialize()
    first.pool.start_poller()
    outer = first.execute()
    try:
        message = await asyncio.wait_for(first.pipeline.started.get(), 3)
        sid = message.session.session_id
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer
        await first.send(sid, "persisted queued")
        await first.registry.register(SessionInfo.from_str(sid).model_copy(update={
            "metadata": {SessionTreeMetadata.PAUSED: pause_flag},
        }))
    finally:
        first.pipeline.release.set()
        if outer.done() and not outer.cancelled():
            outer.result()
        outer.cancel()
        await asyncio.gather(outer, return_exceptions=True)
        await first.pool.shutdown_all()

    if backend_connection is not None:
        await backend_connection.close()
        await backend_connection.open()
    second = _Runtime(tmp_path, backend_connection, strategy)
    second.node.node_id = first.node.node_id
    await second.initialize()
    second.pipeline.block = False
    second.pool.start_poller()
    try:
        await second.poller._tick()
        await asyncio.sleep(0.05)
        assert second.pipeline.calls == []
        resumed = second.execute()
        await asyncio.wait_for(resumed, 3)
        assert {m.session.session_id for m in second.pipeline.calls} == {sid}
        assert [m.content for m in second.pipeline.calls] == ["[Origin Request]:\noriginal", "persisted queued"]
        assert not second.poller._inflight
    finally:
        await second.pool.shutdown_all()


async def test_graph_pause_admission_stays_closed_through_cleanup_and_live_resume(
    tmp_path: Path, backend_connection: ConnectionManager | None,
) -> None:
    runtime = _Runtime(tmp_path, backend_connection)
    await runtime.initialize()
    runtime.pool.start_poller()
    outer = runtime.execute()
    try:
        original = await asyncio.wait_for(runtime.pipeline.started.get(), 3)
        sid = original.session.session_id
        runtime.pipeline.cleanup_release.clear()
        outer.cancel()
        await asyncio.wait_for(runtime.pipeline.cleanup_started.wait(), 3)
        assert not outer.done()
        assert runtime.bindings.get(sid) is not None
        with pytest.raises(RuntimeError, match="drain"):
            await runtime.tree.resume_session(sid)
        await runtime.send("new.child", "late child", sid)
        await runtime.send(sid, "late parent")
        await runtime.send("independent.main", "unrelated")
        unrelated = await asyncio.wait_for(runtime.pipeline.started.get(), 3)
        assert unrelated.session.session_id == "independent.main"
        assert len(runtime.pipeline.calls) == 2
        runtime.pipeline.cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(outer, 3)
        assert set(runtime.poller._inflight) == {"independent.main"}

        runtime.pipeline.block = False
        resumed = runtime.execute()
        await asyncio.wait_for(resumed, 3)
        root_inputs = [m.content for m in runtime.pipeline.calls if m.session.session_id == sid]
        assert root_inputs == ["[Origin Request]:\noriginal", "[Origin Request]:\noriginal", "late parent"]
        assert len([m for m in runtime.pipeline.calls if m.session.session_id == "new.child"]) == 1
        assert runtime.bindings.get(sid) is None
    finally:
        runtime.pipeline.cleanup_release.set()
        runtime.pipeline.release.set()
        outer.cancel()
        await asyncio.gather(outer, return_exceptions=True)
        await runtime.pool.shutdown_all()


async def test_graph_cancel_during_dispatch_finalization_retains_owner_until_drain(
    tmp_path: Path, backend_connection: ConnectionManager | None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime(tmp_path, backend_connection)
    await runtime.initialize()
    runtime.pipeline.block = False
    entered = asyncio.Event()
    release = asyncio.Event()
    close_tracks = runtime.tracks.close_tracks_for_session
    finalizations: list[MessageTrackStatus] = []

    async def blocked_close(sid: str, status: MessageTrackStatus) -> None:
        finalizations.append(status)
        entered.set()
        await release.wait()
        await close_tracks(sid, status)

    monkeypatch.setattr(runtime.tracks, "close_tracks_for_session", blocked_close)
    runtime.pool.start_poller()
    outer = runtime.execute()
    try:
        await asyncio.wait_for(entered.wait(), 3)
        sid = runtime.pipeline.calls[0].session.session_id
        outer.cancel()
        await asyncio.sleep(0.03)
        assert sid in runtime.poller._inflight
        assert runtime.bindings.get(sid) is not None
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(outer, 3)
        assert sid not in runtime.tree._running
        assert not runtime.poller._inflight
        await runtime.tree.wait_quiesce(sid)
        assert len(finalizations) == 1
    finally:
        release.set()
        outer.cancel()
        await asyncio.gather(outer, return_exceptions=True)
        await runtime.pool.shutdown_all()


async def test_graph_cancelled_foldin_append_retains_result_for_reentry(
    tmp_path: Path, backend_connection: ConnectionManager | None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime(tmp_path, backend_connection)
    await runtime.initialize()
    history = ListMessageHistory([])
    hook = InboxFlushHook(runtime.consumer, "main")
    ready = asyncio.Event()
    append_entered = asyncio.Event()
    append_release = asyncio.Event()
    append = history.append

    async def blocked_append(message: ChatMessage | dict[str, object]) -> None:
        if "late result" in str(message):
            append_entered.set()
            await append_release.wait()
        await append(message)

    async def fold(message: InputMessage) -> None:
        await ready.wait()
        await hook.before_iteration(AgentContext(
            system_prompt="", history=history, tool_manager=InMemoryToolManager(), session=message.session,
        ))

    monkeypatch.setattr(history, "append", blocked_append)
    runtime.pipeline.on_message = fold
    runtime.pool.start_poller()
    outer = runtime.execute()
    try:
        original = await asyncio.wait_for(runtime.pipeline.started.get(), 3)
        sid = original.session.session_id
        await runtime.tree.deliver(sid, AgentMessageEnvelope(
            payload={"content": "folded result"}, source=AgentAddress(name="child"),
            target=AgentAddress(name="main"), agent_session_id=sid,
            message_type=AgentMessageType.AGENT_RESULT,
        ))
        await runtime.tree.deliver(sid, AgentMessageEnvelope(
            payload={"content": "late result"}, source=AgentAddress(name="child"),
            target=AgentAddress(name="main"), agent_session_id=sid,
            message_type=AgentMessageType.AGENT_RESULT,
        ))
        ready.set()
        await asyncio.wait_for(append_entered.wait(), 3)
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(outer, 3)
        recorded = await history.to_list()
        assert len(recorded) == 1
        assert "folded result" in str(recorded[0].content)
        assert len((await runtime.tree.pending_work(sid)).pending) == 2
        runtime.pipeline.on_message = None
        runtime.pipeline.block = False
        await asyncio.wait_for(runtime.execute(), 3)
        assert [m.content for m in runtime.pipeline.calls].count("late result") == 1
        assert "folded result" not in [m.content for m in runtime.pipeline.calls]
        assert not (await runtime.tree.pending_work(sid)).pending
    finally:
        append_release.set()
        ready.set()
        runtime.pipeline.release.set()
        outer.cancel()
        await asyncio.gather(outer, return_exceptions=True)
        await runtime.pool.shutdown_all()


async def test_graph_saved_only_lazy_child_restores_materialization_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.bot_project.tests.integration.test_botagentnode_lazy_leaf import (
        _PoolBuild,
        _ScriptedProvider,
    )

    monkeypatch.setattr("modex_agent.plugins.defaults.hooks.resolve_modexctl_bin_dir", lambda: tmp_path)
    build = await _PoolBuild.build(tmp_path, _ScriptedProvider(), WorkspaceResolverCell())
    boundary = _PipelineBoundary(build.binding_store)
    created: list[tuple[str, str]] = []

    async def on_created(sid: str, parent: str) -> None:
        created.append((sid, parent))

    deps = build.pool.materialize_deps
    assert deps is not None
    deps.on_subagent_created = on_created
    create_agent = build.factory.create_agent

    async def create_with_boundary(*args, **kwargs):
        instance = await create_agent(*args, **kwargs)
        assert instance.pipeline is not None
        instance.pipeline.process_message = boundary.process_message
        return instance

    monkeypatch.setattr(build.factory, "create_agent", create_with_boundary)
    parent_sid = "owner.reviewer"
    child_sid = "saved.office-expert"
    try:
        await build.session_registry.register(SessionInfo.from_str(parent_sid))
        await build.tree_manager.deliver(child_sid, AgentMessageEnvelope(
            payload={"content": "unfinished child"}, source=AgentAddress(name="reviewer"),
            target=AgentAddress(name="office-expert"), agent_session_id=child_sid,
            parent_session_id=parent_sid, invocation_id="saved",
            message_type=AgentMessageType.TASK_REQUEST,
        ))
        build.pool.start_poller()
        await asyncio.wait_for(boundary.started.get(), 3)
        assert created == [(child_sid, parent_sid)]
        await build.tree_manager.pause_session(child_sid)
        assert await build.inbox_server.count(child_sid) == 0
        assert (await build.tree_manager.pending_work(child_sid)).pending
        await build.pool._shutdown_agent("office-expert")
        created.clear()
        boundary.block = False
        await build.tree_manager.resume_session(parent_sid)
        await asyncio.wait_for(build.tree_manager.wait_quiesce(parent_sid), 3)
        assert created == [(child_sid, parent_sid)]
        assert len(boundary.calls) == 2
    finally:
        boundary.release.set()
        await build.close()


async def test_graph_receipts_keep_poller_batch_out_of_nested_foldin(
    tmp_path: Path, backend_connection: ConnectionManager | None,
) -> None:
    runtime = _Runtime(tmp_path, backend_connection)
    await runtime.initialize()
    history = ListMessageHistory([])
    hook = InboxFlushHook(runtime.consumer, "main")
    sid = "held.main"

    async def deliver(content: str) -> None:
        await runtime.tree.deliver(sid, AgentMessageEnvelope(
            payload={"content": content}, source=AgentAddress(name="sender"),
            target=AgentAddress(name="main"), agent_session_id=sid,
            message_type=AgentMessageType.AGENT_MESSAGE,
        ))

    async def fold(message: InputMessage) -> None:
        if message.content == "first held":
            await deliver("late fold")
        await hook.before_iteration(AgentContext(
            system_prompt="", history=history, tool_manager=InMemoryToolManager(), session=message.session,
        ))

    runtime.pipeline.on_message = fold
    runtime.pipeline.block = False
    try:
        await deliver("first held")
        await deliver("second held")
        runtime.pool.start_poller()
        await asyncio.wait_for(runtime.tree.wait_quiesce(sid), 3)
        assert [message.content for message in runtime.pipeline.calls] == ["first held", "second held"]
        recorded = await history.to_list()
        assert len(recorded) == 1
        assert "late fold" in str(recorded[0].content)
        assert not (await runtime.tree.pending_work(sid)).pending
        assert await runtime.bus.peek(sid) == []
    finally:
        await runtime.pool.shutdown_all()


async def test_graph_direct_tree_pause_does_not_complete_waiting_node(
    tmp_path: Path, backend_connection: ConnectionManager | None,
) -> None:
    runtime = _Runtime(tmp_path, backend_connection)
    await runtime.initialize()
    runtime.pool.start_poller()
    outer = runtime.execute()
    try:
        original = await asyncio.wait_for(runtime.pipeline.started.get(), 3)
        await runtime.tree.pause_session(original.session.session_id)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(outer, 3)
    finally:
        runtime.pipeline.release.set()
        outer.cancel()
        await asyncio.gather(outer, return_exceptions=True)
        await runtime.pool.shutdown_all()


async def test_graph_crash_owner_cannot_be_reopened_by_late_graph_envelope(
    tmp_path: Path, backend_connection: ConnectionManager | None,
) -> None:
    first = _Runtime(tmp_path, backend_connection)
    await first.initialize()
    first.pool.start_poller()
    outer = first.execute()
    try:
        original = await asyncio.wait_for(first.pipeline.started.get(), 3)
        sid = original.session.session_id
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer
        # Crash can leave ACTIVE persisted; ownership must gate independently.
        await first.trees.update_status(sid, SessionTreeStatus.ACTIVE)
        await first.registry.register(SessionInfo.from_str(sid).model_copy(update={
            "metadata": {SessionTreeMetadata.PAUSED: False},
        }))
    finally:
        first.pipeline.release.set()
        outer.cancel()
        await asyncio.gather(outer, return_exceptions=True)
        await first.pool.shutdown_all()

    second = _Runtime(tmp_path, backend_connection)
    await second.initialize()
    second.pipeline.block = False
    try:
        await second.tree.recover_tree(sid)
        await second.tree.deliver(sid, AgentMessageEnvelope(
            payload={"content": "late graph reply"}, source=AgentAddress(name="child"),
            target=AgentAddress(name="main"), agent_session_id=sid,
            metadata={"graph_instance_id": 71},
        ))
        second.pool.start_poller()
        await second.poller._tick()
        await asyncio.sleep(0.05)
        assert second.pipeline.calls == []
        with pytest.raises(RuntimeError, match="binding"):
            await second.tree.resume_session(sid)
    finally:
        await second.pool.shutdown_all()


async def test_graph_second_outer_cancel_cannot_interrupt_owned_cleanup(
    tmp_path: Path, backend_connection: ConnectionManager | None,
) -> None:
    runtime = _Runtime(tmp_path, backend_connection)
    await runtime.initialize()
    runtime.pool.start_poller()
    outer = runtime.execute()
    try:
        original = await asyncio.wait_for(runtime.pipeline.started.get(), 3)
        runtime.pipeline.cleanup_release.clear()
        outer.cancel()
        await asyncio.wait_for(runtime.pipeline.cleanup_started.wait(), 3)
        outer.cancel()
        await asyncio.sleep(0.03)
        assert not outer.done()
        assert runtime.bindings.get(original.session.session_id) is not None
        runtime.pipeline.cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(outer, 3)
        assert runtime.pipeline.cleanup_finished.is_set()
        assert not runtime.poller._inflight
    finally:
        runtime.pipeline.cleanup_release.set()
        runtime.pipeline.release.set()
        outer.cancel()
        await asyncio.gather(outer, return_exceptions=True)
        await runtime.pool.shutdown_all()


async def test_graph_paused_child_work_survives_pool_retention(
    tmp_path: Path, backend_connection: ConnectionManager | None,
) -> None:
    runtime = _Runtime(tmp_path, backend_connection)
    await runtime.initialize()
    runtime.pool.start_poller()
    outer = runtime.execute()
    try:
        original = await asyncio.wait_for(runtime.pipeline.started.get(), 3)
        await runtime.send("retained.child", "unfinished child", original.session.session_id)
        await asyncio.wait_for(runtime.pipeline.started.get(), 3)
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer
        runtime.pool._retention.max_sessions_per_subagent = 0
        # Exercise the same policy operation invoked by the pool's cleanup tick.
        await runtime.pool._enforce_session_cap("child")
        await asyncio.sleep(0.05)
        assert (await runtime.tree.pending_work("retained.child")).pending
        assert await runtime.registry.get("retained.child") is not None
    finally:
        runtime.pipeline.release.set()
        outer.cancel()
        await asyncio.gather(outer, return_exceptions=True)
        await runtime.pool.shutdown_all()
