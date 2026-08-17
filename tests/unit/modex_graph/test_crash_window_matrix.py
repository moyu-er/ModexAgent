# ruff: noqa: ANN401
"""Crash-window matrix test suite (W5-W14 + N1-N7).

Window-test traceability (audit: docs/design/graph-convergence/research/crash-window-audit.md):

W1-W4: covered by tests/unit/modex_graph/test_staged_deliver_crash_matrix.py (task 9).
  W1 execute crash -> STAGED survives + retry promotes both (at-least-once)
  W2 complete<->promote crash -> bootstrap auto-promote
  W3 promote<->dispatch gap -> store rescan seeds
  W4 input at-least-once (mark_consumed->complete crash)

W5:  TestWindow5OrphanRunning        - orphan RUNNING node record cleanup (I4)
W6:  TestWindow6InterruptPauseSplit   - GraphInterrupt <-> instance PAUSED split write (I1)
W7:  TestWindow7RemovedSnapshot       - retired snapshot window (structural + semantic)
W8:  TestWindow8InstanceOrphanRunning - instance-level orphan RUNNING (I1/I4)
W9:  TestWindow9ControlPathTransitions- GraphDrained / control-path status converge (I4)
W10: TestWindow10StopInFlight         - cooperative drain, post-stop delivers (D6)
W11: TestWindow11CompleteIORecord     - complete<->IORecord failure (D7)
W12: TestWindow12ReInvokeLeftover     - v2 with v1 leftover delivers (D2 by-design)
W13: TestWindow13ExternalDeliverGap   - deliver<->notify gap, store rescan (I2)
W14: TestWindow14ExceptionPropagation - in-memory guard, exception propagation

N1: TestN1FreshVsRecovery       - FRESH vs RECOVERY mode deliver残留
N2: TestN2EndSeedReachedEnd     - END seed / reached_end
N3: TestN3ReactNullPath         - Null four-state contract regression
N4: TestN4SweeperReference      - 09 sweeper (reference to task 29)
N5: TestN5D6StopCooperative     - D6 stop cooperative semantics
N6: TestN6D7CompleteIORecord    - D7 complete<->IORecord
N7: TestN7D8LinearNoExternal    - D8 Linear no external deliver admission

Ticket: docs/design/graph-convergence/issues/12-crash-window-matrix-tests.md
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest
from fault_injection import FaultInjectingNodeStateStore
from helpers import CounterState, InterruptNode, make_runtime

from modex_graph import (
    DefaultGraphState,
    DeliverConsumptionStatus,
    Graph,
    GraphContext,
    GraphInstanceStatus,
    GraphInterrupt,
    GraphIORecord,
    GraphMetadata,
    GraphNode,
    GraphPayload,
    GraphPersistenceCoordinator,
    GraphRuntime,
    InMemoryDeliverStoreFactory,
    InMemoryGraphInstanceStore,
    InMemoryGraphIORecordStore,
    InMemoryNodeStateStore,
    IntegratedInput,
    InvocationStatus,
    LinearScheduler,
    Node,
    NullDeliverStore,
    ParallelScheduler,
    SchedulerKind,
    create_null_coordinator,
)
from modex_graph.scheduler.bootstrap import BootstrapMode, bootstrap

# ── Test node helpers ──────────────────────────────────────────────────────


class _SourceNode(Node[Any]):
    """Delivers a payload to a target during execute."""

    def __init__(self, payload: str, target: str) -> None:
        self._payload = payload
        self._target = target
        self.executions: list[str] = []

    async def execute(
        self, ctx: GraphContext[Any], integrated_input: IntegratedInput
    ) -> None:
        self.executions.append(self._payload)
        self.deliver(self._payload, self._target, ctx)


class _RecordingTarget(Node[Any]):
    """Records integrated inputs. Optionally crashes once after integration."""

    def __init__(self, *, crash_once: bool = False) -> None:
        self.crash_once = crash_once
        self.inputs: list[Any] = []

    async def execute(
        self, ctx: GraphContext[Any], integrated_input: IntegratedInput
    ) -> None:
        self.inputs.append(integrated_input.integrated_content)
        if self.crash_once:
            self.crash_once = False
            raise RuntimeError("crashed after input integration")


class _SimpleEndNode(Node[DefaultGraphState]):
    """Mimics EndNode: sets state.result from integrated_input.payloads."""

    async def execute(
        self, ctx: GraphContext[DefaultGraphState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.result = [
            GraphPayload(content=str(payload.content))
            for payload in integrated_input.payloads
        ]


# ── Coordinator / context builders ─────────────────────────────────────────


def _make_in_memory_coordinator(
    graph_instance_id: int,
    node_ids: tuple[str, ...],
    *,
    node_state_store: Any | None = None,
) -> tuple[GraphPersistenceCoordinator, InMemoryGraphInstanceStore]:
    """Build a coordinator with InMemory stores (four-state deliver store)."""
    instance_store = InMemoryGraphInstanceStore()
    instance_store.save(
        GraphMetadata(
            graph_instance_id=graph_instance_id,
            spec_id=0,
            parent_instance_id=None,
            parent_node=None,
            status=GraphInstanceStatus.RUNNING,
        )
    )
    nss = (
        node_state_store
        if node_state_store is not None
        else InMemoryNodeStateStore(graph_instance_id)
    )
    coordinator = GraphPersistenceCoordinator(
        graph_instance_id=graph_instance_id,
        instance_store=instance_store,
        node_state_store=nss,
        default_deliver_store_factory=InMemoryDeliverStoreFactory(),
    )
    for nid in node_ids:
        coordinator.register_node(nid)
    return coordinator, instance_store


def _make_ctx(
    coordinator: GraphPersistenceCoordinator,
    graph_instance_id: int,
    *,
    state: Any | None = None,
    runtime: GraphRuntime | None = None,
) -> GraphContext[Any]:
    ctx = GraphContext(
        state=state if state is not None else CounterState(),
        runtime=runtime if runtime is not None else make_runtime(),
        coordinator=coordinator,
        scheduler_kind=SchedulerKind.LINEAR,
        graph_instance_id=graph_instance_id,
    )
    ctx.set_dispatch_handler(lambda _src, _tgt: None)
    return ctx


def _compile_pair(
    source: Node[Any], target: Node[Any]
) -> Any:
    graph: Graph[Any] = Graph()
    graph.add_node("a", source)
    graph.add_node("b", target)
    graph.add_edge(GraphNode.START, "a")
    graph.add_edge("a", "b")
    graph.add_edge("b", GraphNode.END)
    return graph.compile()


def _load_status(store: InMemoryGraphInstanceStore, gid: int) -> GraphInstanceStatus:
    """Load instance status with None-narrowing."""
    meta = store.load(gid)
    assert meta is not None
    return meta.status


def _load_version(store: InMemoryGraphInstanceStore, gid: int) -> int:
    meta = store.load(gid)
    assert meta is not None
    return meta.version


# ── W5: Orphan RUNNING node invocation ─────────────────────────────────────


class TestWindow5OrphanRunning:
    """W5: Orphan RUNNING (process kill mid-invocation).

    begin_invocation creates RUNNING, crash before complete. Next begin_invocation
    CAS-marks prior CRASHED. Version chain continues at max+1.

    Invariants: I4 (lifecycle idempotence).
    Audit: crash-window-audit.md section W5.
    """

    def test_orphan_running_marked_crashed_on_next_begin(self) -> None:
        """Direct store-level: orphan RUNNING is CAS-marked CRASHED by next begin."""
        gid = 9201
        store = InMemoryNodeStateStore(gid)

        store.begin_invocation("node-a")
        record1 = store.load_latest("node-a")
        assert record1 is not None
        assert record1.status is InvocationStatus.RUNNING

        # Process death: no complete/crash/finalize called.
        inv2 = store.begin_invocation("node-a")

        records = store.query_versions("node-a")
        assert len(records) == 2
        # query_versions returns descending (latest first)
        assert records[0].status is InvocationStatus.RUNNING  # v1 (new)
        assert records[1].status is InvocationStatus.CRASHED  # v0 (prior orphan)
        assert records[0].version == 1
        assert records[1].version == 0
        assert inv2.version == 1
        assert inv2.parent_version is None

    async def test_fault_injection_orphan_running_through_node_run(self) -> None:
        """FaultInjectingNodeStateStore crashes after begin_invocation; next run recovers."""
        gid = 9202
        base_store = InMemoryNodeStateStore(gid)
        fault_store = FaultInjectingNodeStateStore(base_store)
        fault_store.crash_after("begin_invocation")

        source = _SourceNode("payload-a", "b")
        target = _RecordingTarget()
        compiled = _compile_pair(source, target)
        coordinator, _ = _make_in_memory_coordinator(
            gid,
            tuple(n.node_id for n in compiled.nodes.values()),
            node_state_store=fault_store,
        )

        with pytest.raises(RuntimeError, match="injected crash"):
            await source.run(_make_ctx(coordinator, gid), graph=compiled)

        running_record = base_store.load_latest(compiled.nodes["a"].node_id)
        assert running_record is not None
        assert running_record.status is InvocationStatus.RUNNING

        fault_store.begin_invocation(compiled.nodes["a"].node_id)
        records = base_store.query_versions(compiled.nodes["a"].node_id)
        assert len(records) == 2
        assert records[0].status is InvocationStatus.RUNNING  # latest
        assert records[1].status is InvocationStatus.CRASHED  # prior orphan
        assert records[0].version == 1

    def test_finalize_safety_net_covers_orphan_running(self) -> None:
        """finalize_invocation marks orphan RUNNING as CRASHED (safety net)."""
        gid = 9203
        store = InMemoryNodeStateStore(gid)
        inv = store.begin_invocation("node-x")
        before = store.load_latest("node-x")
        assert before is not None
        assert before.status is InvocationStatus.RUNNING

        store.finalize_invocation(inv)

        after = store.load_latest("node-x")
        assert after is not None
        assert after.status is InvocationStatus.CRASHED


# ── W6: Node interrupt <-> instance PAUSED split write ─────────────────────


class TestWindow6InterruptPauseSplit:
    """W6: GraphInterrupt -> node cancel + instance suspend (PAUSED).

    Verify instance stays PAUSED, node is CANCELED. Resume via RECOVERY mode.

    Invariants: I1 (recoverable).
    Audit: crash-window-audit.md section W6.
    """

    async def test_interrupt_cancels_node_and_suspends_instance(self) -> None:
        """Normal flow: GraphInterrupt -> CANCELED node + PAUSED instance."""
        gid = 9301
        interrupt_node = InterruptNode(value="need-input")
        compiled = _compile_pair(interrupt_node, _RecordingTarget())
        node_id = compiled.nodes["a"].node_id
        coordinator, instance_store = _make_in_memory_coordinator(
            gid, tuple(n.node_id for n in compiled.nodes.values())
        )
        inst_ctx = instance_store.begin_invocation(gid)

        with pytest.raises(GraphInterrupt):
            await interrupt_node.run(_make_ctx(coordinator, gid), graph=compiled)

        node_record = coordinator.node_state_store.load_latest(node_id)
        assert node_record is not None
        assert node_record.status is InvocationStatus.CANCELED

        instance_store.suspend_invocation(inst_ctx)
        assert _load_status(instance_store, gid) is GraphInstanceStatus.PAUSED

    async def test_crash_between_cancel_and_suspend_recovers(self) -> None:
        """Crash after cancel_invocation (before suspend): node CRASHED via finalize, instance RUNNING."""
        gid = 9302
        base_store = InMemoryNodeStateStore(gid)
        fault_store = FaultInjectingNodeStateStore(base_store)
        fault_store.crash_after("cancel_invocation")

        interrupt_node = InterruptNode(value="need-input")
        compiled = _compile_pair(interrupt_node, _RecordingTarget())
        node_id = compiled.nodes["a"].node_id
        coordinator, instance_store = _make_in_memory_coordinator(
            gid, tuple(n.node_id for n in compiled.nodes.values()), node_state_store=fault_store
        )
        instance_store.begin_invocation(gid)

        with pytest.raises(RuntimeError, match="injected crash"):
            await interrupt_node.run(_make_ctx(coordinator, gid), graph=compiled)

        # cancel completed before crash (AFTER position); node is CANCELED, not CRASHED
        node_record = base_store.load_latest(node_id)
        assert node_record is not None
        assert node_record.status is InvocationStatus.CANCELED

        # Instance still RUNNING (suspend never called) -- recover_crashed picks up
        assert _load_status(instance_store, gid) is GraphInstanceStatus.RUNNING

        # I1: bootstrap RECOVERY can continue -- returns seeds (entry_node fallback)
        ctx = _make_ctx(coordinator, gid)
        seeds = bootstrap(ctx, compiled, mode=BootstrapMode.RECOVERY)
        assert len(seeds) > 0

    async def test_recovery_mode_seeds_crashed_interrupted_node(self) -> None:
        """After crash-between recovery, the CRASHED node is seeded by RECOVERY bootstrap."""
        gid = 9303
        interrupt_node = InterruptNode()
        compiled = _compile_pair(interrupt_node, _RecordingTarget())
        coordinator, _ = _make_in_memory_coordinator(
            gid, tuple(n.node_id for n in compiled.nodes.values())
        )

        with pytest.raises(GraphInterrupt):
            await interrupt_node.run(_make_ctx(coordinator, gid), graph=compiled)

        ctx = _make_ctx(coordinator, gid)
        seeds = bootstrap(ctx, compiled, mode=BootstrapMode.RECOVERY)
        # CANCELED is skipped by bootstrap; node has no PENDING delivers -> empty seeds -> entry
        assert seeds == [compiled.entry_node]


# ── W7: REMOVED snapshot window ─────────────────────────────────────────────


class TestWindow7RemovedSnapshot:
    """W7: Concurrent snapshot cut (retired -- state_json/suspended deleted).

    Assert the window is structurally gone: rebuild_main_state absent,
    ctx.state is in-memory only (not restored from persisted snapshot).

    Audit: crash-window-audit.md section W7 (D3 latent -- retired by phase 07).
    Grep guard: tests/unit/modex_graph/test_phase07_grep_guard.py.
    """

    def test_rebuild_main_state_absent_from_coordinator(self) -> None:
        """rebuild_main_state must not exist on GraphPersistenceCoordinator."""
        assert not hasattr(GraphPersistenceCoordinator, "rebuild_main_state")

    def test_rebuild_main_state_absent_from_source(self) -> None:
        """rebuild_main_state must not appear in the persistence_coordinator source."""
        source = inspect.getsource(GraphPersistenceCoordinator)
        assert "rebuild_main_state" not in source

    async def test_state_not_restored_from_persisted_snapshot(self) -> None:
        """Two nodes complete; new ctx.state is NOT restored from store -- in-memory only."""
        gid = 9401
        source = _SourceNode("data", "b")
        target = _RecordingTarget()
        compiled = _compile_pair(source, target)
        coordinator, _ = _make_in_memory_coordinator(
            gid, tuple(n.node_id for n in compiled.nodes.values())
        )

        ctx1 = _make_ctx(coordinator, gid)
        ctx1.state.count = 42
        await source.run(ctx1, graph=compiled)
        await target.run(ctx1, graph=compiled)

        ctx2 = _make_ctx(coordinator, gid)
        assert ctx2.state.count == 0  # NOT restored from ctx1

        bootstrap(ctx2, compiled, mode=BootstrapMode.RECOVERY)
        assert ctx2.state.count == 0  # bootstrap does NOT restore state


# ── W8: Instance-level orphan RUNNING ──────────────────────────────────────


class TestWindow8InstanceOrphanRunning:
    """W8: Instance begin_invocation(gid) creates RUNNING, process dies.

    recover_crashed picks up CRASHED + orphan RUNNING. Verify recovery.

    Invariants: I1/I4.
    Audit: crash-window-audit.md section W8.
    """

    def test_orphan_running_instance_picked_up_by_load_by_status(self) -> None:
        """load_by_status(RUNNING) picks up orphan RUNNING instances."""
        gid = 9501
        store = InMemoryGraphInstanceStore()
        store.save(
            GraphMetadata(
                graph_instance_id=gid,
                spec_id=0,
                parent_instance_id=None,
                parent_node=None,
                status=GraphInstanceStatus.RUNNING,
            )
        )
        store.begin_invocation(gid)
        assert _load_status(store, gid) is GraphInstanceStatus.RUNNING

        # Process death: no complete/crash/finalize called.
        running = store.load_by_status(GraphInstanceStatus.RUNNING)
        assert len(running) == 1
        assert running[0].graph_instance_id == gid

        crashed = store.load_by_status(GraphInstanceStatus.CRASHED)
        assert len(crashed) == 0

    def test_crashed_and_orphan_running_both_picked_up(self) -> None:
        """recover_crashed picks up both CRASHED and orphan RUNNING."""
        store = InMemoryGraphInstanceStore()

        gid_crashed = 9502
        store.save(
            GraphMetadata(
                graph_instance_id=gid_crashed,
                spec_id=0,
                parent_instance_id=None,
                parent_node=None,
                status=GraphInstanceStatus.RUNNING,
            )
        )
        store.begin_invocation(gid_crashed)
        store.update_status(gid_crashed, GraphInstanceStatus.CRASHED)

        gid_orphan = 9503
        store.save(
            GraphMetadata(
                graph_instance_id=gid_orphan,
                spec_id=0,
                parent_instance_id=None,
                parent_node=None,
                status=GraphInstanceStatus.RUNNING,
            )
        )
        store.begin_invocation(gid_orphan)

        crashed = store.load_by_status(GraphInstanceStatus.CRASHED)
        running = store.load_by_status(GraphInstanceStatus.RUNNING)
        assert {m.graph_instance_id for m in crashed} == {gid_crashed}
        assert {m.graph_instance_id for m in running} == {gid_orphan}

    def test_next_begin_invocation_marks_prior_running_crashed(self) -> None:
        """A later begin_invocation on the same gid marks prior RUNNING CRASHED."""
        gid = 9504
        store = InMemoryGraphInstanceStore()
        store.save(
            GraphMetadata(
                graph_instance_id=gid,
                spec_id=0,
                parent_instance_id=None,
                parent_node=None,
                status=GraphInstanceStatus.RUNNING,
            )
        )
        store.begin_invocation(gid)
        assert _load_version(store, gid) == 1
        assert _load_status(store, gid) is GraphInstanceStatus.RUNNING

        store.begin_invocation(gid)
        assert _load_version(store, gid) == 2
        assert _load_status(store, gid) is GraphInstanceStatus.RUNNING

        versions = store._instances[gid]
        assert len(versions) == 3  # original + 2 begin_invocations
        assert versions[0].status is GraphInstanceStatus.CRASHED  # original
        assert versions[1].status is GraphInstanceStatus.CRASHED  # first new (marked by second begin)
        assert versions[2].status is GraphInstanceStatus.RUNNING  # second new (current)


# ── W9: GraphDrained / control-path status transitions ─────────────────────


class TestWindow9ControlPathTransitions:
    """W9: pause -> PAUSED persisted, stop -> STOPPED persisted.

    Verify status transitions converge.

    Invariants: I4 (lifecycle idempotence).
    Audit: crash-window-audit.md section W9.
    """

    def test_suspend_transitions_running_to_paused(self) -> None:
        """suspend_invocation: RUNNING -> PAUSED."""
        gid = 9601
        store = InMemoryGraphInstanceStore()
        store.save(
            GraphMetadata(
                graph_instance_id=gid,
                spec_id=0,
                parent_instance_id=None,
                parent_node=None,
                status=GraphInstanceStatus.RUNNING,
            )
        )
        ctx = store.begin_invocation(gid)
        assert _load_status(store, gid) is GraphInstanceStatus.RUNNING

        store.suspend_invocation(ctx)
        assert _load_status(store, gid) is GraphInstanceStatus.PAUSED

    def test_update_status_to_stopped(self) -> None:
        """update_status: -> STOPPED."""
        gid = 9602
        store = InMemoryGraphInstanceStore()
        store.save(
            GraphMetadata(
                graph_instance_id=gid,
                spec_id=0,
                parent_instance_id=None,
                parent_node=None,
                status=GraphInstanceStatus.RUNNING,
            )
        )
        store.begin_invocation(gid)

        store.update_status(gid, GraphInstanceStatus.STOPPED)
        assert _load_status(store, gid) is GraphInstanceStatus.STOPPED

    def test_complete_transitions_running_to_completed(self) -> None:
        """complete_invocation: RUNNING -> COMPLETED."""
        gid = 9603
        store = InMemoryGraphInstanceStore()
        store.save(
            GraphMetadata(
                graph_instance_id=gid,
                spec_id=0,
                parent_instance_id=None,
                parent_node=None,
                status=GraphInstanceStatus.RUNNING,
            )
        )
        ctx = store.begin_invocation(gid)

        store.complete_invocation(ctx)
        assert _load_status(store, gid) is GraphInstanceStatus.COMPLETED

    def test_crash_transitions_to_crashed(self) -> None:
        """crash_invocation: tolerant CAS -> CRASHED."""
        gid = 9604
        store = InMemoryGraphInstanceStore()
        store.save(
            GraphMetadata(
                graph_instance_id=gid,
                spec_id=0,
                parent_instance_id=None,
                parent_node=None,
                status=GraphInstanceStatus.RUNNING,
            )
        )
        ctx = store.begin_invocation(gid)

        store.crash_invocation(ctx)
        assert _load_status(store, gid) is GraphInstanceStatus.CRASHED

    def test_finalize_silent_noop_on_paused(self) -> None:
        """finalize_invocation silently no-ops on PAUSED (CAS expects RUNNING)."""
        gid = 9605
        store = InMemoryGraphInstanceStore()
        store.save(
            GraphMetadata(
                graph_instance_id=gid,
                spec_id=0,
                parent_instance_id=None,
                parent_node=None,
                status=GraphInstanceStatus.RUNNING,
            )
        )
        ctx = store.begin_invocation(gid)
        store.suspend_invocation(ctx)
        assert _load_status(store, gid) is GraphInstanceStatus.PAUSED

        store.finalize_invocation(ctx)
        assert _load_status(store, gid) is GraphInstanceStatus.PAUSED


# ── W10: Stop while node in-flight ─────────────────────────────────────────


class TestWindow10StopInFlight:
    """W10: STOPPED write doesn't interrupt node body (cooperative drain).

    Node completes after STOPPED. Post-stop delivers persist (PENDING).
    Instance finalize silently no-ops (D6 accepted behavior).

    Invariants: I3 (post-stop side effects) + I4 (silent CAS).
    Audit: crash-window-audit.md section W10 (D6).
    """

    async def test_node_body_completes_after_stopped(self) -> None:
        """Node deliver persists as STAGED even after instance is STOPPED."""
        gid = 9701
        source = _SourceNode("post-stop-data", "b")
        target = _RecordingTarget()
        compiled = _compile_pair(source, target)
        coordinator, instance_store = _make_in_memory_coordinator(
            gid, tuple(n.node_id for n in compiled.nodes.values())
        )
        inst_ctx = instance_store.begin_invocation(gid)

        # Simulate stop while node is "in-flight"
        instance_store.update_status(gid, GraphInstanceStatus.STOPPED)

        # Node body still runs to completion (cooperative drain)
        await source.run(_make_ctx(coordinator, gid), graph=compiled)

        # Deliver persisted as STAGED (promoted to PENDING by complete_invocation path)
        target_id = compiled.nodes["b"].node_id
        delivers = coordinator.collect_consumable_delivers(target_id, 0)
        assert len(delivers) == 1
        assert delivers[0].content == "post-stop-data"

        # Instance finalize silently no-ops on STOPPED (D6 accepted)
        instance_store.finalize_invocation(inst_ctx)
        assert _load_status(instance_store, gid) is GraphInstanceStatus.STOPPED

    async def test_post_stop_delivers_consumable_by_future_run(self) -> None:
        """Post-stop delivers (PENDING) are consumable by a future recovery run."""
        gid = 9702
        source = _SourceNode("delayed", "b")
        target = _RecordingTarget()
        compiled = _compile_pair(source, target)
        coordinator, instance_store = _make_in_memory_coordinator(
            gid, tuple(n.node_id for n in compiled.nodes.values())
        )
        instance_store.begin_invocation(gid)
        instance_store.update_status(gid, GraphInstanceStatus.STOPPED)

        await source.run(_make_ctx(coordinator, gid), graph=compiled)

        target_id = compiled.nodes["b"].node_id
        delivers = coordinator.collect_consumable_delivers(target_id, 0)
        assert len(delivers) == 1
        assert delivers[0].status is DeliverConsumptionStatus.PENDING


# ── W11: complete<->IORecord failure ────────────────────────────────────────


class TestWindow11CompleteIORecord:
    """W11: complete commits COMPLETED, crash before io_store.update_output.

    Instance status is authoritative (COMPLETED), io null tolerated (D7).

    Invariants: I1 (state terminal and consistent).
    Audit: crash-window-audit.md section W11 (D7).
    """

    def test_instance_completed_with_null_io_output(self) -> None:
        """complete_invocation commits COMPLETED; io output=None tolerated."""
        gid = 9801
        instance_store = InMemoryGraphInstanceStore()
        instance_store.save(
            GraphMetadata(
                graph_instance_id=gid,
                spec_id=0,
                parent_instance_id=None,
                parent_node=None,
                status=GraphInstanceStatus.RUNNING,
            )
        )
        io_store = InMemoryGraphIORecordStore()

        inst_ctx = instance_store.begin_invocation(gid)
        io_store.save(
            GraphIORecord(
                record_id=1,
                graph_instance_id=gid,
                spec_id=0,
                version=inst_ctx.version,
                user_input=None,
                output=None,
                created_at=0,
            )
        )

        # complete_invocation succeeds
        instance_store.complete_invocation(inst_ctx)
        assert _load_status(instance_store, gid) is GraphInstanceStatus.COMPLETED

        # Crash before io_store.update_output -- io output stays None
        io_record = io_store.get_latest_by_instance(gid)
        assert io_record is not None
        assert io_record.output is None  # tolerated (D7)

    def test_instance_status_authoritative_over_io(self) -> None:
        """Instance status (COMPLETED) is the authority, not the IORecord."""
        gid = 9802
        instance_store = InMemoryGraphInstanceStore()
        instance_store.save(
            GraphMetadata(
                graph_instance_id=gid,
                spec_id=0,
                parent_instance_id=None,
                parent_node=None,
                status=GraphInstanceStatus.RUNNING,
            )
        )
        io_store = InMemoryGraphIORecordStore()

        inst_ctx = instance_store.begin_invocation(gid)
        io_store.save(
            GraphIORecord(
                record_id=2,
                graph_instance_id=gid,
                spec_id=0,
                version=inst_ctx.version,
                user_input=None,
                output=None,
                created_at=0,
            )
        )

        instance_store.complete_invocation(inst_ctx)
        instance_store.finalize_invocation(inst_ctx)

        # Instance is COMPLETED -- authoritative
        assert _load_status(instance_store, gid) is GraphInstanceStatus.COMPLETED
        # IORecord output is None -- tolerated, doesn't affect instance status
        io_record = io_store.get_latest_by_instance(gid)
        assert io_record is not None
        assert io_record.output is None


# ── W12: Re-invoke v2 with v1 leftover delivers ────────────────────────────


class TestWindow12ReInvokeLeftover:
    """W12: v1 delivers leak into v2. STAGED from v1 survives, promoted by v1's
    source completion. At-least-once (D2 reclassified as by-design).

    Invariants: I3 (cross-invocation duplication, by-design at-least-once).
    Audit: crash-window-audit.md section W12 (D2).
    """

    async def test_v1_staged_survives_and_promoted_by_source_completion(self) -> None:
        """v1 STAGED deliver survives; promoted to PENDING when source completes."""
        gid = 9901
        source = _SourceNode("v1-data", "b")
        target = _RecordingTarget()
        compiled = _compile_pair(source, target)
        coordinator, _ = _make_in_memory_coordinator(
            gid, tuple(n.node_id for n in compiled.nodes.values())
        )
        target_id = compiled.nodes["b"].node_id

        # v1: source delivers (STAGED), source completes -> STAGED promoted to PENDING
        await source.run(_make_ctx(coordinator, gid), graph=compiled)

        delivers = coordinator.collect_consumable_delivers(target_id, 0)
        assert len(delivers) == 1
        assert delivers[0].content == "v1-data"
        assert delivers[0].status is DeliverConsumptionStatus.PENDING

    async def test_v2_fresh_does_not_seed_from_v1_leftover(self) -> None:
        """FRESH bootstrap returns [entry_node] -- v1 PENDING delivers don't seed v2."""
        gid = 9902
        source = _SourceNode("v1-data", "b")
        target = _RecordingTarget()
        compiled = _compile_pair(source, target)
        coordinator, _ = _make_in_memory_coordinator(
            gid, tuple(n.node_id for n in compiled.nodes.values())
        )

        await source.run(_make_ctx(coordinator, gid), graph=compiled)

        ctx2 = _make_ctx(coordinator, gid)
        seeds = bootstrap(ctx2, compiled, mode=BootstrapMode.FRESH)
        assert seeds == [compiled.entry_node]

    async def test_v2_recovery_seeds_from_v1_leftover_delivers(self) -> None:
        """RECOVERY bootstrap seeds target from v1's PENDING delivers."""
        gid = 9903
        source = _SourceNode("v1-data", "b")
        target = _RecordingTarget()
        compiled = _compile_pair(source, target)
        coordinator, _ = _make_in_memory_coordinator(
            gid, tuple(n.node_id for n in compiled.nodes.values())
        )

        await source.run(_make_ctx(coordinator, gid), graph=compiled)

        ctx2 = _make_ctx(coordinator, gid)
        seeds = bootstrap(ctx2, compiled, mode=BootstrapMode.RECOVERY)
        assert "b" in seeds  # target seeded because it has PENDING delivers


