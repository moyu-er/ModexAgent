"""Graph commands exercise a real owner, persistence and blocked-node drain."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator

import pytest

from modex_agent.control.graph_control import (
    GraphControlService,
    InMemoryGraphEngineController,
    LiveGraphEngineController,
)
from modex_agent.control.types import ControlCommand, ControlScope
from modex_agent.control.types import ControlCommandType as Command
from modex_agent.orchestration import GraphOrchestrator, SqliteCoordinatorFactory
from modex_graph import (
    DefaultGraphState,
    DeliverConsumptionStatus,
    EdgeSpec,
    FunctionNodeFactory,
    GraphContext,
    GraphNode,
    GraphPayload,
    GraphRunControl,
    GraphSpec,
    NodeRegistry,
    NodeSpec,
    RoutingError,
    SqliteGraphInstanceStore,
    SqliteGraphSpecStore,
)
from modex_graph import (
    GraphInstanceStatus as Status,
)


class ControlGraph:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.store = SqliteGraphInstanceStore(self.conn)
        self.specs = SqliteGraphSpecStore(self.conn)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cleaning = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        registry = NodeRegistry()
        registry.register("function", FunctionNodeFactory({"work": self.work}))
        self.orch = GraphOrchestrator(
            node_registry=registry,
            state_classes={"default": DefaultGraphState},
            spec_store=self.specs,
            instance_store=self.store,
            coordinator_factory=SqliteCoordinatorFactory(self.conn),
        )
        self.service = GraphControlService(self.store, self.orch)
        self.spec_id = self.specs.save(
            GraphSpec(
                name="control",
                state_class="default",
                nodes=[
                    NodeSpec(name=name, node_type="function", config={"function": "work"})
                    for name in ("alpha", "beta")
                ],
                edges=[
                    EdgeSpec(source=GraphNode.START, target="alpha"),
                    EdgeSpec(source="alpha", target="beta"),
                    EdgeSpec(source="beta", target=GraphNode.END),
                ],
            )
        )

    async def work(self, ctx: GraphContext[DefaultGraphState]) -> GraphPayload:
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cleaning.set()
            await self.cleanup_release.wait()
            raise
        return GraphPayload(content="answer")

    async def create(self, status: Status = Status.PENDING) -> int:
        gid = await self.orch.create_instance(self.spec_id)
        self.store.update_status(gid, status)
        return gid

    async def start(self) -> tuple[int, asyncio.Task[None]]:
        gid = await self.create()
        task = self.orch.start_run(gid)
        await asyncio.wait_for(self.entered.wait(), 2)
        return gid, task

    def command(self, kind: Command, gid: int | None, **payload: object) -> ControlCommand:
        return ControlCommand(
            command_id="command",
            type=kind,
            scope=ControlScope(session_id="control-test", graph_instance_id=gid),
            payload=payload,
        )


@pytest.fixture
async def graph() -> AsyncIterator[ControlGraph]:
    graph = ControlGraph()
    try:
        yield graph
    finally:
        graph.release.set()
        graph.cleanup_release.set()
        await graph.orch.cleanup()
        graph.conn.close()


@pytest.mark.parametrize(
    "command,transition,terminal",
    [
        (Command.PAUSE_GRAPH, Status.PAUSING, Status.PAUSED),
        (Command.STOP_GRAPH, Status.STOPPING, Status.STOPPED),
    ],
)
async def test_control_waits_for_real_owner_drain(
    graph: ControlGraph,
    command: Command,
    transition: Status,
    terminal: Status,
) -> None:
    gid, run = await graph.start()
    request = asyncio.create_task(graph.service.handle(graph.command(command, gid)))
    await asyncio.wait_for(graph.cleaning.wait(), 2)
    assert graph.orch.get_state(gid).metadata.status is transition
    assert not request.done()
    graph.cleanup_release.set()
    await asyncio.gather(request, run)
    assert graph.orch.get_state(gid).metadata.status is terminal
    assert graph.orch.get_graph_context(gid) is None


async def test_control_resume_waits_for_graph_not_just_status_write(graph: ControlGraph) -> None:
    gid = await graph.create(Status.PAUSED)
    request = asyncio.create_task(graph.service.handle(graph.command(Command.RESUME_GRAPH, gid)))
    await asyncio.wait_for(graph.entered.wait(), 2)
    assert not request.done()
    graph.release.set()
    await request
    assert graph.orch.get_state(gid).metadata.status is Status.COMPLETED


@pytest.mark.parametrize("status", [s for s in Status if s is not Status.PAUSED])
async def test_resume_rejects_nonpaused_without_mutating_metadata(
    graph: ControlGraph, status: Status
) -> None:
    gid = await graph.create(status)
    before = graph.store.load(gid)
    with pytest.raises(ValueError, match="only PAUSED"):
        await graph.service.handle(graph.command(Command.RESUME_GRAPH, gid))
    assert graph.store.load(gid) == before


@pytest.mark.parametrize("command", [Command.PAUSE_GRAPH, Command.STOP_GRAPH])
@pytest.mark.parametrize(
    "status",
    [
        Status.RUNNING,
        Status.PAUSING,
        Status.STOPPING,
        Status.CRASHED,
        Status.COMPLETED,
        Status.FAILED,
        Status.PENDING,
    ],
)
async def test_control_cannot_fabricate_a_remote_or_terminal_owner(
    graph: ControlGraph,
    command: Command,
    status: Status,
) -> None:
    gid = await graph.create(status)
    before = graph.store.load(gid)
    with pytest.raises(ValueError):
        await graph.service.handle(graph.command(command, gid))
    assert graph.store.load(gid) == before


async def test_stop_paused_without_engine_is_terminal_and_idempotent(graph: ControlGraph) -> None:
    gid = await graph.create(Status.PAUSED)
    await graph.service.handle(graph.command(Command.STOP_GRAPH, gid))
    await graph.service.handle(graph.command(Command.STOP_GRAPH, gid))
    assert graph.orch.get_state(gid).metadata.status is Status.STOPPED


@pytest.mark.parametrize(
    "command",
    [Command.PAUSE_GRAPH, Command.STOP_GRAPH, Command.RESUME_GRAPH, Command.DELIVER_TO_NODE],
)
@pytest.mark.parametrize("gid", [None, 999999])
async def test_graph_commands_validate_identity(
    graph: ControlGraph, command: Command, gid: int | None
) -> None:
    with pytest.raises(ValueError):
        await graph.service.handle(graph.command(command, gid, node_name="alpha", content="x"))


async def test_non_graph_command_is_ignored(graph: ControlGraph) -> None:
    gid, run = await graph.start()
    before = graph.store.load(gid)
    await graph.service.handle(graph.command(Command.CANCEL_TURN, gid))
    assert not graph.cleaning.is_set()
    assert graph.store.load(gid) == before
    graph.release.set()
    await run


@pytest.mark.parametrize("status", [Status.RUNNING, Status.PAUSED, Status.PENDING])
@pytest.mark.parametrize("content", [None, "hello", 42, {"k": "v"}, [1, 2, 3]])
async def test_deliver_routes_real_records_without_changing_status(
    graph: ControlGraph,
    status: Status,
    content: object,
) -> None:
    gid = await graph.create(status)
    before = graph.store.load(gid)
    await graph.service.handle(
        graph.command(Command.DELIVER_TO_NODE, gid, node_name="alpha", content=content)
    )
    coordinator = graph.orch._lookup_coordinator(gid)
    assert coordinator is not None and before is not None
    node_id = before.node_id_map["alpha"]
    records = coordinator.collect_consumable_delivers(node_id, 0)
    assert len(records) == 1
    assert records[0].content == content
    assert records[0].node_id == node_id
    assert records[0].source_node_id == "__external__"
    assert records[0].source_invocation_id == 0
    assert records[0].status is DeliverConsumptionStatus.PENDING
    assert graph.store.load(gid) == before


async def test_deliver_order_and_node_isolation(graph: ControlGraph) -> None:
    gid = await graph.create()
    for name, value in [("alpha", 1), ("beta", 2), ("alpha", 3)]:
        await graph.service.handle(
            graph.command(Command.DELIVER_TO_NODE, gid, node_name=name, content=value)
        )
    ctx = graph.orch._lookup_coordinator(gid)
    metadata = graph.store.load(gid)
    assert ctx is not None and metadata is not None
    for name, expected in [("alpha", [1, 3]), ("beta", [2])]:
        assert [
            d.content for d in ctx.collect_consumable_delivers(metadata.node_id_map[name], 0)
        ] == expected


@pytest.mark.parametrize("payload", [{}, {"node_name": 123}])
async def test_deliver_requires_string_node_name(
    graph: ControlGraph, payload: dict[str, object]
) -> None:
    gid = await graph.create()
    with pytest.raises(ValueError, match="node_name"):
        await graph.service.handle(graph.command(Command.DELIVER_TO_NODE, gid, **payload))


async def test_deliver_unknown_node_raises_routing_error(graph: ControlGraph) -> None:
    gid = await graph.create()
    with pytest.raises(RoutingError):
        await graph.service.handle(
            graph.command(Command.DELIVER_TO_NODE, gid, node_name="missing", content="x")
        )


@pytest.mark.parametrize(
    "status",
    [
        Status.PAUSING,
        Status.STOPPING,
        Status.STOPPED,
        Status.CRASHED,
        Status.COMPLETED,
        Status.FAILED,
    ],
)
async def test_deliver_rejects_unavailable_status(graph: ControlGraph, status: Status) -> None:
    gid = await graph.create(status)
    with pytest.raises(ValueError):
        await graph.service.handle(
            graph.command(Command.DELIVER_TO_NODE, gid, node_name="alpha", content="x")
        )


async def test_deliver_wakes_only_matching_execution_after_persistence(graph: ControlGraph) -> None:
    gid, run = await graph.start()
    other = await graph.create(Status.PAUSED)
    ctx = graph.orch.get_graph_context(gid)
    assert ctx is not None
    wakeup = asyncio.Event()
    ctx.control.set_wakeup(wakeup)
    await graph.service.handle(
        graph.command(Command.DELIVER_TO_NODE, other, node_name="alpha", content="other")
    )
    assert not wakeup.is_set()
    await graph.service.handle(
        graph.command(Command.DELIVER_TO_NODE, gid, node_name="beta", content="wake")
    )
    assert wakeup.is_set()
    metadata = graph.store.load(gid)
    assert metadata is not None
    assert (
        ctx.coordinator.collect_consumable_delivers(metadata.node_id_map["beta"], 0)[0].content
        == "wake"
    )
    graph.release.set()
    await run


@pytest.mark.parametrize("stop", [False, True])
async def test_exported_live_controller_signals_control(stop: bool) -> None:
    control = GraphRunControl()
    wakeup = asyncio.Event()
    control.set_wakeup(wakeup)
    controller = LiveGraphEngineController(1, control)
    if stop:
        await controller.stop()
        assert control.stop_requested
    else:
        await controller.pause()
        assert control.pause_requested
    assert wakeup.is_set()


async def test_exported_recording_controller_retains_recording_contract() -> None:
    controller = InMemoryGraphEngineController(1)
    await controller.pause()
    await controller.stop()
    await controller.deliver_to_node("alpha", "x")
    assert controller.graph_instance_id == 1
    assert controller.pause_called and controller.stop_called
    assert controller.deliver_calls == [("alpha", "x")]
