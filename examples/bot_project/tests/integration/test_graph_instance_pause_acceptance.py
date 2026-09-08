"""Offline completion acceptance across the graph and native session lifecycles.

Real BotAgentNodeFactory compilation, SQLite graph stores, pool/poller/tree,
AgentPipeline, ReActTurnRunner, ReAct and GraphDeliverTool. Only the provider
is scripted; workspace lookup is a minimal resource boundary. Resident agents
use InMemoryContextManager, so restart covers session identity/receipts/tree
and graph persistence, not conversation-memory recovery or lazy assembly.
The HTTP-like waiter calls the public orchestrator API without an HTTP server.
Deliver source identity is deliberately not asserted (known inner-invocation
issue); delivery and END aggregation run with the current production contract.
"""

from __future__ import annotations

import asyncio
import socket
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from bot.graph.agent_node_factory import BotAgentNodeFactory
from bot.workspace.handle import WorkspaceResolverCell

from examples.bot_project.tests.integration.test_graph_scope_pause_drain import _Runtime
from modex_agent.core.agent import current_agent_context
from modex_agent.core.llm_request import LLMRequest
from modex_agent.core.llm_struct import FinishReason, RuntimeSafetyPolicy
from modex_agent.core.message import MessageRole
from modex_agent.core.provider import LLMProvider
from modex_agent.core.stream_events import Finish, LLMStreamEvent, TextDelta, ToolCallComplete
from modex_agent.memory.context import InMemoryContextManager
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.descriptor import AgentDescriptor
from modex_agent.multi_agent.factory import DefaultAgentFactory
from modex_agent.multi_agent.session_tree.models import NodeVersionStatus
from modex_agent.orchestration import GraphOrchestrator, SqliteCoordinatorFactory
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.pipeline.turn_context_config import wire_graph_turn_config
from modex_graph import (
    DefaultGraphState,
    EdgeSpec,
    GraphInstanceStatus,
    GraphNode,
    GraphOutput,
    GraphOutputAdapter,
    GraphOutputKind,
    GraphPayload,
    GraphSpec,
    InvocationStatus,
    NodeRegistry,
    NodeSpec,
    SchedulerKind,
    SqliteGraphInstanceStore,
    SqliteGraphIORecordStore,
    SqliteGraphSpecStore,
)

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def offline_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject_connection(*args: object, **kwargs: object) -> None:
        raise AssertionError("Network access is forbidden in offline acceptance")

    monkeypatch.setattr(socket, "create_connection", reject_connection)
    monkeypatch.setattr(socket, "getaddrinfo", reject_connection)
    monkeypatch.setattr(socket.socket, "connect", reject_connection)
    monkeypatch.setattr(socket.socket, "connect_ex", reject_connection)


class _Gate:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.cleanup_started = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.cleanup_finished = asyncio.Event()

    async def wait(self) -> None:
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cleanup_started.set()
            await self.cleanup_release.wait()
            self.cleanup_finished.set()
            raise


class _OfflineProvider(LLMProvider):
    """First calls block; replay delivers once, then returns a final response."""

    def __init__(self) -> None:
        super().__init__()
        self.block_new_sessions = True
        self.started: asyncio.Queue[str] = asyncio.Queue()
        self.gates: dict[str, _Gate] = {}
        self.requests: list[tuple[str, LLMRequest]] = []
        self.active: set[asyncio.Task[object]] = set()

    def get_default_model(self) -> str:
        return "offline-acceptance"

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        ctx = current_agent_context.get()
        assert ctx is not None
        sid = ctx.session.session_id
        self.requests.append((sid, request))
        task = asyncio.current_task()
        assert task is not None
        self.active.add(task)
        try:
            if self.block_new_sessions and sid not in self.gates:
                gate = self.gates[sid] = _Gate()
                self.started.put_nowait(sid)
                await gate.wait()
            user_index = max(
                i for i, message in enumerate(request.messages) if message.role == MessageRole.USER
            )
            content = str(request.messages[user_index].content)
            tool_results = [
                message for message in request.messages[user_index + 1:]
                if message.role == MessageRole.TOOL
            ]
            if not tool_results and any(
                tool["function"]["name"] == "deliver" for tool in request.tools
            ):
                yield ToolCallComplete(
                    call_id=f"deliver-{len(self.requests)}", tool_name="deliver",
                    arguments={
                        "target": GraphNode.END,
                        "content": "primary accepted" if "primary request" in content else "independent accepted",
                    },
                )
                yield Finish(finish_reason=FinishReason.TOOL_CALLS)
            else:
                yield TextDelta(text=f"finished: {content}")
                yield Finish(finish_reason=FinishReason.STOP)
        finally:
            self.active.discard(task)

    def release_all(self) -> None:
        self.block_new_sessions = False
        for gate in self.gates.values():
            gate.release.set()
            gate.cleanup_release.set()