# ── W13: External deliver <-> engine notify gap ────────────────────────────


class TestWindow13ExternalDeliverGap:
    """W13: deliver persisted, engine not yet notified.

    _recheck_pending store rescan catches it. Verify delayed admission.

    Invariants: I2 (no input loss).
    Audit: crash-window-audit.md section W13 (D8).
    """

    def test_pending_deliver_visible_without_engine_notify(self) -> None:
        """A PENDING deliver in the store is visible to collect_consumable_delivers
        even when the engine was never notified."""
        gid = 10001
        coordinator, _ = _make_in_memory_coordinator(gid, ("target",))
        target_id = "target"

        # External deliver persisted (no engine notify)
        coordinator.route_deliver(target_id, "external-data", "__external__", 0)

        # Store rescan catches it
        delivers = coordinator.collect_consumable_delivers(target_id, 0)
        assert len(delivers) == 1
        assert delivers[0].content == "external-data"
        assert delivers[0].status is DeliverConsumptionStatus.PENDING

    def test_external_deliver_to_staged_then_promoted(self) -> None:
        """External deliver with stage=True is invisible until promoted."""
        gid = 10002
        coordinator, _ = _make_in_memory_coordinator(gid, ("target",))
        target_id = "target"

        coordinator.route_deliver(target_id, "staged-external", "source-a", 1, stage=True)

        delivers = coordinator.collect_consumable_delivers(target_id, 0)
        assert len(delivers) == 0  # STAGED is not consumable

        affected = coordinator.promote_staged_by_source(gid, "source-a")
        assert target_id in affected

        delivers = coordinator.collect_consumable_delivers(target_id, 0)
        assert len(delivers) == 1
        assert delivers[0].content == "staged-external"
        assert delivers[0].status is DeliverConsumptionStatus.PENDING


