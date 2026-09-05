"""Public lifecycle regressions with real SQLite and asynchronous cleanup gates."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Callable

import pytest
from pydantic import BaseModel

from modex_agent.orchestration import GraphOrchestrator, SqliteCoordinatorFactory
from modex_graph import (
    DefaultGraphState,
    EdgeSpec,
    FunctionNodeFactory,
    GraphContext,
    GraphNode,
    GraphOutput,
    GraphOutputAdapter,
    GraphOutputKind,
    GraphPayload,
    GraphSpec,
    IntegratedInput,
    Node,
    NodeFactory,
    NodeRegistry,
    NodeSpec,
    SchedulerKind,
    SqliteGraphInstanceStore,
    SqliteGraphIORecordStore,
    SqliteGraphSpecStore,
    id_generator,
)
from modex_graph import (
    GraphInstanceStatus as Status,
)
from modex_graph.scheduler.bootstrap import BootstrapMode


class Lifecycle(GraphOutputAdapter):
    def __init__(self, scheduler: SchedulerKind) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.store = SqliteGraphInstanceStore(self.connection)
        self.specs = SqliteGraphSpecStore(self.connection)
        self.io = SqliteGraphIORecordStore(self.connection)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cleaning = asyncio.Event()
        self.cleaned = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.outputs: list[GraphOutput] = []
        self.inputs: list[GraphPayload | None] = []
        self.fail = False
        self.fail_cleanup = False
        self.output_gate_kind: GraphOutputKind | None = None
        self.output_gate_status: Status | None = None
        self.output_gate_node: str | None = None
        self.output_entered = asyncio.Event()
        self.output_release = asyncio.Event()
        self.registry = NodeRegistry()
        self.registry.register("function", FunctionNodeFactory({"work": self.work}))
        self.orch = self.build()
        self.spec_id = self.specs.save(
            GraphSpec(
                name="owned",
                state_class="default",
                scheduler=scheduler,
                nodes=[NodeSpec(name="work", node_type="function", config={"function": "work"})],
                edges=[
                    EdgeSpec(source=GraphNode.START, target="work"),
                    EdgeSpec(source="work", target=GraphNode.END),
                ],
            )
        )

    def build(self) -> GraphOrchestrator:
        return GraphOrchestrator(
            node_registry=self.registry,
            state_classes={"default": DefaultGraphState},
            instance_store=self.store,
            spec_store=self.specs,
            io_store=self.io,
            coordinator_factory=SqliteCoordinatorFactory(self.connection),
            output_adapter=self,
        )

    async def work(self, ctx: GraphContext[DefaultGraphState]) -> GraphPayload:
        self.inputs.append(ctx.user_input)
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cleaning.set()
            await self.cleanup_release.wait()
            self.cleaned.set()
            if self.fail_cleanup:
                raise RuntimeError("cleanup failure") from None
            raise
        if self.fail:
            raise RuntimeError("manual recovery failure")
        return GraphPayload(content="answer")

    async def emit(self, output: GraphOutput) -> None:
        if (
            output.kind is self.output_gate_kind
            and output.status is self.output_gate_status
            and output.node_name == self.output_gate_node
        ):
            self.output_entered.set()
            await self.output_release.wait()
        self.outputs.append(output)

    def status(self, gid: int) -> Status:
        return self.orch.get_state(gid).metadata.status

    async def start(self) -> tuple[int, asyncio.Task[None]]:
        gid = await self.orch.create_instance(
            self.spec_id, user_input=GraphPayload(content="origin")
        )
        task = self.orch.start_run(gid)
        await asyncio.wait_for(self.entered.wait(), 2)
        return gid, task


@pytest.fixture(params=[SchedulerKind.LINEAR, SchedulerKind.PARALLEL])
async def lifecycle(request: pytest.FixtureRequest) -> AsyncIterator[Lifecycle]:
    env = Lifecycle(request.param)
    try:
        yield env
    finally:
        env.release.set()
        env.cleanup_release.set()
        env.output_release.set()
        await env.orch.cleanup()
        env.connection.close()


@pytest.fixture
def backwards_restart(monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    clock = 2_000_000_000.0
    worker = 1
    monkeypatch.setattr(id_generator.time, "time", lambda: clock)
    monkeypatch.setattr(
        id_generator, "_default_generator", id_generator.SnowflakeIdGenerator(worker)
    )

    def restart() -> None:
        nonlocal clock, worker
        clock -= 1
        worker += 1
        monkeypatch.setattr(
            id_generator, "_default_generator", id_generator.SnowflakeIdGenerator(worker)
        )

    return restart


async def test_pause_waits_for_cleanup_and_rejects_early_resume(lifecycle: Lifecycle) -> None:
    env = lifecycle
    gid, run = await env.start()
    context = env.orch.get_graph_context(gid)
    pause = asyncio.create_task(env.orch.pause(gid))
    try:
        await asyncio.wait_for(env.cleaning.wait(), 2)
        assert env.status(gid) is Status.PAUSING
        assert not pause.done()
        assert env.orch.get_graph_context(gid) is context
        with pytest.raises(ValueError):
            env.orch.start_resume(gid)
        with pytest.raises(ValueError):
            await env.orch.run_instance(gid, mode=BootstrapMode.RECOVERY)
    finally:
        env.cleanup_release.set()
        await pause
        await run
    assert env.cleaned.is_set()
    assert env.status(gid) is Status.PAUSED
    assert env.orch.get_graph_context(gid) is None
    await env.orch.pause(gid)
    env.release.set()
    await env.orch.start_resume(gid)
    assert env.status(gid) is Status.COMPLETED
    assert env.inputs == [GraphPayload(content="origin")] * 2
    statuses = [o.status for o in env.outputs if o.kind is GraphOutputKind.STATUS_CHANGED]
    assert statuses == [Status.RUNNING, Status.PAUSING, Status.PAUSED, Status.RUNNING]


async def test_cancelled_pause_request_does_not_cancel_cleanup(lifecycle: Lifecycle) -> None:
    env = lifecycle
    gid, run = await env.start()
    pause = asyncio.create_task(env.orch.pause(gid))
    await asyncio.wait_for(env.cleaning.wait(), 2)
    pause.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pause
    assert not run.done()
    assert env.status(gid) is Status.PAUSING
    env.cleanup_release.set()
    await run
    assert env.cleaned.is_set()
    assert env.status(gid) is Status.PAUSED


async def test_stop_upgrades_repeated_pause_while_draining(lifecycle: Lifecycle) -> None:
    env = lifecycle
    gid, run = await env.start()
    pause = asyncio.create_task(env.orch.pause(gid))
    await asyncio.wait_for(env.cleaning.wait(), 2)
    second_pause = asyncio.create_task(env.orch.pause(gid))
    stop = asyncio.create_task(env.orch.stop(gid))
    await asyncio.sleep(0)
    try:
        assert env.status(gid) is Status.STOPPING
        assert not stop.done()
        assert not second_pause.done()
    finally:
        env.cleanup_release.set()
        await asyncio.gather(pause, second_pause, stop, run)
    assert env.status(gid) is Status.STOPPED
    assert env.cleaned.is_set()
    statuses = [o.status for o in env.outputs if o.kind is GraphOutputKind.STATUS_CHANGED]
    assert statuses == [Status.RUNNING, Status.PAUSING, Status.STOPPING, Status.STOPPED]


@pytest.mark.parametrize("operation", ["start_run", "start_resume", "start_invoke"])
async def test_immediate_duplicate_start_is_rejected(lifecycle: Lifecycle, operation: str) -> None:
    env = lifecycle
    gid = await env.orch.create_instance(env.spec_id)
    if operation == "start_resume":
        env.store.update_status(gid, Status.PAUSED)
        start = env.orch.start_resume
    elif operation == "start_invoke":
        env.store.update_status(gid, Status.COMPLETED)
        start = env.orch.start_invoke
    else:
        start = env.orch.start_run
    first = start(gid)
    try:
        with pytest.raises(ValueError):
            start(gid)
        with pytest.raises(ValueError):
            await env.orch.run_instance(gid, mode=BootstrapMode.FRESH)
    finally:
        env.release.set()
        await first
    assert len(env.inputs) == 1


async def test_recovery_does_not_touch_local_running_owner(lifecycle: Lifecycle) -> None:
    env = lifecycle
    gid, run = await env.start()
    context = env.orch.get_graph_context(gid)
    before = env.store.load(gid)
    assert await env.orch.recover_crashed() == []
    assert env.store.load(gid) == before
    assert env.orch.get_graph_context(gid) is context
    env.release.set()
    await run
    assert env.status(gid) is Status.COMPLETED


async def test_paused_restart_retains_input_and_manual_failure_propagates(
    lifecycle: Lifecycle,
) -> None:
    env = lifecycle
    gid, run = await env.start()
    env.cleanup_release.set()
    await env.orch.pause(gid)
    await run
    await env.orch.cleanup()
    env.orch = env.build()
    assert await env.orch.recover_crashed() == []
    assert env.status(gid) is Status.PAUSED
    env.release.set()
    env.fail = True
    with pytest.raises(RuntimeError, match="manual recovery failure"):
        await env.orch.resume(gid)
    assert env.inputs == [GraphPayload(content="origin")] * 2
    assert env.status(gid) is Status.CRASHED


async def test_recovery_of_completed_end_preserves_result_without_replay(
    lifecycle: Lifecycle,
) -> None:
    env = lifecycle
    env.release.set()
    gid, run = await env.start()
    await run
    env.store.update_status(gid, Status.CRASHED)
    assert await env.orch.recover_crashed() == [gid]
    assert len(env.inputs) == 1
    latest = env.io.get_latest_by_instance(gid)
    assert latest is not None
    assert latest.user_input == GraphPayload(content="origin")
    assert latest.output == [GraphPayload(content="answer")]
    assert env.outputs[-1].result == [GraphPayload(content="answer")]


async def test_stop_paused_after_restart_and_delivery_use_persisted_instance(
    lifecycle: Lifecycle,
) -> None:
    env = lifecycle
    gid, run = await env.start()
    env.cleanup_release.set()
    await env.orch.pause(gid)
    await run
    await env.orch.cleanup()
    env.orch = env.build()
    await env.orch.deliver_to_node(gid, "work", GraphPayload(content="resume input"))
    await env.orch.stop(gid)
    assert env.status(gid) is Status.STOPPED
    assert [o.status for o in env.outputs[-2:]] == [Status.STOPPING, Status.STOPPED]


async def test_pause_retains_owner_until_node_output_finalization_exits(
    lifecycle: Lifecycle,
) -> None:
    env = lifecycle
    env.output_gate_kind = GraphOutputKind.NODE_STARTED
    env.output_gate_node = "work"
    gid, run = await env.start()
    await asyncio.wait_for(env.output_entered.wait(), 2)
    env.cleanup_release.set()
    pause = asyncio.create_task(env.orch.pause(gid))
    await asyncio.wait_for(env.cleaned.wait(), 2)
    await asyncio.sleep(0)
    assert env.status(gid) is Status.PAUSING
    assert not pause.done()
    with pytest.raises(ValueError):
        env.orch.unregister_instance(gid)
    with pytest.raises(ValueError):
        env.orch.start_resume(gid)
    env.output_release.set()
    await asyncio.gather(pause, run)
    assert env.status(gid) is Status.PAUSED


@pytest.mark.parametrize("stop", [False, True])
async def test_natural_completion_wins_control_race_during_finalization(
    lifecycle: Lifecycle,
    stop: bool,
) -> None:
    env = lifecycle
    env.output_gate_kind = GraphOutputKind.COMPLETED
    env.release.set()
    gid, run = await env.start()
    await asyncio.wait_for(env.output_entered.wait(), 2)
    request = asyncio.create_task(env.orch.stop(gid) if stop else env.orch.pause(gid))
    await asyncio.sleep(0)
    assert not request.done()
    assert env.status(gid) is Status.COMPLETED
    with pytest.raises(ValueError):
        env.orch.start_invoke(gid)
    env.output_release.set()
    await asyncio.gather(request, run)
    assert env.status(gid) is Status.COMPLETED
    assert [o.status for o in env.outputs if o.kind is GraphOutputKind.STATUS_CHANGED] == [
        Status.RUNNING
    ]


async def test_stop_during_paused_status_emission_still_finalizes_stopped(
    lifecycle: Lifecycle,
) -> None:
    env = lifecycle
    env.output_gate_kind = GraphOutputKind.STATUS_CHANGED
    env.output_gate_status = Status.PAUSED
    gid, run = await env.start()
    env.cleanup_release.set()
    pause = asyncio.create_task(env.orch.pause(gid))
    await asyncio.wait_for(env.output_entered.wait(), 2)
    stop = asyncio.create_task(env.orch.stop(gid))
    await asyncio.sleep(0)
    assert env.status(gid) is Status.STOPPING
    env.output_release.set()
    await asyncio.gather(run, pause, stop)
    assert env.status(gid) is Status.STOPPED
    assert env.outputs[-1].status is Status.STOPPED


async def test_pause_before_admitted_coroutine_starts_keeps_status_order(
    lifecycle: Lifecycle,
) -> None:
    env = lifecycle
    gid = await env.orch.create_instance(env.spec_id)
    run = env.orch.start_run(gid)
    await env.orch.pause(gid)
    await run
    assert env.inputs == []
    assert env.status(gid) is Status.PAUSED
    assert [o.status for o in env.outputs] == [Status.RUNNING, Status.PAUSING, Status.PAUSED]


async def test_resume_waits_for_execution_and_preserves_node_identity(lifecycle: Lifecycle) -> None:
    env = lifecycle
    gid, run = await env.start()
    before = env.store.load(gid)
    env.cleanup_release.set()
    await env.orch.pause(gid)
    await run
    env.entered.clear()
    resume = asyncio.create_task(env.orch.resume(gid))
    await asyncio.wait_for(env.entered.wait(), 2)
    assert not resume.done()
    with pytest.raises(ValueError):
        env.orch.start_resume(gid)
    env.release.set()
    await resume
    after = env.store.load(gid)
    assert before is not None and after is not None
    assert after.graph_instance_id == before.graph_instance_id
    assert after.node_id_map == before.node_id_map
    assert after.version == before.version + 1


async def test_recovery_admission_rejects_misclassified_local_owner_without_mutation(
    lifecycle: Lifecycle,
) -> None:
    env = lifecycle
    gid, run = await env.start()
    context = env.orch.get_graph_context(gid)
    env.store.update_status(gid, Status.CRASHED)
    before = env.store.load(gid)
    assert await env.orch.recover_crashed() == []
    assert env.store.load(gid) == before
    assert env.orch.get_graph_context(gid) is context
    env.release.set()
    await run


async def test_output_persistence_failure_cannot_publish_completed(
    lifecycle: Lifecycle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = lifecycle

    def fail_output(record_id: int, output: list[GraphPayload] | None) -> None:
        raise RuntimeError("output persistence failure")

    monkeypatch.setattr(env.io, "update_output", fail_output)
    env.release.set()
    gid, run = await env.start()
    with pytest.raises(RuntimeError, match="output persistence failure"):
        await run
    assert env.status(gid) is Status.CRASHED
    assert env.outputs[-1].kind is GraphOutputKind.CRASHED


@pytest.mark.parametrize("stop", [False, True])
async def test_cleanup_fault_propagates_instead_of_successful_drain(
    lifecycle: Lifecycle,
    stop: bool,
) -> None:
    env = lifecycle
    gid, run = await env.start()
    env.fail_cleanup = True
    request = asyncio.create_task(env.orch.stop(gid) if stop else env.orch.pause(gid))
    await asyncio.wait_for(env.cleaning.wait(), 2)
    env.cleanup_release.set()
    with pytest.raises(RuntimeError, match="cleanup failure"):
        await request
    with pytest.raises(RuntimeError, match="cleanup failure"):
        await run
    assert env.status(gid) is Status.CRASHED
    assert env.outputs[-1].kind is GraphOutputKind.CRASHED
    assert any(o.kind is GraphOutputKind.NODE_CRASHED for o in env.outputs)


async def test_repeated_owner_cancellation_cannot_abort_node_cleanup(lifecycle: Lifecycle) -> None:
    env = lifecycle
    gid, run = await env.start()
    run.cancel()
    await asyncio.wait_for(env.cleaning.wait(), 2)
    run.cancel()
    await asyncio.sleep(0)
    run.cancel()
    await asyncio.sleep(0)
    try:
        assert not run.done()
        assert env.orch.get_graph_context(gid) is not None
        with pytest.raises(ValueError):
            env.orch.start_run(gid)
    finally:
        env.cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await run
    assert env.cleaned.is_set()
    assert env.status(gid) is Status.CRASHED
    assert env.outputs[-1].kind is GraphOutputKind.CRASHED


async def test_immediate_task_cancel_settles_admitted_version(lifecycle: Lifecycle) -> None:
    env = lifecycle
    gid = await env.orch.create_instance(env.spec_id, user_input=GraphPayload(content="admitted"))
    run = env.orch.start_run(gid)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert env.status(gid) is Status.CRASHED
    assert env.inputs == []
    assert env.outputs[-1].kind is GraphOutputKind.CRASHED
    latest = env.io.get_latest_by_instance(gid)
    assert latest is not None and latest.user_input == GraphPayload(content="admitted")


async def test_owner_cancellation_during_status_finalization_waits_for_emission(
    lifecycle: Lifecycle,
) -> None:
    env = lifecycle
    env.output_gate_kind = GraphOutputKind.STATUS_CHANGED
    env.output_gate_status = Status.PAUSED
    gid, run = await env.start()
    env.cleanup_release.set()
    pause = asyncio.create_task(env.orch.pause(gid))
    await asyncio.wait_for(env.output_entered.wait(), 2)
    run.cancel()
    await asyncio.sleep(0)
    run.cancel()
    await asyncio.sleep(0)
    try:
        assert not run.done()
        with pytest.raises(ValueError):
            env.orch.start_resume(gid)
    finally:
        env.output_release.set()
        await asyncio.gather(run, pause, return_exceptions=True)
    assert env.status(gid) is Status.PAUSED
    assert env.outputs[-1].status is Status.PAUSED
    assert env.orch.get_graph_context(gid) is None


@pytest.mark.parametrize("cancel", [False, True])
async def test_end_output_survives_control_scheduled_by_node_completed_event(
    lifecycle: Lifecycle,
    monkeypatch: pytest.MonkeyPatch,
    cancel: bool,
) -> None:
    env = lifecycle
    pause: asyncio.Task[None] | None = None
    run: asyncio.Task[None] | None = None
    original_emit = env.emit

    async def pause_at_end(output: GraphOutput) -> None:
        nonlocal pause
        await original_emit(output)
        if output.kind is GraphOutputKind.NODE_COMPLETED and output.node_name == GraphNode.END:
            if cancel:
                assert run is not None
                run.cancel()
            else:
                pause = asyncio.create_task(env.orch.pause(output.graph_instance_id))

    monkeypatch.setattr(env, "emit", pause_at_end)
    env.release.set()
    gid, run = await env.start()
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await run
        assert await env.orch.recover_crashed() == [gid]
    else:
        await run
        assert pause is not None
        await pause
    if env.status(gid) is Status.PAUSED:
        await env.orch.resume(gid)
    assert env.status(gid) is Status.COMPLETED
    assert len(env.inputs) == 1
    latest = env.io.get_latest_by_instance(gid)
    assert latest is not None and latest.output == [GraphPayload(content="answer")]
    assert env.outputs[-1].result == [GraphPayload(content="answer")]


async def test_owner_cancellation_during_node_output_drain_retains_finalization(
    lifecycle: Lifecycle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = lifecycle
    env.output_gate_kind = GraphOutputKind.NODE_STARTED
    env.output_gate_node = "work"
    gid, run = await env.start()
    context = env.orch.get_graph_context(gid)
    assert context is not None
    finalizing = asyncio.Event()
    original_drain = context.coordinator.drain_output_events

    async def drain_events() -> None:
        finalizing.set()
        await original_drain()

    monkeypatch.setattr(context.coordinator, "drain_output_events", drain_events)
    env.cleanup_release.set()
    pause = asyncio.create_task(env.orch.pause(gid))
    await asyncio.wait_for(finalizing.wait(), 2)
    run.cancel()
    await asyncio.sleep(0)
    run.cancel()
    await asyncio.sleep(0)
    try:
        assert not run.done()
        assert env.status(gid) is Status.PAUSING
        assert env.orch.get_graph_context(gid) is context
    finally:
        env.output_release.set()
        await asyncio.gather(run, pause, return_exceptions=True)
    assert env.status(gid) is Status.PAUSED
    assert env.outputs[-1].status is Status.PAUSED


async def test_fresh_reinvoke_paused_before_start_resumes_new_input_after_restart(
    lifecycle: Lifecycle,
) -> None:
    env = lifecycle
    env.release.set()
    gid, first = await env.start()
    await first
    second = env.orch.start_invoke(gid, user_input=GraphPayload(content="second"))
    await env.orch.pause(gid)
    await second
    assert len(env.inputs) == 1
    assert env.status(gid) is Status.PAUSED
    await env.orch.cleanup()
    env.orch = env.build()
    # Repeated pause/recovery before START must retain the same fresh intent.
    third = env.orch.start_resume(gid)
    await env.orch.pause(gid)
    await third
    await env.orch.resume(gid)
    assert env.inputs == [GraphPayload(content="origin"), GraphPayload(content="second")]
    assert env.status(gid) is Status.COMPLETED


async def test_running_status_must_finish_emitting_before_nodes_start(lifecycle: Lifecycle) -> None:
    env = lifecycle
    env.output_gate_kind = GraphOutputKind.STATUS_CHANGED
    env.output_gate_status = Status.RUNNING
    gid = await env.orch.create_instance(env.spec_id)
    run = env.orch.start_run(gid)
    await asyncio.wait_for(env.output_entered.wait(), 2)
    await asyncio.sleep(0)
    try:
        assert not env.entered.is_set()
        assert env.outputs == []
    finally:
        env.output_release.set()
        env.release.set()
        await run
    assert env.outputs[0].status is Status.RUNNING


async def test_cancelled_owner_emits_ordered_crashed_outcome(lifecycle: Lifecycle) -> None:
    env = lifecycle
    gid, run = await env.start()
    env.cleanup_release.set()
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert env.status(gid) is Status.CRASHED
    assert env.outputs[-1].kind is GraphOutputKind.CRASHED
    assert env.outputs[0].status is Status.RUNNING


@pytest.mark.parametrize("new_input", [GraphPayload(content="second"), None])
@pytest.mark.parametrize("setup_failure", ["missing_spec", "invalid_state"])
async def test_fresh_intent_survives_setup_failure_without_prior_input_or_output(
    lifecycle: Lifecycle,
    new_input: GraphPayload | None,
    setup_failure: str,
    backwards_restart: Callable[[], None],
) -> None:
    env = lifecycle
    env.release.set()
    gid, first = await env.start()
    await first
    backwards_restart()
    spec = env.specs.load_by_id(env.spec_id)
    assert spec is not None
    env.specs.delete(env.spec_id)
    if setup_failure == "invalid_state":
        env.specs.save(spec.model_copy(update={"state_class": "missing"}), env.spec_id)
    with pytest.raises(ValueError):
        await env.orch.start_invoke(gid, user_input=new_input)
    assert env.status(gid) is Status.CRASHED
    failed_io = env.io.get_latest_by_instance(gid)
    assert failed_io is not None
    assert failed_io.user_input == new_input
    assert failed_io.output is None
    env.specs.delete(env.spec_id)
    env.specs.save(spec, env.spec_id)
    await env.orch.cleanup()
    env.orch = env.build()
    assert await env.orch.recover_crashed() == [gid]
    assert env.inputs == [GraphPayload(content="origin"), new_input]
    assert env.status(gid) is Status.COMPLETED


async def test_recovery_does_not_treat_previous_fresh_end_as_current_completion(
    lifecycle: Lifecycle,
    monkeypatch: pytest.MonkeyPatch,
    backwards_restart: Callable[[], None],
) -> None:
    env = lifecycle
    env.release.set()
    gid, first = await env.start()
    await first

    backwards_restart()

    class DeadEnd(Node[DefaultGraphState]):
        async def execute(
            self, ctx: GraphContext[DefaultGraphState], integrated_input: IntegratedInput
        ) -> None:
            await env.work(ctx)

    class DeadEndFactory(NodeFactory):
        def create(self, spec: NodeSpec) -> Node[DefaultGraphState]:
            return DeadEnd()

        def config_schema(self) -> type[BaseModel]:
            return FunctionNodeFactory().config_schema()

    env.registry = NodeRegistry()
    env.registry.register("function", DeadEndFactory())
    env.orch = env.build()
    original_emit = env.emit
    pause: asyncio.Task[None] | None = None

    async def pause_after_work(output: GraphOutput) -> None:
        nonlocal pause
        await original_emit(output)
        if output.kind is GraphOutputKind.NODE_COMPLETED and output.node_name == "work":
            pause = asyncio.create_task(env.orch.pause(gid))

    monkeypatch.setattr(env, "emit", pause_after_work)
    await env.orch.start_invoke(gid, user_input=GraphPayload(content="second"))
    assert pause is not None
    await pause
    assert env.status(gid) is Status.PAUSED
    monkeypatch.setattr(env, "emit", original_emit)
    await env.orch.cleanup()
    env.orch = env.build()
    await env.orch.resume(gid)
    assert env.status(gid) is Status.FAILED
    assert env.inputs == [GraphPayload(content="origin"), GraphPayload(content="second")]
    latest = env.io.get_latest_by_instance(gid)
    assert latest is not None and latest.output is None
    assert env.outputs[-1].kind is GraphOutputKind.FAILED
    snapshot = env.orch.get_state(gid)
    end_versions = snapshot.nodes[snapshot.metadata.node_id_map[GraphNode.END]]
    assert len(end_versions) == 1


async def test_recovery_io_preserves_same_logical_output_through_immediate_cancellation(
    lifecycle: Lifecycle,
    monkeypatch: pytest.MonkeyPatch,
    backwards_restart: Callable[[], None],
) -> None:
    env = lifecycle
    original_emit = env.emit
    pause: asyncio.Task[None] | None = None

    async def pause_at_end(output: GraphOutput) -> None:
        nonlocal pause
        await original_emit(output)
        if output.kind is GraphOutputKind.NODE_COMPLETED and output.node_name == GraphNode.END:
            pause = asyncio.create_task(env.orch.pause(output.graph_instance_id))

    monkeypatch.setattr(env, "emit", pause_at_end)
    env.release.set()
    gid, run = await env.start()
    await run
    assert pause is not None
    await pause
    assert env.status(gid) is Status.PAUSED
    monkeypatch.setattr(env, "emit", original_emit)
    backwards_restart()
    resumed = env.orch.start_resume(gid)
    resumed.cancel()
    with pytest.raises(asyncio.CancelledError):
        await resumed
    latest = env.io.get_latest_by_instance(gid)
    assert latest is not None
    assert latest.user_input == GraphPayload(content="origin")
    assert latest.output == [GraphPayload(content="answer")]
    backwards_restart()
    await env.orch.cleanup()
    env.orch = env.build()
    assert await env.orch.recover_crashed() == [gid]
    assert env.status(gid) is Status.COMPLETED
    assert env.outputs[-1].result == [GraphPayload(content="answer")]
    assert len(env.inputs) == 1


@pytest.mark.parametrize("new_input", [GraphPayload(content="second"), None])
async def test_fresh_cancelled_recovery_never_inherits_previous_logical_io(
    lifecycle: Lifecycle,
    new_input: GraphPayload | None,
) -> None:
    env = lifecycle
    env.release.set()
    gid, first = await env.start()
    await first
    second = env.orch.start_invoke(gid, user_input=new_input)
    await env.orch.pause(gid)
    await second
    resumed = env.orch.start_resume(gid)
    resumed.cancel()
    with pytest.raises(asyncio.CancelledError):
        await resumed
    latest = env.io.get_latest_by_instance(gid)
    assert latest is not None and latest.user_input == new_input and latest.output is None
    await env.orch.cleanup()
    env.orch = env.build()
    assert await env.orch.recover_crashed() == [gid]
    assert env.inputs == [GraphPayload(content="origin"), new_input]


async def test_restart_backwards_clock_scopes_fresh_run_by_membership(
    lifecycle: Lifecycle,
    backwards_restart: Callable[[], None],
) -> None:
    env = lifecycle
    env.release.set()
    gid, first = await env.start()
    await first
    first_io = env.io.get_latest_by_instance(gid)
    assert first_io is not None
    await env.orch.cleanup()
    backwards_restart()
    env.orch = env.build()
    second = env.orch.start_invoke(gid, user_input=GraphPayload(content="second"))
    await env.orch.pause(gid)
    await second
    second_io = env.io.get_latest_by_instance(gid)
    assert second_io is not None and second_io.record_id < first_io.record_id
    assert env.status(gid) is Status.PAUSED
    await env.orch.resume(gid)
    assert env.inputs == [GraphPayload(content="origin"), GraphPayload(content="second")]
    snapshot = env.orch.get_state(gid)
    for records in snapshot.nodes.values():
        assert {r.graph_run_version for r in records} == {1, 2}
    latest = env.io.get_latest_by_instance(gid)
    assert latest is not None and latest.graph_run_version == 2


async def test_legacy_unscoped_sqlite_graph_resumes_without_replaying_end(
    lifecycle: Lifecycle,
) -> None:
    """Pre-membership rows must stay unscoped, not be assigned a guessed run."""
    env = lifecycle
    env.release.set()
    gid, first = await env.start()
    await first
    await env.orch.cleanup()
    # Reconstruct the shipped schema before logical membership existed.
    env.connection.execute("ALTER TABLE node_states DROP COLUMN graph_run_version")
    env.connection.execute("ALTER TABLE graph_io_records DROP COLUMN graph_run_version")
    env.connection.execute(
        "UPDATE graph_instances SET attrs_json = json_remove(attrs_json, '$.graph_run_version')"
    )
    env.connection.commit()
    env.store.update_status(gid, Status.PAUSED)
    env.io = SqliteGraphIORecordStore(env.connection)
    env.orch = env.build()
    await env.orch.resume(gid)
    assert env.status(gid) is Status.COMPLETED
    assert len(env.inputs) == 1
    assert env.outputs[-1].result == [GraphPayload(content="answer")]
    snapshot = env.orch.get_state(gid)
    assert all(
        record.graph_run_version is None
        for records in snapshot.nodes.values()
        for record in records
    )
    latest = env.io.get_latest_by_instance(gid)
    assert latest is not None and latest.graph_run_version is None
    await env.orch.start_invoke(gid, user_input=GraphPayload(content="new scoped run"))
    assert len(env.inputs) == 2
    latest = env.io.get_latest_by_instance(gid)
    assert latest is not None and latest.graph_run_version == latest.version