class _Outputs(GraphOutputAdapter):
    def __init__(self) -> None:
        self.events: list[GraphOutput] = []

    async def emit(self, output: GraphOutput) -> None:
        self.events.append(output)


class _AcceptanceRuntime:
    def __init__(self, root: Path, connection: ConnectionManager | None) -> None:
        # Reuse only the sibling's storage/pool/tree assembly, never its stub
        # pipeline, precompiled node, GraphContext, or node.run entry point.
        self.sessions = _Runtime(root, connection)
        self.provider = _OfflineProvider()
        self.connection = sqlite3.connect(root / "graphs.db")
        self.instances = SqliteGraphInstanceStore(self.connection)
        self.specs = SqliteGraphSpecStore(self.connection)
        self.io = SqliteGraphIORecordStore(self.connection)
        self.outputs = _Outputs()
        resolver = WorkspaceResolverCell()
        resolver.set(SimpleNamespace(pools={"p": SimpleNamespace(  # type: ignore[arg-type]
            pool=self.sessions.pool, tree_manager=self.sessions.tree,
            session_binding_store=self.sessions.bindings,
        )}))
        registry = NodeRegistry()
        registry.register("agent", BotAgentNodeFactory(resolver))
        self.orchestrator = GraphOrchestrator(
            node_registry=registry, state_classes={"default": DefaultGraphState},
            spec_store=self.specs, instance_store=self.instances,
            coordinator_factory=SqliteCoordinatorFactory(self.connection),
            io_store=self.io, output_adapter=self.outputs,
        )

    async def initialize(self) -> None:
        await self.sessions.registry.load_all()
        factory = DefaultAgentFactory(
            default_llm_provider=self.provider, session_registry=self.sessions.registry,
            inbox_consumer=self.sessions.consumer,
        )
        for name in ("main", "child", "leaf"):
            descriptor = AgentDescriptor(
                address=AgentAddress(name=name), max_iterations=3,
                safety_policy=RuntimeSafetyPolicy(),
            )
            instance = await factory.create_agent(
                descriptor, broker=self.sessions.pool._broker,
                context_manager=InMemoryContextManager(base_system_prompt="Offline acceptance."),
                hooks=[],
            )
            assert instance.pipeline is not None
            wire_graph_turn_config(
                instance.pipeline._turn_runner.turn_context_builder,
                graph_context_resolver=self.orchestrator.get_graph_context,
                session_binding_store=self.sessions.bindings,
            )
            await self.sessions.pool.register_resident(descriptor, instance)
        self.sessions.pool.start_poller()

    async def create(self, content: str) -> int:
        spec_id = self.specs.save(GraphSpec(
            name="pause-acceptance", state_class="default", scheduler=SchedulerKind.PARALLEL,
            nodes=[NodeSpec(name="worker", node_type="agent", config={
                "agent": "main", "pool": "p", "knowledge": {"enabled": False},
            })],
            edges=[
                EdgeSpec(source=GraphNode.START, target="worker"),
                EdgeSpec(source="worker", target=GraphNode.END),
            ],
        ))
        return await self.orchestrator.create_instance(spec_id, user_input=GraphPayload(content=content))

    async def close(self) -> None:
        self.provider.release_all()
        try:
            await self.orchestrator.cleanup()
        finally:
            try:
                await self.sessions.pool.shutdown_all()
                await self.sessions.bus.close()
                await self.sessions.pool._broker.stop()
            finally:
                self.connection.close()