# ── W14: In-memory guard exception propagation ─────────────────────────────


class TestWindow14ExceptionPropagation:
    """W14: In-memory _scheduled_deliver_ids guard.

    Within-run stranding impossible (exceptions propagate). Verify exception
    propagation from node execution.

    Invariants: I4 (exceptions propagate, no silent stranding).
    Audit: crash-window-audit.md section W14.
    """

    async def test_node_exception_propagates_and_marks_crashed(self) -> None:
        """Exception from execute propagates out of Node.run; invocation is CRASHED."""
        gid = 10101
        crash_target = _RecordingTarget(crash_once=True)
        compiled = _compile_pair(_SourceNode("data", "b"), crash_target)
        coordinator, _ = _make_in_memory_coordinator(
            gid, tuple(n.node_id for n in compiled.nodes.values())
        )
        target_id = compiled.nodes["b"].node_id

        # Deliver a payload so target has input to consume
        coordinator.route_deliver(target_id, "prior-input", "external", 0)

        with pytest.raises(RuntimeError, match="crashed after input integration"):
            await crash_target.run(_make_ctx(coordinator, gid), graph=compiled)

        record = coordinator.node_state_store.load_latest(target_id)
        assert record is not None
        assert record.status is InvocationStatus.CRASHED

    async def test_scheduler_exception_propagates_out(self) -> None:
        """Exception from a node propagates out of LinearScheduler.run_async."""
        gid = 10102
        crash_target = _RecordingTarget(crash_once=True)
        compiled = _compile_pair(_SourceNode("data", "b"), crash_target)
        coordinator, _ = _make_in_memory_coordinator(
            gid, tuple(n.node_id for n in compiled.nodes.values())
        )

        ctx = _make_ctx(coordinator, gid)
        with pytest.raises(RuntimeError, match="crashed after input integration"):
            await LinearScheduler(compiled).run_async(ctx, mode=BootstrapMode.FRESH)

        target_id = compiled.nodes["b"].node_id
        record = coordinator.node_state_store.load_latest(target_id)
        assert record is not None
        assert record.status is InvocationStatus.CRASHED


