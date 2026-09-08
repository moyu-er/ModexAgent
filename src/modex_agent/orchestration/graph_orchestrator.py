# ruff: noqa: ANN401

"""Graph lifecycle owner: synchronous admission, execution, drain and recovery.

Every entry point reserves the same per-instance execution before scheduling a
task. Pause/stop signal that execution and shield their wait for its real exit;
only the owner finalizes status and releases runtime resources. Recovery selects
instances but never prewrites status or assembles a parallel execution path.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from typing import Any

from modex_agent.control.graph_control import GraphControlService
from modex_agent.control.graph_recovery import GraphRecoveryService
from modex_agent.control.types import ControlCommand, ControlCommandType, ControlScope
from modex_agent.runtime.constants import EXECUTOR_PROCESS_ID_KEY
from modex_agent.runtime.process_identity import ProcessIdentity
from modex_graph import (
    CoordinatorFactory,
    FieldSpec,
    GraphContext,
    GraphDrained,
    GraphEngine,
    GraphInstance,
    GraphInstanceStatus,
    GraphInstanceStore,
    GraphInterrupt,
    GraphIORecord,
    GraphIORecordStore,
    GraphMetadata,
    GraphNode,
    GraphOutput,
    GraphOutputAdapter,
    GraphOutputKind,
    GraphPayload,
    GraphPersistenceCoordinator,
    GraphRunControl,
    GraphRuntime,
    GraphSpec,
    GraphSpecCompiler,
    GraphSpecStore,
    GraphState,
    GraphStateSnapshot,
    InvocationStatus,
    NodeRegistry,
    NullCoordinatorFactory,
    NullGraphIORecordStore,
    default_id_generator,
)
from modex_graph.persistence._time import now_ms
from modex_graph.persistence.graph_metadata import GraphInvocationContext
from modex_graph.scheduler.bootstrap import (
    GRAPH_RUN_VERSION_KEY,
    BootstrapMode,
    graph_run_version,
)

logger = logging.getLogger(__name__)
_NULL_COORDINATOR_FACTORY = NullCoordinatorFactory()
_NULL_IO_STORE = NullGraphIORecordStore()
_TERMINAL = frozenset(
    {
        GraphInstanceStatus.COMPLETED,
        GraphInstanceStatus.FAILED,
        GraphInstanceStatus.CRASHED,
        GraphInstanceStatus.STOPPED,
    }
)


class _Execution:
    """One run's task, control and finalization resources, never request-owned."""

    task: asyncio.Task[None]

    def __init__(self, control: GraphRunControl) -> None:
        self.control = control
        self.context: GraphContext[Any] | None = None
        self.outcome: GraphInstanceStatus | None = None
        self.status_output: asyncio.Task[None] | None = None


