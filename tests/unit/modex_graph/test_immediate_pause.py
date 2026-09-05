"""Real scheduler pause/drain and same-instance recovery contracts."""

from __future__ import annotations

import asyncio

import pytest

from modex_graph import (
    CompiledGraph,
    DefaultGraphState,
    DeliverConsumptionStatus,
    Graph,
    GraphContext,
    GraphDrained,
    GraphEngine,
    GraphInstanceStatus,
    GraphInterrupt,
    GraphMetadata,
    GraphNode,
    GraphPersistenceCoordinator,
    GraphRuntime,
    InMemoryDeliverStoreFactory,
    InMemoryGraphInstanceStore,
    InMemoryNodeStateStore,
    IntegratedInput,
    Node,
    SchedulerKind,
)
from modex_graph.scheduler.bootstrap import BootstrapMode, bootstrap


class WorkNode(Node[DefaultGraphState]):
    def __init__(self, *targets: str, block: bool = False, with_input: bool = True) -> None:
        self.targets = targets
        self.block = block
        self.with_input = with_input
        self.inputs: list[IntegratedInput] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cleaning = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.cleaned = False

    async def execute(
        self, ctx: GraphContext[DefaultGraphState], integrated_input: IntegratedInput
    ) -> None:
        self.inputs.append(integrated_input)
        self.started.set()
        if self.block and len(self.inputs) == 1:
            try:
                await self.release.wait()
            finally:
                self.cleaning.set()
                await self.cleanup_release.wait()
                self.cleaned = True
        for target in self.targets:
            if self.with_input:
                self.deliver(self.name, target, ctx)
            else:
                ctx.dispatch(target)


def context_for(
    graph: Graph[DefaultGraphState], kind: SchedulerKind,
) -> tuple[CompiledGraph[DefaultGraphState], GraphContext[DefaultGraphState]]:
    compiled = graph.compile(scheduler=kind)
    instances = InMemoryGraphInstanceStore()
    instances.save(GraphMetadata(
        graph_instance_id=42, spec_id=7, parent_instance_id=None,
        parent_node=None, status=GraphInstanceStatus.RUNNING,
    ))
    coordinator = GraphPersistenceCoordinator(
        graph_instance_id=42,
        instance_store=instances,
        node_state_store=InMemoryNodeStateStore(42),
        default_deliver_store_factory=InMemoryDeliverStoreFactory(),
    )
    for node in compiled.nodes.values():
        coordinator.register_node(node.node_id)
    ctx = GraphContext(state=DefaultGraphState(), runtime=GraphRuntime(), coordinator=coordinator)
    return compiled, ctx


@pytest.mark.parametrize("kind", list(SchedulerKind))
@pytest.mark.parametrize("with_input", [True, False])
async def test_pause_drains_blocked_node_then_recovers_without_replaying_source(
    kind: SchedulerKind, with_input: bool,
) -> None:
    source = WorkNode("blocked", with_input=with_input)
    blocked = WorkNode("next", block=True)
    following = WorkNode(GraphNode.END)
    graph: Graph[DefaultGraphState] = Graph()
    for name, node in (("source", source), ("blocked", blocked), ("next", following)):
        graph.add_node(name, node)
    for src, dst in ((GraphNode.START, "source"), ("source", "blocked"),
                     ("blocked", "next"), ("next", GraphNode.END)):
        graph.add_edge(src, dst)
    compiled, ctx = context_for(graph, kind)
    engine = GraphEngine(compiled)
    run = asyncio.create_task(engine.run_async(ctx, mode=BootstrapMode.FRESH))
    try:
        await asyncio.wait_for(blocked.started.wait(), 1)
        ctx.control.request_pause("pause now")
        await asyncio.wait_for(blocked.cleaning.wait(), 1)
        assert not run.done(), "GraphDrained must wait for node cleanup"
        assert following.inputs == []
        ctx.control.request_pause("pause now")
        await asyncio.sleep(0)
        assert not run.done()
        blocked.cleanup_release.set()
        with pytest.raises(GraphDrained, match="pause now"):
            await asyncio.wait_for(asyncio.shield(run), 1)
        assert blocked.cleaned
        record = ctx.node_state_store.load_latest(blocked.node_id)
        assert record is not None and record.status.value == "canceled"
        remaining = ctx.coordinator.collect_consumable_delivers(blocked.node_id, 0)
        assert [d.status for d in remaining] == (
            [DeliverConsumptionStatus.CONSUMED_PENDING] if with_input else []
        )

        ctx.coordinator.instance_store.update_status(42, GraphInstanceStatus.PAUSED)
        resumed = GraphContext(
            state=DefaultGraphState(), runtime=GraphRuntime(), coordinator=ctx.coordinator,
        )
        assert bootstrap(resumed, compiled, mode=BootstrapMode.RECOVERY) == ["blocked"]
        await engine.run_async(resumed, mode=BootstrapMode.RECOVERY)
        assert len(source.inputs) == 1
        assert len(blocked.inputs) == 2
        assert blocked.inputs[-1].integrated_content == (["source"] if with_input else [])
        assert len(following.inputs) == 1
        assert resumed.state.result is not None
        assert [p.content for p in resumed.state.result] == ["next"]
        assert ctx.coordinator.collect_consumable_delivers(blocked.node_id, 0) == []
    finally:
        blocked.release.set()
        blocked.cleanup_release.set()
        await asyncio.gather(run, return_exceptions=True)