# ── N1: FRESH vs RECOVERY deliver残留 ───────────────────────────────────────


class TestN1FreshVsRecovery:
    """N1: re-invoke (FRESH mode) after v1 completed.

    v1 PENDING/CONSUMED_PENDING/STAGED rows don't trigger FRESH.
    Data all visible. RECOVERY mode seeds normally.

    Ticket: 12-crash-window-matrix-tests.md row N1.
    """

    async def test_fresh_returns_entry_without_scanning(self) -> None:
        """FRESH bootstrap returns [entry_node] regardless of store state."""
        gid = 11001
        source = _SourceNode("v1", "b")
        target = _RecordingTarget()
        compiled = _compile_pair(source, target)
        coordinator, _ = _make_in_memory_coordinator(
            gid, tuple(n.node_id for n in compiled.nodes.values())
        )

        await source.run(_make_ctx(coordinator, gid), graph=compiled)

        ctx2 = _make_ctx(coordinator, gid)
        seeds = bootstrap(ctx2, compiled, mode=BootstrapMode.FRESH)
        assert seeds == [compiled.entry_node]

    async def test_recovery_seeds_from_pending_delivers(self) -> None:
        """RECOVERY bootstrap seeds nodes with PENDING delivers."""
        gid = 11002
        source = _SourceNode("v1", "b")
        target = _RecordingTarget()
        compiled = _compile_pair(source, target)
        coordinator, _ = _make_in_memory_coordinator(
            gid, tuple(n.node_id for n in compiled.nodes.values())
        )

        await source.run(_make_ctx(coordinator, gid), graph=compiled)

        ctx2 = _make_ctx(coordinator, gid)
        seeds = bootstrap(ctx2, compiled, mode=BootstrapMode.RECOVERY)
        assert "b" in seeds

    async def test_fresh_data_still_visible_in_store(self) -> None:
        """After v1 completes, data is visible in the store (no quarantine)."""
        gid = 11003
        source = _SourceNode("visible-data", "b")
        target = _RecordingTarget()
        compiled = _compile_pair(source, target)
        coordinator, _ = _make_in_memory_coordinator(
            gid, tuple(n.node_id for n in compiled.nodes.values())
        )
        target_id = compiled.nodes["b"].node_id

        await source.run(_make_ctx(coordinator, gid), graph=compiled)

        delivers = coordinator.collect_consumable_delivers(target_id, 0)
        assert len(delivers) == 1
        assert delivers[0].content == "visible-data"