class GraphOrchestrator:
    """Compile and execute graphs with one admitted execution per instance.

    ``start_run``, ``start_invoke`` and ``start_resume`` validate synchronously
    and return the owned task. ``run_instance`` and ``resume`` wait for execution.
    ``pause``/``stop`` wait for drain, not merely signal delivery. PAUSED runtimes
    retain their coordinator; after restart the same IDs/stores are reconstructed
    through the normal run assembly. No process liveness is inferred here.
    """

    def __init__(
        self,
        *,
        node_registry: NodeRegistry,
        state_classes: Mapping[str, type[GraphState]],
        spec_store: GraphSpecStore,
        instance_store: GraphInstanceStore,
        coordinator_factory: CoordinatorFactory = _NULL_COORDINATOR_FACTORY,
        output_adapter: GraphOutputAdapter | None = None,
        io_store: GraphIORecordStore = _NULL_IO_STORE,
        process_identity: ProcessIdentity | None = None,
        state_schema_compiler: Callable[[dict[str, FieldSpec]], type[GraphState]] | None = None,
    ) -> None:
        self._spec_store = spec_store
        self._instance_store = instance_store
        self._coordinator_factory = coordinator_factory
        self._output_adapter = output_adapter
        self._io_store = io_store
        self._process_identity = process_identity
        self._compiler = GraphSpecCompiler(
            node_registry,
            state_classes,
            state_schema_compiler=state_schema_compiler,
        )
        self._runtime = GraphRuntime()
        self._active_instances: dict[int, GraphInstance] = {}
        self._executions: dict[int, _Execution] = {}
        self._recovery_service = GraphRecoveryService(instance_store, self)
        self._control_service = GraphControlService(instance_store, self)

    async def create_instance(
        self,
        spec_id: int,
        *,
        parent_instance_id: int | None = None,
        user_input: GraphPayload | None = None,
    ) -> int:
        """Compile and persist a PENDING instance without executing it."""
        compiled = self._compiler.compile(self._load_spec(spec_id))
        gid = default_id_generator().generate()
        metadata = GraphMetadata(
            graph_instance_id=gid,
            spec_id=spec_id,
            parent_instance_id=parent_instance_id,
            parent_node=None,
            status=GraphInstanceStatus.PENDING,
            node_id_map={name: node.node_id for name, node in compiled.nodes.items()},
        )
        self._instance_store.save(metadata)
        coordinator = self._coordinator_factory.create(gid, self._instance_store)
        self._attach_output_adapter(coordinator)
        for node_id in metadata.node_id_map.values():
            coordinator.register_node(node_id)
        self._active_instances[gid] = GraphInstance(
            metadata,
            coordinator,
            compiled=compiled,
            user_input=user_input,
        )
        return gid

    def _metadata(self, gid: int) -> GraphMetadata:
        latest = self._instance_store.load(gid)
        if latest is None:
            instance = self._active_instances.get(gid)
            if instance is not None:
                latest = instance.metadata
        if latest is None:
            raise ValueError(f"Graph instance {gid} not found in store.")
        return latest

    def _require_idle(self, gid: int) -> None:
        if gid in self._executions:
            raise ValueError(f"Graph instance {gid} is already running or draining.")

    def _start_execution(
        self,
        gid: int,
        *,
        user_input: GraphPayload | None,
        mode: BootstrapMode,
    ) -> asyncio.Task[None]:
        self._require_idle(gid)
        metadata = self._metadata(gid)
        allowed = (
            {GraphInstanceStatus.PAUSED, GraphInstanceStatus.CRASHED}
            if mode is BootstrapMode.RECOVERY
            else {
                GraphInstanceStatus.PENDING,
                GraphInstanceStatus.COMPLETED,
                GraphInstanceStatus.FAILED,
                GraphInstanceStatus.CRASHED,
            }
        )
        if metadata.status not in allowed:
            raise ValueError(f"Cannot run instance {gid}: status is {metadata.status.value}.")
        if (
            mode is BootstrapMode.FRESH
            and metadata.status is GraphInstanceStatus.PENDING
            and user_input is None
        ):
            existing = self._active_instances.get(gid)
            if existing is not None:
                user_input = existing.user_input
        # No await between admission and reservation, including the create_task gap.
        invocation = self._instance_store.begin_invocation(gid)
        execution = _Execution(GraphRunControl())
        self._executions[gid] = execution
        # Enter try/finally before exposing the task to immediate cancellation.
        # The first suspension precedes engine execution and waits for RUNNING.
        task = asyncio.Task(
            self._execute(gid, invocation, user_input=user_input, mode=mode),
            loop=asyncio.get_running_loop(),
            eager_start=True,
        )
        execution.task = task
        task.add_done_callback(lambda finished: self._release_execution(gid, finished))
        return task

    def _release_execution(self, gid: int, task: asyncio.Task[None]) -> None:
        execution = self._executions.get(gid)
        if execution is not None and execution.task is task:
            self._executions.pop(gid)
        # Background REST callers need not await the task. Retrieving its exception
        # prevents unobserved-task warnings; awaiting the returned task still raises.
        if not task.cancelled():
            task.exception()

    async def run_instance(
        self,
        graph_instance_id: int,
        *,
        user_input: GraphPayload | None = None,
        mode: BootstrapMode,
    ) -> None:
        """Admit through the same guard as background starts, then wait for the run."""
        await self._start_execution(graph_instance_id, user_input=user_input, mode=mode)

    async def _execute(
        self,
        gid: int,
        invocation: GraphInvocationContext,
        *,
        user_input: GraphPayload | None,
        mode: BootstrapMode,
    ) -> None:
        execution = self._executions[gid]
        status = GraphInstanceStatus.CRASHED
        output: GraphOutput | None = None
        try:
            latest = self._metadata(gid)
            existing = self._active_instances.get(gid)
            record_id = default_id_generator().generate()
            run_version = (
                invocation.version if mode is BootstrapMode.FRESH else graph_run_version(latest)
            )
            attrs: dict[str, int | str | None] = {}
            if mode is BootstrapMode.FRESH:
                attrs[GRAPH_RUN_VERSION_KEY] = run_version
            if self._process_identity is not None:
                attrs[EXECUTOR_PROCESS_ID_KEY] = self._process_identity.process_id
            if attrs:
                self._instance_store.update_attrs(gid, attrs)
                latest = latest.model_copy(update={"attrs": {**latest.attrs, **attrs}})
            prior_io = (
                self._io_store.get_latest_by_instance(gid)
                if mode is BootstrapMode.RECOVERY
                else None
            )
            if prior_io is not None and prior_io.graph_run_version != run_version:
                prior_io = None
            if user_input is None and mode is BootstrapMode.RECOVERY:
                if prior_io is not None:
                    user_input = prior_io.user_input
                elif existing is not None and graph_run_version(existing.metadata) == run_version:
                    user_input = existing.user_input
            if existing is not None:
                existing.metadata = latest
                existing.user_input = user_input
            io_record = GraphIORecord(
                record_id=record_id,
                graph_instance_id=gid,
                spec_id=latest.spec_id,
                version=invocation.version,
                graph_run_version=run_version,
                user_input=user_input,
                output=prior_io.output if prior_io is not None else None,
                created_at=now_ms(),
            )
            self._io_store.save(io_record)
            # Fresh identity and scoped IO are durable before fallible assembly
            # or suspension. Recovery placeholders carry the same run's result.
            running_output = self._set_status(gid, GraphInstanceStatus.RUNNING)
            spec = self._load_spec(latest.spec_id)
            compiled = self._compiler.compile(spec)
            coordinator = (
                existing.coordinator
                if existing is not None
                else self._coordinator_factory.create(gid, self._instance_store)
            )
            self._attach_output_adapter(coordinator)
            for node in compiled.nodes.values():
                node.node_id = latest.node_id_map[node.name]
                if coordinator.get_deliver_store(node.node_id) is None:
                    coordinator.register_node(node.node_id)
            state = (
                existing.initial_state
                if existing is not None and existing.initial_state is not None
                else self._create_state(spec)
            )
            instance = existing if existing is not None else GraphInstance(latest, coordinator)
            instance.metadata = self._metadata(gid)
            instance.compiled = compiled
            instance.user_input = user_input
            instance.initial_state = None
            self._active_instances[gid] = instance
            ctx = GraphContext(
                state=state,
                runtime=self._runtime,
                coordinator=coordinator,
                user_input=user_input,
                graph_instance_id=gid,
                control=execution.control,
                graph_run_version=run_version,
            )
            execution.context = ctx
            end_before = coordinator.node_state_store.load_latest(latest.node_id_map[GraphNode.END])
            await asyncio.shield(running_output)
            final_state: GraphState | None = None
            try:
                final_state = await GraphEngine(compiled).run_async(ctx, mode=mode)
            except GraphDrained:
                status = GraphInstanceStatus.PAUSED
            else:
                status = (
                    GraphInstanceStatus.COMPLETED if ctx.reached_end else GraphInstanceStatus.FAILED
                )
            finally:
                result = dict(final_state if final_state is not None else ctx.state).get("result")
                end_after = coordinator.node_state_store.load_latest(
                    latest.node_id_map[GraphNode.END]
                )
                if (
                    mode is BootstrapMode.RECOVERY
                    and ctx.reached_end
                    and end_before is not None
                    and end_before.graph_run_version == run_version
                    and end_before.status is InvocationStatus.COMPLETED
                    and end_before == end_after
                    and prior_io is not None
                ):
                    # END is already durable; never replay it just to rebuild output.
                    result = prior_io.output
                if status in {GraphInstanceStatus.COMPLETED, GraphInstanceStatus.FAILED} or (
                    end_after is not None
                    and end_after.graph_run_version == run_version
                    and end_after.status is InvocationStatus.COMPLETED
                    and end_after != end_before
                ):
                    # Preserve newly completed END even if pause/cancel won the
                    # scheduler wakeup before normal graph completion was observed.
                    self._io_store.update_output(
                        io_record.record_id,
                        result if isinstance(result, list) else None,
                    )
            if status is not GraphInstanceStatus.PAUSED:
                output = GraphOutput(
                    kind=GraphOutputKind.COMPLETED if ctx.reached_end else GraphOutputKind.FAILED,
                    graph_instance_id=gid,
                    result=result,
                    timestamp=now_ms(),
                )
        except GraphInterrupt:
            status = GraphInstanceStatus.PAUSED
            raise
        except asyncio.CancelledError:
            status = GraphInstanceStatus.CRASHED
            output = GraphOutput(
                kind=GraphOutputKind.CRASHED,
                graph_instance_id=gid,
                error="Graph execution cancelled",
                timestamp=now_ms(),
            )
            raise
        except Exception as exc:
            status = GraphInstanceStatus.CRASHED
            output = GraphOutput(
                kind=GraphOutputKind.CRASHED,
                graph_instance_id=gid,
                error=str(exc),
                timestamp=now_ms(),
            )
            raise
        finally:
            execution.outcome = status
            try:
                await execution.control.wait_for_settlement(
                    asyncio.create_task(
                        self._finalize_instance(gid, status, output=output, invocation=invocation),
                    )
                )
            finally:
                execution.context = None

    def start_run(
        self,
        graph_instance_id: int,
        *,
        user_input: GraphPayload | None = None,
    ) -> asyncio.Task[None]:
        """Synchronously admit a fresh execution and return its retained task."""
        return self._start_execution(
            graph_instance_id, user_input=user_input, mode=BootstrapMode.FRESH
        )

    def start_invoke(
        self,
        graph_instance_id: int,
        *,
        user_input: GraphPayload | None = None,
    ) -> asyncio.Task[None]:
        """Start a fresh version of a completed/failed/crashed instance."""
        latest = self._metadata(graph_instance_id)
        if latest.status not in {
            GraphInstanceStatus.COMPLETED,
            GraphInstanceStatus.FAILED,
            GraphInstanceStatus.CRASHED,
        }:
            raise ValueError(
                f"Graph instance {graph_instance_id}: only completed/failed/crashed can be re-invoked."
            )
        return self.start_run(graph_instance_id, user_input=user_input)

    def start_resume(self, graph_instance_id: int) -> asyncio.Task[None]:
        """Validate PAUSED and reserve recovery synchronously, before returning a task."""
        self._require_idle(graph_instance_id)
        metadata = self._metadata(graph_instance_id)
        if metadata.status is GraphInstanceStatus.STOPPED:
            raise ValueError("STOPPED is a terminal status; only PAUSED instances can be resumed.")
        if metadata.status is not GraphInstanceStatus.PAUSED:
            raise ValueError(
                f"Cannot resume instance {graph_instance_id}: only PAUSED instances can be resumed."
            )
        return self._start_execution(
            graph_instance_id, user_input=None, mode=BootstrapMode.RECOVERY
        )

    async def resume(self, graph_instance_id: int) -> None:
        """Resume the same instance and wait for graph completion (or propagate failure)."""
        await self.start_resume(graph_instance_id)

    async def create_and_run(
        self,
        spec_id: int,
        *,
        initial_state: GraphState | None = None,
        parent_instance_id: int | None = None,
        user_input: GraphPayload | None = None,
    ) -> int:
        """Create an instance and wait for its admitted execution."""
        gid = await self.create_instance(
            spec_id,
            parent_instance_id=parent_instance_id,
            user_input=user_input,
        )
        self._active_instances[gid].initial_state = initial_state
        await self.start_run(gid, user_input=user_input)
        return gid

    def get_graph_context(self, graph_instance_id: int) -> GraphContext[Any] | None:
        """Return the live context, including node artifacts, until finalization exits."""
        execution = self._executions.get(graph_instance_id)
        return execution.context if execution is not None else None

    def get_state(self, graph_instance_id: int) -> GraphStateSnapshot:
        """Read authoritative persisted metadata and node histories."""
        instance = self._active_instances.get(graph_instance_id)
        if instance is not None:
            return instance.get_state()
        metadata = self._metadata(graph_instance_id)
        coordinator = self._coordinator_factory.create(graph_instance_id, self._instance_store)
        try:
            for node_id in metadata.node_id_map.values():
                coordinator.register_node(node_id)
            return coordinator.get_graph_state()
        finally:
            coordinator.close()

    async def pause(self, graph_instance_id: int) -> None:
        """Request immediate pause and wait, shielded, for the owner's full drain."""
        await self._drain(graph_instance_id, stop=False)

    async def stop(self, graph_instance_id: int) -> None:
        """Request terminal stop, upgrading any in-progress pause, and await drain."""
        await self._drain(graph_instance_id, stop=True)

    async def _drain(self, gid: int, *, stop: bool) -> None:
        metadata = self._metadata(gid)
        execution = self._executions.get(gid)
        if execution is not None and execution.task.done():
            execution = None
        if execution is None:
            if not stop and metadata.status is GraphInstanceStatus.PAUSED:
                return
            if stop and metadata.status is GraphInstanceStatus.STOPPED:
                return
            if stop and metadata.status is GraphInstanceStatus.PAUSED:
                control = GraphRunControl()
                control.request_stop("external stop")
                task = asyncio.create_task(
                    self._finalize_instance(gid, GraphInstanceStatus.STOPPED)
                )
                execution = _Execution(control)
                execution.task = task
                self._executions[gid] = execution
                task.add_done_callback(lambda finished: self._release_execution(gid, finished))
                self._set_status(gid, GraphInstanceStatus.STOPPING)
            else:
                raise ValueError(
                    f"Cannot {'stop' if stop else 'pause'} instance {gid}: "
                    "no local execution owner; must be RUNNING or PAUSED."
                )
        elif execution.outcome not in _TERMINAL:
            if stop and not execution.control.stop_requested:
                self._set_status(gid, GraphInstanceStatus.STOPPING)
                execution.control.request_stop("external stop")
            elif (
                not stop
                and execution.outcome is None
                and not (execution.control.pause_requested or execution.control.stop_requested)
            ):
                self._set_status(gid, GraphInstanceStatus.PAUSING)
                execution.control.request_pause("external pause")
        # Cancelling an HTTP waiter must not issue a second cancellation into
        # node/session cleanup, or cancel the no-engine finalization task.
        await asyncio.shield(execution.task)

    def _set_status(self, gid: int, status: GraphInstanceStatus) -> asyncio.Task[None]:
        self._instance_store.update_status(gid, status)
        instance = self._active_instances.get(gid)
        if instance is not None:
            instance.metadata = instance.metadata.model_copy(update={"status": status})
        execution = self._executions[gid]
        previous = execution.status_output

        async def emit_status() -> None:
            if previous is not None:
                await previous
            await self._emit(
                GraphOutput(
                    kind=GraphOutputKind.STATUS_CHANGED,
                    graph_instance_id=gid,
                    status=status,
                    timestamp=now_ms(),
                )
            )

        execution.status_output = asyncio.create_task(emit_status())
        return execution.status_output

    async def _emit(self, output: GraphOutput) -> None:
        if self._output_adapter is not None:
            try:
                await self._output_adapter.emit(output)
            except Exception:
                logger.warning(
                    "Output emit failed for instance %s", output.graph_instance_id, exc_info=True
                )

    async def _finalize_instance(
        self,
        graph_instance_id: int,
        status: GraphInstanceStatus,
        *,
        output: GraphOutput | None = None,
        invocation: GraphInvocationContext | None = None,
    ) -> None:
        gid = graph_instance_id
        execution = self._executions[gid]
        instance = self._active_instances.get(gid)
        if instance is not None:
            await instance.coordinator.drain_output_events()
        if execution.status_output is not None:
            await execution.status_output
        if status is GraphInstanceStatus.PAUSED and execution.control.stop_requested:
            status = GraphInstanceStatus.STOPPED
        execution.outcome = status
        if status in {GraphInstanceStatus.PAUSED, GraphInstanceStatus.STOPPED}:
            self._set_status(gid, status)
        else:
            self._instance_store.update_status(gid, status)
        if invocation is not None:
            self._instance_store.finalize_invocation(invocation)
        if execution.status_output is not None:
            await execution.status_output
        # Stop can arrive while PAUSED emission is still draining. The owner
        # remains reserved, so it must settle the upgrade before releasing it.
        if status is GraphInstanceStatus.PAUSED and execution.control.stop_requested:
            status = GraphInstanceStatus.STOPPED
            execution.outcome = status
            await self._set_status(gid, status)
        if output is not None:
            await self._emit(output)
        if status in _TERMINAL:
            self._evict_instance(gid)

    async def deliver_to_node(self, graph_instance_id: int, node_name: str, content: Any) -> None:
        """Persist external delivery via the unchanged coordinator routing contract."""
        await self._control_service.handle(
            ControlCommand(
                command_id=f"orchestrator-{graph_instance_id}-deliver",
                type=ControlCommandType.DELIVER_TO_NODE,
                scope=ControlScope(session_id="_orchestrator", graph_instance_id=graph_instance_id),
                payload={"node_name": node_name, "content": content},
            )
        )

    def _notify_deliver(self, gid: int, node_name: str) -> None:
        execution = self._executions.get(gid)
        if execution is not None:
            execution.control.notify_deliver(node_name)

    async def recover_crashed(self) -> list[int]:
        """Recover explicit CRASHED instances only; never infer remote process death."""
        return await self._recovery_service.recover_crashed()

    async def _run_existing_instance(self, graph_instance_id: int) -> None:
        """Recovery entry delegates to normal admission and assembly, without eviction."""
        await self.run_instance(graph_instance_id, mode=BootstrapMode.RECOVERY)

    async def pause_all_active(self) -> None:
        """Drain every locally owned execution, including admitted and transitioning runs."""
        await asyncio.gather(*(self.pause(gid) for gid in tuple(self._executions)))

    async def cleanup(self) -> None:
        """Drain owners before releasing their coordinators; never cancel cleanup twice."""
        await self.pause_all_active()
        for gid in tuple(self._active_instances):
            self.unregister_instance(gid)

    def unregister_instance(self, graph_instance_id: int) -> None:
        """Release idle runtime resources. An admitted/draining owner cannot be evicted."""
        self._require_idle(graph_instance_id)
        self._evict_instance(graph_instance_id)

    def _evict_instance(self, gid: int) -> None:
        instance = self._active_instances.pop(gid, None)
        if instance is not None:
            instance.coordinator.close()

    def _attach_output_adapter(self, coordinator: GraphPersistenceCoordinator) -> None:
        coordinator.set_output_adapter(self._output_adapter)

    def _lookup_coordinator(self, graph_instance_id: int) -> GraphPersistenceCoordinator | None:
        instance = self._active_instances.get(graph_instance_id)
        if instance is None:
            metadata = self._instance_store.load(graph_instance_id)
            if metadata is None or metadata.status not in {
                GraphInstanceStatus.PENDING,
                GraphInstanceStatus.PAUSED,
            }:
                return None
            coordinator = self._coordinator_factory.create(graph_instance_id, self._instance_store)
            self._attach_output_adapter(coordinator)
            for node_id in metadata.node_id_map.values():
                coordinator.register_node(node_id)
            instance = GraphInstance(metadata, coordinator)
            self._active_instances[graph_instance_id] = instance
        return instance.coordinator

    def _load_spec(self, spec_id: int) -> GraphSpec:
        spec = self._spec_store.load_by_id(spec_id)
        if spec is None:
            raise ValueError(f"GraphSpec {spec_id} not found in spec_store.")
        return spec

    def _create_state(self, spec: GraphSpec) -> GraphState:
        return self._compiler.resolve_state(spec)()


__all__ = ["GraphOrchestrator"]