async def test_parallel_interrupt_recovers_all_interrupted_branches_before_join() -> None:
    source = WorkNode("blocked", "finished", "interrupt")
    blocked = WorkNode(GraphNode.END, block=True)
    finished = WorkNode(GraphNode.END)

    class InterruptOnce(WorkNode):
        async def execute(
            self, ctx: GraphContext[DefaultGraphState], integrated_input: IntegratedInput,
        ) -> None:
            if not self.inputs:
                self.inputs.append(integrated_input)
                await blocked.started.wait()
                await finished.started.wait()
                ctx.interrupt("approval")
            await super().execute(ctx, integrated_input)

    interrupted = InterruptOnce(GraphNode.END)
    graph: Graph[DefaultGraphState] = Graph()
    for name, node in (("source", source), ("blocked", blocked),
                       ("finished", finished), ("interrupt", interrupted)):
        graph.add_node(name, node)
    graph.add_edge(GraphNode.START, "source")
    for branch in ("blocked", "finished", "interrupt"):
        graph.add_edge("source", branch)
        graph.add_edge(branch, GraphNode.END)
    compiled, ctx = context_for(graph, SchedulerKind.PARALLEL)
    run = asyncio.create_task(GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH))
    try:
        await asyncio.wait_for(blocked.cleaning.wait(), 1)
        ctx.control.request_pause("during interrupt cleanup")
        await asyncio.sleep(0)
        assert not run.done()
        blocked.cleanup_release.set()
        with pytest.raises(GraphInterrupt):
            await asyncio.wait_for(asyncio.shield(run), 1)
        assert blocked.cleaned
        ctx.coordinator.instance_store.update_status(42, GraphInstanceStatus.PAUSED)
        resumed = GraphContext(
            state=DefaultGraphState(), runtime=GraphRuntime(), coordinator=ctx.coordinator,
        )
        await GraphEngine(compiled).run_async(resumed, mode=BootstrapMode.RECOVERY)
        assert len(source.inputs) == len(finished.inputs) == 1
        assert len(blocked.inputs) == len(interrupted.inputs) == 2
        assert resumed.state.result is not None
        assert sorted(p.content for p in resumed.state.result) == ["blocked", "finished", "interrupt"]
    finally:
        blocked.release.set()
        blocked.cleanup_release.set()
        await asyncio.gather(run, return_exceptions=True)


@pytest.mark.parametrize("kind", list(SchedulerKind))
async def test_paused_completed_graph_has_no_entry_fallback(kind: SchedulerKind) -> None:
    node = WorkNode(GraphNode.END)
    graph: Graph[DefaultGraphState] = Graph()
    graph.add_node("work", node)
    graph.add_edge(GraphNode.START, "work")
    graph.add_edge("work", GraphNode.END)
    compiled, ctx = context_for(graph, kind)
    await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)
    ctx.coordinator.instance_store.update_status(42, GraphInstanceStatus.PAUSED)
    resumed = GraphContext(
        state=DefaultGraphState(), runtime=GraphRuntime(), coordinator=ctx.coordinator,
    )
    assert bootstrap(resumed, compiled, mode=BootstrapMode.RECOVERY) == []
    await GraphEngine(compiled).run_async(resumed, mode=BootstrapMode.RECOVERY)
    assert len(node.inputs) == 1
    assert resumed.reached_end


@pytest.mark.parametrize("kind", list(SchedulerKind))
async def test_recovery_of_interrupted_end_preserves_terminal_reachability(
    kind: SchedulerKind,
) -> None:
    class InterruptEnd(WorkNode):
        async def execute(
            self, ctx: GraphContext[DefaultGraphState], integrated_input: IntegratedInput,
        ) -> None:
            self.inputs.append(integrated_input)
            if len(self.inputs) == 1:
                ctx.interrupt("end approval")

    end = InterruptEnd()
    source = WorkNode(GraphNode.END)
    graph: Graph[DefaultGraphState] = Graph()
    graph.add_node("source", source)
    graph.add_node(GraphNode.END, end)
    graph.add_edge(GraphNode.START, "source")
    graph.add_edge("source", GraphNode.END)
    compiled, ctx = context_for(graph, kind)
    with pytest.raises(GraphInterrupt):
        await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)
    resumed = GraphContext(
        state=DefaultGraphState(), runtime=GraphRuntime(), coordinator=ctx.coordinator,
    )
    await GraphEngine(compiled).run_async(resumed, mode=BootstrapMode.RECOVERY)
    assert len(source.inputs) == 1
    assert len(end.inputs) == 2
    assert resumed.reached_end