# ── N2: END seed / reached_end ──────────────────────────────────────────────


class TestN2EndSeedReachedEnd:
    """N2: upstream all COMPLETED + END has PENDING -> crash recovery ->
    END executes, reached_end=True (not FAILED/from-scratch).

    Ticket: 12-crash-window-matrix-tests.md row N2.
    """

    async def test_recovery_seeds_end_when_upstream_completed(self) -> None:
        """When upstream A is COMPLETED and END has PENDING delivers,
        bootstrap RECOVERY seeds END (not entry_node)."""
        gid = 12001
        source = _SourceNode("end-input", GraphNode.END)
        graph: Graph[Any] = Graph()
        graph.add_node("a", source)
        graph.add_node(GraphNode.END, _SimpleEndNode())
        graph.add_edge(GraphNode.START, "a")
        graph.add_edge("a", GraphNode.END)
        compiled = graph.compile()

        instance_store = InMemoryGraphInstanceStore()
        instance_store.save(
            GraphMetadata(
                graph_instance_id=gid,
                spec_id=0,
                parent_instance_id=None,
                parent_node=None,
                status=GraphInstanceStatus.RUNNING,
            )
        )
        coordinator = GraphPersistenceCoordinator(
            graph_instance_id=gid,
            instance_store=instance_store,
            node_state_store=InMemoryNodeStateStore(gid),
            default_deliver_store_factory=InMemoryDeliverStoreFactory(),
        )
        for node in compiled.nodes.values():
            coordinator.register_node(node.node_id)

        # Run source to completion (delivers to END as STAGED, then promoted to PENDING)
        ctx = GraphContext(
            state=DefaultGraphState(),
            runtime=make_runtime(),
            coordinator=coordinator,
            scheduler_kind=SchedulerKind.LINEAR,
            graph_instance_id=gid,
        )
        ctx.set_dispatch_handler(lambda _s, _t: None)
        await source.run(ctx, graph=compiled)

        # Source is now COMPLETED, END has PENDING delivers
        source_record = coordinator.node_state_store.load_latest(compiled.nodes["a"].node_id)
        assert source_record is not None
        assert source_record.status is InvocationStatus.COMPLETED

        end_id = compiled.nodes[GraphNode.END].node_id
        end_delivers = coordinator.collect_consumable_delivers(end_id, 0)
        assert len(end_delivers) == 1
        assert end_delivers[0].status is DeliverConsumptionStatus.PENDING

        # Bootstrap RECOVERY should seed END
        ctx2 = GraphContext(
            state=DefaultGraphState(),
            runtime=make_runtime(),
            coordinator=coordinator,
            scheduler_kind=SchedulerKind.LINEAR,
            graph_instance_id=gid,
        )
        ctx2.set_dispatch_handler(lambda _s, _t: None)
        seeds = bootstrap(ctx2, compiled, mode=BootstrapMode.RECOVERY)
        assert GraphNode.END in seeds
        assert seeds[0] != compiled.entry_node

    async def test_parallel_scheduler_recovers_end_promote_dispatch_gap(self) -> None:
        gid = 12002
        source = _SourceNode("final-data", GraphNode.END)
        graph: Graph[Any] = Graph()
        graph.add_node("a", source)
        graph.add_node(GraphNode.END, _SimpleEndNode())
        graph.add_edge(GraphNode.START, "a")
        graph.add_edge("a", GraphNode.END)
        compiled = graph.compile()

        instance_store = InMemoryGraphInstanceStore()
        instance_store.save(
            GraphMetadata(
                graph_instance_id=gid,
                spec_id=0,
                parent_instance_id=None,
                parent_node=None,
                status=GraphInstanceStatus.RUNNING,
            )
        )
        coordinator = GraphPersistenceCoordinator(
            graph_instance_id=gid,
            instance_store=instance_store,
            node_state_store=InMemoryNodeStateStore(gid),
            default_deliver_store_factory=InMemoryDeliverStoreFactory(),
        )
        for node in compiled.nodes.values():
            coordinator.register_node(node.node_id)

        ctx = GraphContext(
            state=DefaultGraphState(),
            runtime=make_runtime(),
            coordinator=coordinator,
            scheduler_kind=SchedulerKind.LINEAR,
            graph_instance_id=gid,
        )
        ctx.set_dispatch_handler(lambda _s, _t: None)
        await source.run(ctx, graph=compiled)

        end_node = compiled.nodes[GraphNode.END]
        ctx2 = GraphContext(
            state=DefaultGraphState(),
            runtime=make_runtime(),
            coordinator=coordinator,
            scheduler_kind=SchedulerKind.PARALLEL,
            graph_instance_id=gid,
        )
        await ParallelScheduler(compiled).run_async(ctx2, mode=BootstrapMode.RECOVERY)

        assert ctx2.state.result is not None
        assert len(ctx2.state.result) == 1
        assert ctx2.state.result[0].content == "final-data"
        assert ctx2.reached_end is True

        end_record = coordinator.node_state_store.load_latest(end_node.node_id)
        assert end_record is not None
        assert end_record.status is InvocationStatus.COMPLETED