@pytest.fixture(params=["file", "sqlite"])
async def session_connection(
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
            await raw_connection.close()


@pytest.mark.parametrize("restart", [False, True], ids=["same-process", "rebuilt-stores"])
async def test_pause_drains_native_scope_then_resume_completes_original_instance(
    tmp_path: Path, session_connection: ConnectionManager | None, restart: bool,
) -> None:
    """Removing scope drain, the pause wait, or recovery restoration must fail."""
    runtime = _AcceptanceRuntime(tmp_path, session_connection)
    waiters: list[asyncio.Task[None]] = []
    executions: list[asyncio.Task[None]] = []
    try:
        await runtime.initialize()
        # Test-only deadline bounds failure; production completion remains the
        # orchestrator task and SessionTreeManager.wait_quiesce, not a new tracker.
        async with asyncio.timeout(15):
            gid = await runtime.create("primary request")
            execution = runtime.orchestrator.start_run(gid)
            executions.append(execution)
            root = await runtime.provider.started.get()
            original = await runtime.sessions.registry.get(root)
            assert original is not None
            initial = runtime.orchestrator.get_state(gid)
            node_ids = initial.metadata.node_id_map
            assert set(node_ids) == {GraphNode.START, "worker", GraphNode.END}
            assert initial.metadata.status == GraphInstanceStatus.RUNNING
            assert initial.nodes[node_ids["worker"]][0].status == InvocationStatus.RUNNING

            await runtime.sessions.send("active.child", "active child", root)
            assert await runtime.provider.started.get() == "active.child"
            owned_provider_tasks = set(runtime.provider.active)
            owned_poller_tasks = [runtime.sessions.poller._inflight[sid] for sid in (root, "active.child")]
            other_gid = await runtime.create("independent request")
            other_execution = runtime.orchestrator.start_run(other_gid)
            executions.append(other_execution)
            other_root = await runtime.provider.started.get()
            assert other_root != root
            other_initial = runtime.orchestrator.get_state(other_gid)
            other_task = runtime.sessions.poller._inflight[other_root]

            pause = asyncio.create_task(runtime.orchestrator.pause(gid))
            waiters.append(pause)
            root_gate = runtime.provider.gates[root]
            child_gate = runtime.provider.gates["active.child"]
            await root_gate.cleanup_started.wait()
            await child_gate.cleanup_started.wait()
            assert not pause.done(), "pause returned before native provider cleanup"
            assert not execution.done()
            assert runtime.orchestrator.get_state(gid).metadata.status == GraphInstanceStatus.PAUSING
            assert runtime.sessions.bindings.get(root).task_id == gid
            assert runtime.sessions.bindings.get("active.child").task_id == gid
            with pytest.raises(ValueError):
                runtime.orchestrator.start_resume(gid)

            # A disconnected HTTP waiter must not cancel the execution owner
            # or inject a second cancellation into native cleanup.
            pause.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pause
            pause = asyncio.create_task(runtime.orchestrator.pause(gid))
            waiters.append(pause)
            await runtime.sessions.send("queued.leaf", "queued descendant", "active.child")
            await runtime.sessions.poller._tick()
            assert not pause.done()
            assert "queued.leaf" not in {sid for sid, _ in runtime.provider.requests}
            root_gate.cleanup_release.set()
            child_gate.cleanup_release.set()
            await pause
            await execution

            paused = runtime.orchestrator.get_state(gid)
            assert paused.metadata.status == GraphInstanceStatus.PAUSED
            assert root_gate.cleanup_finished.is_set() and child_gate.cleanup_finished.is_set()
            assert all(task.done() for task in owned_provider_tasks | set(owned_poller_tasks))
            assert runtime.orchestrator.get_graph_context(gid) is None
            assert gid not in runtime.orchestrator._executions
            assert set(runtime.sessions.poller._inflight) == {other_root}
            assert runtime.sessions.tree._running == {other_root}
            for sid in (root, "active.child", "queued.leaf"):
                assert runtime.sessions.bindings.get(sid) is None
                assert await runtime.sessions.tree.is_session_paused(sid)
            for sid in (root, "active.child"):
                record = await runtime.sessions.nodes.get(sid)
                assert record is not None and record.status == NodeVersionStatus.CANCELLED
            assert paused.nodes[node_ids["worker"]][0].status == InvocationStatus.CANCELED
            assert paused.nodes[node_ids[GraphNode.END]] == []
            await runtime.sessions.tree.wait_quiesce(root)
            assert (await runtime.sessions.tree.pending_work(root)).pending
            assert len(await runtime.sessions.bus.peek("queued.leaf")) == 1
            await runtime.sessions.poller._tick()
            assert "queued.leaf" not in {sid for sid, _ in runtime.provider.requests}

            # The unrelated graph still owns the very same session task and
            # binding, then completes while the primary graph stays PAUSED.
            assert runtime.sessions.poller._inflight[other_root] is other_task
            assert not other_task.done() and not other_execution.done()
            assert runtime.sessions.bindings.get(other_root).task_id == other_gid
            assert runtime.orchestrator.get_state(other_gid) == other_initial
            assert not runtime.provider.gates[other_root].cleanup_started.is_set()
            runtime.provider.gates[other_root].release.set()
            await other_execution
            assert runtime.orchestrator.get_state(other_gid).metadata.status == GraphInstanceStatus.COMPLETED
            assert runtime.orchestrator.get_state(gid).metadata.status == GraphInstanceStatus.PAUSED
            other_io = runtime.io.get_latest_by_instance(other_gid)
            assert other_io is not None and other_io.output == [GraphPayload(content="independent accepted")]
            assert not runtime.provider.active
            assert not runtime.sessions.poller._inflight

            if restart:
                await runtime.close()
                if session_connection is not None:
                    await session_connection.close()
                    await session_connection.open()
                runtime = _AcceptanceRuntime(tmp_path, session_connection)
                runtime.provider.block_new_sessions = False
                await runtime.initialize()
                assert runtime.orchestrator.get_state(gid) == paused
                assert runtime.sessions.bindings.get(root) is None
                # Starting a rebuilt poller cannot run saved-only receipts or
                # a queued descendant before the graph node restores its bind.
                await runtime.sessions.poller._tick()
                await asyncio.sleep(0.05)
                assert runtime.provider.requests == []
                assert not runtime.sessions.poller._inflight
            else:
                runtime.provider.block_new_sessions = False

            before_resume = len(runtime.provider.requests)
            await runtime.orchestrator.resume(gid)
            completed = runtime.orchestrator.get_state(gid)
            assert completed.metadata.graph_instance_id == gid
            assert completed.metadata.status == GraphInstanceStatus.COMPLETED
            assert completed.metadata.version == paused.metadata.version + 1
            assert completed.metadata.node_id_map == node_ids
            worker_versions = completed.nodes[node_ids["worker"]]
            assert [v.status for v in worker_versions] == [
                InvocationStatus.COMPLETED, InvocationStatus.CANCELED,
            ]
            assert worker_versions[0].version == worker_versions[1].version + 1
            assert worker_versions[0].invocation_id != worker_versions[1].invocation_id
            assert len(completed.nodes[node_ids[GraphNode.START]]) == 1
            assert [v.status for v in completed.nodes[node_ids[GraphNode.END]]] == [
                InvocationStatus.COMPLETED,
            ]
            io_records = runtime.io.list_by_instance(gid)
            assert len(io_records) == 2
            assert io_records[0].output is None
            assert io_records[1].user_input == GraphPayload(content="primary request")
            assert io_records[1].output == [
                GraphPayload(content="primary accepted"),
            ]
            assert any(
                event.graph_instance_id == gid and event.kind == GraphOutputKind.COMPLETED
                for event in runtime.outputs.events
            )
            resumed_sessions = {sid for sid, _ in runtime.provider.requests[before_resume:]}
            assert resumed_sessions == {root, "active.child", "queued.leaf"}
            retained = await runtime.sessions.registry.get(root)
            assert retained is not None and retained.created_at == original.created_at
            assert retained.session_id == original.session_id
            for sid in resumed_sessions:
                assert runtime.sessions.bindings.get(sid) is None
                assert not (await runtime.sessions.tree.pending_work(sid)).pending
                assert await runtime.sessions.bus.peek(sid) == []
            assert not runtime.sessions.poller._inflight
            assert not runtime.sessions.tree._running
            assert not runtime.provider.active
            assert runtime.orchestrator.get_state(other_gid).metadata.status == GraphInstanceStatus.COMPLETED
    finally:
        runtime.provider.release_all()
        for waiter in waiters:
            waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
        # Reap the returned owner tasks even if a regressed pause returns early.
        await asyncio.gather(*executions, return_exceptions=True)
        await runtime.close()