async def test_parallel_task_ready_before_pause_does_not_enter_node() -> None:
    class PauseNode(WorkNode):
        async def execute(
            self, ctx: GraphContext[DefaultGraphState], integrated_input: IntegratedInput,
        ) -> None:
            await super().execute(ctx, integrated_input)
            ctx.control.request_pause("ready race")

    source = WorkNode("a_pause", "z_ready")
    pause = PauseNode(GraphNode.END)
    ready = WorkNode(GraphNode.END)
    graph: Graph[DefaultGraphState] = Graph()
    for name, node in (("source", source), ("a_pause", pause), ("z_ready", ready)):
        graph.add_node(name, node)
    graph.add_edge(GraphNode.START, "source")
    for branch in ("a_pause", "z_ready"):
        graph.add_edge("source", branch)
        graph.add_edge(branch, GraphNode.END)
    compiled, ctx = context_for(graph, SchedulerKind.PARALLEL)
    with pytest.raises(GraphDrained):
        await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)
    assert ready.inputs == []
    assert ctx.node_state_store.load_latest(ready.node_id) is None


async def test_parallel_canceled_seed_consumes_new_and_previous_input_once() -> None:
    class InterruptOnce(WorkNode):
        async def execute(
            self, ctx: GraphContext[DefaultGraphState], integrated_input: IntegratedInput,
        ) -> None:
            if not self.inputs:
                self.inputs.append(integrated_input)
                ctx.interrupt("approval")
            await super().execute(ctx, integrated_input)

    source = WorkNode("work")
    work = InterruptOnce(GraphNode.END)
    graph: Graph[DefaultGraphState] = Graph()
    graph.add_node("source", source)
    graph.add_node("work", work)
    graph.add_edge(GraphNode.START, "source")
    graph.add_edge("source", "work")
    graph.add_edge("work", GraphNode.END)
    compiled, ctx = context_for(graph, SchedulerKind.PARALLEL)
    with pytest.raises(GraphInterrupt):
        await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)
    ctx.coordinator.route_deliver(work.node_id, "approved", "external", 0)
    await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.RECOVERY)
    assert len(work.inputs) == 2
    assert work.inputs[-1].integrated_content == ["source", "approved"]


@pytest.mark.parametrize("kind", list(SchedulerKind))
async def test_pause_does_not_mask_node_crash(kind: SchedulerKind) -> None:
    class CrashNode(WorkNode):
        async def execute(
            self, ctx: GraphContext[DefaultGraphState], integrated_input: IntegratedInput,
        ) -> None:
            ctx.control.request_pause("concurrent pause")
            raise RuntimeError("actual crash")

    graph: Graph[DefaultGraphState] = Graph()
    graph.add_node("work", CrashNode())
    graph.add_edge("work", GraphNode.END)
    if kind == SchedulerKind.PARALLEL:
        # Other already-created tasks reject entry with GraphDrained. Those
        # control signals must not mask the failing branch's real exception.
        targets = ["work", *(f"z_ready_{i}" for i in range(10))]
        graph.add_node("source", WorkNode(*targets))
        graph.add_edge(GraphNode.START, "source")
        for target in targets:
            graph.add_edge("source", target)
        for target in targets[1:]:
            graph.add_node(target, WorkNode(GraphNode.END))
            graph.add_edge(target, GraphNode.END)
    else:
        graph.add_edge(GraphNode.START, "work")
    compiled, ctx = context_for(graph, kind)
    with pytest.raises(RuntimeError, match="actual crash"):
        await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)


@pytest.mark.parametrize("kind", list(SchedulerKind))
@pytest.mark.parametrize("self_cancel", [False, True])
async def test_stop_drains_without_recanceling_cleanup(
    kind: SchedulerKind, self_cancel: bool,
) -> None:
    class CancelingNode(WorkNode):
        async def execute(
            self, ctx: GraphContext[DefaultGraphState], integrated_input: IntegratedInput,
        ) -> None:
            if self_cancel:
                task = asyncio.current_task()
                assert task is not None
                task.cancel()
            await super().execute(ctx, integrated_input)

    work = CancelingNode(GraphNode.END, block=True)
    graph: Graph[DefaultGraphState] = Graph()
    graph.add_node("work", work)
    graph.add_edge(GraphNode.START, "work")
    graph.add_edge("work", GraphNode.END)
    compiled, ctx = context_for(graph, kind)
    run = asyncio.create_task(GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH))
    try:
        await asyncio.wait_for(work.started.wait(), 1)
        if self_cancel:
            await asyncio.wait_for(work.cleaning.wait(), 1)
        ctx.control.request_stop("stop now")
        await asyncio.wait_for(work.cleaning.wait(), 1)
        await asyncio.sleep(0)
        assert not run.done()
        work.cleanup_release.set()
        with pytest.raises(GraphDrained, match="stop now"):
            await asyncio.wait_for(asyncio.shield(run), 1)
        assert work.cleaned
    finally:
        work.release.set()
        work.cleanup_release.set()
        await asyncio.gather(run, return_exceptions=True)