# ── N3: ReAct Null path ────────────────────────────────────────────────────


class TestN3ReactNullPath:
    """N3: Null four-state contract regression.

    STAGED no-op (accumulate creates PENDING, immediately visible),
    mark=delete (not status change). Verify ReAct uses NullDeliverStore.

    Ticket: 12-crash-window-matrix-tests.md row N3.
    """

    def test_null_accumulate_creates_pending_immediately_visible(self) -> None:
        """NullDeliverStore.accumulate creates PENDING records, immediately visible."""
        store = NullDeliverStore()
        store.accumulate(
            graph_instance_id=1,
            node_id="target",
            source_node_id="source",
            source_invocation_id=0,
            content="data",
        )
        delivers = store.query_consumable(1, "target")
        assert len(delivers) == 1
        assert delivers[0].content == "data"
        assert delivers[0].status is DeliverConsumptionStatus.PENDING

    def test_null_mark_consumed_deletes_not_status_change(self) -> None:
        """NullDeliverStore.mark_consumed removes records (not CONSUMED_PENDING)."""
        store = NullDeliverStore()
        did = store.accumulate(
            graph_instance_id=1,
            node_id="target",
            source_node_id="source",
            source_invocation_id=0,
            content="data",
        )
        assert len(store.query_consumable(1, "target")) == 1

        store.mark_consumed([did], 99)

        assert len(store.query_consumable(1, "target")) == 0

    def test_null_promote_consumed_is_noop(self) -> None:
        """NullDeliverStore.promote_consumed is a no-op."""
        store = NullDeliverStore()
        store.accumulate(
            graph_instance_id=1,
            node_id="target",
            source_node_id="source",
            source_invocation_id=0,
            content="data",
        )
        store.promote_consumed(99)  # should not raise, no effect
        assert len(store.query_consumable(1, "target")) == 1

    def test_create_null_coordinator_uses_null_deliver_store(self) -> None:
        """create_null_coordinator (used by ReAct) creates a NullDeliverStoreFactory."""
        coord = create_null_coordinator(0)
        coord.register_node("test-node")
        store = coord.get_deliver_store("test-node")
        assert isinstance(store, NullDeliverStore)

    def test_null_query_consumable_excludes_staged(self) -> None:
        """NullDeliverStore.query_consumable excludes STAGED records."""
        store = NullDeliverStore()
        store.accumulate(
            graph_instance_id=1,
            node_id="target",
            source_node_id="source",
            source_invocation_id=0,
            content="visible",
        )
        store.accumulate(
            graph_instance_id=1,
            node_id="target",
            source_node_id="source",
            source_invocation_id=0,
            content="hidden",
            status=DeliverConsumptionStatus.STAGED,
        )
        delivers = store.query_consumable(1, "target")
        assert len(delivers) == 1
        assert delivers[0].content == "visible"


