# ruff: noqa: ANN401

"""`Node[S]` ABC — async node execution with persisted delivery routing.

`S` is bound to `GraphState` — the typed Pydantic state the node reads from
and writes to via `ctx.state`.

---

Delivery is persisted to the target store during `execute()`, staged until the
source invocation completes, then promoted and dispatched as a scheduling wakeup.

Data flow: `integrated_input` is an EXPLICIT parameter to `execute`, NOT an
instance attribute. `run()` creates a local `integrated` variable and passes
it to `execute()`. `deliver()` requires `ctx` as an explicit parameter (no
implicit instance-attribute fallback).
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Literal

from typing_extensions import TypeVar

from .constants import GraphNode, NodeTrigger, SchedulerKind
from .exceptions import GraphBubbleUp, RoutingError
from .execution_context import get_execution
from .integration import (
    DefaultInputIntegrator,
    InputIntegrator,
    IntegratedInput,
    IntegratedPayload,
)
from .output_adapter import GraphOutputKind

if TYPE_CHECKING:
    from .compiled_graph import CompiledGraph
    from .context import GraphContext
    from .persistence.graph_metadata import InvocationContext
    from .persistence.persistence_coordinator import GraphPersistenceCoordinator
    from .state import GraphState

S = TypeVar("S", bound="GraphState")


class Node[S: "GraphState"](ABC):
    """Abstract graph node. Executes logic and routes through deliver/submit.

    Subclasses implement async ``execute(ctx, integrated_input) -> None``.

    Convention: each `Node` instance has a `name` attribute matching its
    registration key in the `Graph`. The `Graph.add_node(name, node)` call
    sets it; subclasses may also set it in `__init__`.

    `trigger` is the per-node trigger mode under
    `ParallelScheduler`. `None` means "use the compiled graph's
    `default_trigger`". Subclasses may override to force a mode.

    Additive attributes:

    - `input_integrator: InputIntegrator` — default `DefaultInputIntegrator()`.
      Subclasses may override with a custom integrator.
    """

    name: str = ""
    node_id: str = ""
    trigger: NodeTrigger | None = None

    # ── Deliver/submit attributes ───────────────────────────────────
    input_integrator: InputIntegrator = DefaultInputIntegrator()

    # Topology reference (per-execution, set by `run(graph=...)`). Schedulers
    # pass the CompiledGraph so `_resolve_default_target` can resolve
    # `next_node=None` via default edges / downstream / END.
    _graph_ref: CompiledGraph[S] | None = None

    @abstractmethod
    async def execute(
        self,
        ctx: GraphContext[S],
        integrated_input: IntegratedInput,
    ) -> None:
        """Execute this node's logic.

        Contract:
        - Input: read from `integrated_input` (upstream delivers).
        - Output: call `self.deliver(content, next_node, ctx)` to send data downstream.
        - Working state: `ctx.scratch` — the current node's scoped region
          of `ctx.state.node_scratch[self.node_id]`. Write freely. Reading
          other nodes' scratch is PROHIBITED — cross-node data must flow
          through deliver/IntegratedInput only.
        - Framework fields (resume_target, result on DefaultGraphState): framework-managed,
          do not write unless you are a framework node (START/END).

        Args:
            ctx: The graph context (graph-run scoped resources shared, invocation-local
                 fields set by scheduler).
            integrated_input: All upstream delivers materialized as input payloads.
        """
        ...

    # ── Deliver/submit dual-method API ───────────────────────

    # ── Store-driven lifecycle (via ctx.node_state_store) ─────

    async def run(
        self,
        ctx: GraphContext[S],
        *,
        graph: CompiledGraph[S] | None = None,
    ) -> None:
        """Framework entry point. Store-driven lifecycle via ctx.node_state_store:

        begin → integrate → execute →
        complete/cancel/crash → finalize.

        Called by the scheduler (``graph=compiled``) and by direct test
        callers. The coordinator is accessed via ``ctx.coordinator``
        (always present). Upstream payloads now flow through the
        coordinator's ``collect_consumable_delivers`` instead of an
        explicit parameter.

        ``graph`` is the CompiledGraph topology, passed by the scheduler so
        ``_resolve_default_target`` can resolve ``next_node=None`` via default
        edges / downstream / END. Direct callers (tests) may omit it — nodes
        that deliver with explicit ``next_node`` never need topology.

        Lifecycle:

        1. ``begin_invocation`` — create a new RUNNING invocation.
           ``parent_version`` computed internally.
        2. Integrate (inside try — crashes are covered by crash/finalize):
           collect all consumable delivers, mark consumed, and integrate via
           ``input_integrator``.
        3. Execute (single call): call ``execute(ctx, integrated)``.
           Dead-end detection (no delivers produced) is handled by the
           schedulers — a node that delivers nothing produces no dispatches,
           so the graph terminates with ``ctx.reached_end = False`` (FAILED).
        4. Complete the invocation, promote staged outputs, and dispatch their
           targets as scheduling wakeups.
        5. Promote this invocation's consumed inputs.

        Exception handling:

        - ``GraphBubbleUp`` / ``asyncio.CancelledError`` control exceptions: call
          ``cancel_invocation``, re-raise. This includes ``GraphInterrupt``.
        - Other ``Exception``: call ``crash_invocation``, re-raise.
        - ``finally``: ``finalize_invocation`` (safety net for orphan RUNNING).
        """
        coordinator = ctx.coordinator
        store = ctx.node_state_store

        # Begin invocation (parent_version computed internally).
        invocation = store.begin_invocation(self.node_id, graph_run_version=ctx.graph_run_version)

        exec_ctx = get_execution()
        if exec_ctx is None:
            from .execution_context import NodeExecution, reset_execution, set_execution

            exec_ctx = NodeExecution(instance_id="")
            exec_ctx.invocation = invocation
            _run_token = set_execution(exec_ctx)
            _owns_token = True
        else:
            exec_ctx.invocation = invocation
            _run_token = None
            _owns_token = False

        coordinator.emit_output(
            GraphOutputKind.NODE_STARTED,
            node_id=self.node_id,
            node_name=self.name,
            invocation_id=invocation.invocation_id,
        )

        self._graph_ref = graph

        try:
            integrated = self._integrate_upstream(coordinator, invocation)

            # Execute (single call — schedulers detect dead-end natively
            # when no dispatches are produced).
            await self.execute(ctx, integrated)

            store.complete_invocation(invocation)
            affected = coordinator.promote_staged_by_source(
                coordinator.graph_instance_id,
                self.node_id,
            )
            if self.name != GraphNode.END:
                for target_node_id in affected:
                    target = target_node_id
                    if graph is not None:
                        target = next(
                            (
                                name
                                for name, target_node in graph.nodes.items()
                                if target_node.node_id == target_node_id
                            ),
                            None,
                        )
                        if target is None:
                            raise RoutingError(
                                f"No node name found for promoted target id {target_node_id!r}"
                            )
                    if (
                        ctx.scheduler_kind == SchedulerKind.PARALLEL
                        and target == self.name
                    ):
                        ctx.control.notify_deliver(target)
                    else:
                        ctx.dispatch(target)
            coordinator.promote_delivers(self.node_id, invocation.invocation_id)
            coordinator.emit_output(
                GraphOutputKind.NODE_COMPLETED,
                node_id=self.node_id,
                node_name=self.name,
                invocation_id=invocation.invocation_id,
            )
            return None

        except (GraphBubbleUp, asyncio.CancelledError):
            store.cancel_invocation(invocation)
            raise
        except Exception as exc:
            store.crash_invocation(invocation)
            coordinator.emit_output(
                GraphOutputKind.NODE_CRASHED,
                node_id=self.node_id,
                node_name=self.name,
                invocation_id=invocation.invocation_id,
                error=str(exc),
            )
            raise
        finally:
            if _owns_token:
                assert _run_token is not None
                reset_execution(_run_token)
            store.finalize_invocation(invocation)

    def _integrate_upstream(
        self,
        coordinator: GraphPersistenceCoordinator,
        invocation: InvocationContext,
    ) -> IntegratedInput:
        """Collect all consumable delivers, mark consumed, and integrate them."""
        delivers = coordinator.collect_consumable_delivers(
            self.node_id, invocation.invocation_id
        )
        if delivers:
            coordinator.mark_delivers_consumed(
                self.node_id,
                [r.deliver_id for r in delivers],
                invocation.invocation_id,
            )
            payloads = [
                IntegratedPayload(
                    source_node=r.source_node_id,
                    content=r.content,
                    status=r.status,
                    consumed_by_invocation_id=r.consumed_by_invocation_id,
                )
                for r in delivers
            ]
        else:
            payloads = []
        return self.input_integrator.integrate(payloads)

    def _deliver(
        self,
        content: Any,
        next_node: str | None,
        ctx: GraphContext[S],
    ) -> None:
        """Persist one staged output in its target node's deliver store.

        When graph is unavailable the name is used as the target id; direct callers
        must ensure ``node_id == name`` or pass the compiled graph.
        """
        execution = get_execution()
        invocation = execution.invocation if execution is not None else None
        if invocation is None:
            raise RuntimeError("deliver() requires an active node invocation")
        targets = [next_node] if next_node is not None else self._resolve_default_target(ctx)
        graph = self._graph_ref
        for resolved in targets:
            target_node_id = graph.nodes[resolved].node_id if graph is not None else resolved
            ctx.coordinator.route_deliver(
                target_node_id=target_node_id,
                content=content,
                source_node_id=self.node_id,
                source_invocation_id=invocation.invocation_id,
                source_node_name=self.name,
                stage=True,
            )

    def deliver(
        self,
        content: Any,
        next_node: str | None,
        ctx: GraphContext[S],
    ) -> None:
        """Route a staged deliver during `execute()`."""
        self._deliver(content, next_node, ctx)

    def _resolve_default_target(
        self,
        ctx: GraphContext[S],
        *,
        policy: Literal["strict", "graceful"] = "strict",
    ) -> list[str]:
        """Resolve `next_node=None` to concrete target(s) via graph topology.

        Returns the downstream edge targets from the current node. If no
        downstream edges exist, returns ``[GraphNode.END]``.

        ``policy`` controls behavior when the node has more than one
        downstream edge:

        - ``"strict"`` (default): raise ``RoutingError``. Auto-deliver to
          all is forbidden because it causes unconditional fan-out (e.g.
          reviewer→coder + reviewer→END would loop forever). The caller
          must specify an explicit target.
        - ``"graceful"``: return ``[GraphNode.END]`` instead of raising.
          Used by nodes that want a safe fallback (deliver to END) when
          the topology is ambiguous rather than surfacing an error.

        Requires ``self._graph_ref`` (set by ``run(graph=...)``). Raises
        ``RoutingError`` if no topology was passed.
        """
        graph = self._graph_ref
        if graph is None:
            raise RoutingError("next_node=None requires graph topology — pass graph= to run()")
        targets = [e.target for e in graph.edges_from(self.name)]
        if len(targets) > 1:
            if policy == "strict":
                raise RoutingError(
                    f"Node {self.name!r} has {len(targets)} downstream targets "
                    f"({sorted(targets)}) but no explicit target was specified. "
                    "Auto-deliver to all downstream nodes is forbidden when "
                    "multiple edges exist — specify an explicit target."
                )
            return [GraphNode.END]
        if targets:
            return targets
        return [GraphNode.END]



__all__ = ["Node", "S"]