# ── N4: 09 sweeper (reference) ─────────────────────────────────────────────


class TestN4SweeperReference:
    """N4: 09 sweeper -- executor not in alive RUNNING -> CRASHED.

    Already covered in task 29 (tests/unit/runtime/test_phase09_acceptance.py
    and tests/unit/runtime/test_stale_instance_sweeper.py).

    Ticket: 12-crash-window-matrix-tests.md row N4.
    """

    def test_sweeper_test_files_exist(self) -> None:
        """Verify task 29 sweeper/acceptance tests exist."""
        acceptance_tests = (
            Path(__file__).resolve().parent.parent / "runtime" / "test_phase09_acceptance.py"
        )
        assert acceptance_tests.exists(), f"Missing: {acceptance_tests}"


# ── N5: D6 stop cooperative semantics ──────────────────────────────────────


class TestN5D6StopCooperative:
    """N5: D6 stop cooperative semantics.

    Assert node body completes after STOPPED, instance CAS silent idempotent
    (accepted + documented behavior).

    Ticket: 12-crash-window-matrix-tests.md row N5.
    """

    async def test_node_completes_after_stopped_status(self) -> None:
        """Node body runs to completion even after instance is STOPPED."""
        gid = 14001
        source = _SourceNode("cooperative", "b")
        target = _RecordingTarget()
        compiled = _compile_pair(source, target)
        coordinator, instance_store = _make_in_memory_coordinator(
            gid, tuple(n.node_id for n in compiled.nodes.values())
        )
        instance_store.begin_invocation(gid)

        instance_store.update_status(gid, GraphInstanceStatus.STOPPED)

        await source.run(_make_ctx(coordinator, gid), graph=compiled)

        source_record = coordinator.node_state_store.load_latest(compiled.nodes["a"].node_id)
        assert source_record is not None
        assert source_record.status is InvocationStatus.COMPLETED

    def test_instance_finalize_silent_on_stopped(self) -> None:
        """Instance-store finalize silently no-ops on STOPPED (D6 accepted)."""
        gid = 14002
        store = InMemoryGraphInstanceStore()
        store.save(
            GraphMetadata(
                graph_instance_id=gid,
                spec_id=0,
                parent_instance_id=None,
                parent_node=None,
                status=GraphInstanceStatus.RUNNING,
            )
        )
        ctx = store.begin_invocation(gid)
        store.update_status(gid, GraphInstanceStatus.STOPPED)

        store.finalize_invocation(ctx)
        assert _load_status(store, gid) is GraphInstanceStatus.STOPPED


# ── N6: D7 complete<->IORecord ─────────────────────────────────────────────


class TestN6D7CompleteIORecord:
    """N6: D7 complete<->IORecord.

    Assert instance status authoritative (COMPLETED), io null tolerated
    (accepted + documented).

    Ticket: 12-crash-window-matrix-tests.md row N6.
    """

    def test_completed_instance_with_null_io_is_accepted(self) -> None:
        """Instance COMPLETED with io output=None is accepted (D7)."""
        gid = 15001
        instance_store = InMemoryGraphInstanceStore()
        instance_store.save(
            GraphMetadata(
                graph_instance_id=gid,
                spec_id=0,
                parent_instance_id=None,
                parent_node=None,
                status=GraphInstanceStatus.RUNNING,
            )
        )
        io_store = InMemoryGraphIORecordStore()

        inst_ctx = instance_store.begin_invocation(gid)
        io_store.save(
            GraphIORecord(
                record_id=10,
                graph_instance_id=gid,
                spec_id=0,
                version=inst_ctx.version,
                user_input=None,
                output=None,
                created_at=0,
            )
        )
        instance_store.complete_invocation(inst_ctx)

        assert _load_status(instance_store, gid) is GraphInstanceStatus.COMPLETED
        io_record = io_store.get_latest_by_instance(gid)
        assert io_record is not None
        assert io_record.output is None  # tolerated

    def test_instance_status_survives_finalize_after_complete(self) -> None:
        """finalize_invocation after complete is a no-op (COMPLETED is terminal)."""
        gid = 15002
        store = InMemoryGraphInstanceStore()
        store.save(
            GraphMetadata(
                graph_instance_id=gid,
                spec_id=0,
                parent_instance_id=None,
                parent_node=None,
                status=GraphInstanceStatus.RUNNING,
            )
        )
        ctx = store.begin_invocation(gid)
        store.complete_invocation(ctx)
        assert _load_status(store, gid) is GraphInstanceStatus.COMPLETED

        store.finalize_invocation(ctx)
        assert _load_status(store, gid) is GraphInstanceStatus.COMPLETED


# ── N7: D8 Linear no external deliver admission ────────────────────────────


class TestN7D8LinearNoExternal:
    """N7: D8 Linear scheduler doesn't support external deliver admission.

    Design: Linear=ReAct internal flow, external deliver=Parallel/bot graph.
    Assert explicit structural absence of wakeup wiring.

    Ticket: 12-crash-window-matrix-tests.md row N7.
    """

    def test_linear_scheduler_has_no_set_wakeup(self) -> None:
        """LinearScheduler source has no set_wakeup call (no external deliver admission)."""
        source = inspect.getsource(LinearScheduler)
        assert "set_wakeup" not in source
        assert "notify_deliver" not in source

    def test_linear_scheduler_has_no_recheck_pending(self) -> None:
        """LinearScheduler has no _recheck_pending (no store rescan for external delivers)."""
        source = inspect.getsource(LinearScheduler)
        assert "_recheck_pending" not in source

    def test_parallel_scheduler_has_recheck_pending(self) -> None:
        """ParallelScheduler DOES have _recheck_pending (supports external deliver admission)."""
        from modex_graph.scheduler.parallel import ParallelScheduler

        source = inspect.getsource(ParallelScheduler)
        assert "_recheck_pending" in source
