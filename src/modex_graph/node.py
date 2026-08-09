# ruff: noqa: ANN401

"""`Node[S]` ABC — async node execution with the deliver/submit API.

`S` is bound to `GraphState` — the typed Pydantic state the node reads from
and writes to via `ctx.state`.

---

Deliver/submit dual-method API:

Three-layer method split:

- `run` (framework-fixed): orchestrate integrate -> execute (with undelivered
  detection retry) -> submit.
- `_deliver` (framework-fixed): accumulate + persist (ABC-backed).
- `deliver` (node-custom, overridable): actual accumulation logic (default:
  append to pending list).
- `_submit` (framework-fixed): after execute returns, group by `next_node`
  and dispatch.
- `submit` (node-custom, overridable): actual dispatch logic (default:
  group by `next_node`, each group integrated).

Data flow: `integrated_input` is an EXPLICIT parameter to `execute`, NOT an
instance attribute. `run()` creates a local `integrated` variable and passes
it to `execute()`. On retry (undelivered detection), a NEW `IntegratedInput`
is created and passed to the next `execute()` call — the original is never
mutated. `deliver()` requires `ctx` as an explicit parameter (no implicit
instance-attribute fallback).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Literal

from typing_extensions import TypeVar

from .constants import (
    DeliverConsumptionStatus,
    FrameworkPayloadSource,
    GraphNode,
    NodeTrigger,
)
from .exceptions import GraphBubbleUp, GraphInterrupt, RoutingError
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

    # Max retries for undelivered detection. If a node's `execute`
    # produces no delivers, the framework retries with error feedback injected
    # into the integrated input. After `max_retry` retries (so max_retry + 1
    # total executions), `RoutingError` is raised as a safety net.
    max_retry: int = 3

    # Per-execution state (reset by `run`). `_pending_delivers` and
    # `_submit_result` are reset at the start of each `run()` call.
    # NOT concurrency-safe — a single Node instance shared across
    # concurrent executions would race.
    _pending_delivers: list[tuple[Any, str | None]] | None = None
    _submit_result: dict[str, list[Any]] = {}
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
        """Execute node logic and accumulate downstream delivers.

        ``integrated_input`` carries the upstream delivered data, integrated
        by ``InputIntegrator``. It is an explicit parameter — NOT an instance
        attribute. Nodes read ``integrated_input.integrated_content`` (the
        integrated payload) or ``integrated_input.payloads`` (raw upstream
        payloads) to access data delivered by upstream nodes.

        Implementations may:
        - Read/write `ctx.state` imperatively (`ctx.state.x = y`).
        - Call `ctx.interrupt(value)` to suspend for HITL.
        - Call `ctx.runtime.dispatch_hook(...)` for AOP concerns.
        - Call `self.deliver(content, next_node, ctx)` to accumulate delivers
          for the deliver/submit model.
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

        begin → integrate → execute (with undelivered detection retry) →
        complete/cancel/suspend/crash → finalize.

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

        1. ``load_latest`` — resume check (read-only, before
           begin). If the latest invocation is suspended with a state
           snapshot, the snapshot is used as integrated input and delivers
           are NOT re-consumed.
        2. ``begin_invocation`` — create a new RUNNING invocation.
           ``parent_version`` computed internally.
        3. Integrate (inside try — crashes are covered by crash/finalize):
           - Resume: use ``prev.state_json`` as integrated input.
           - Normal: collect consumable delivers, mark consumed, integrate
             via ``input_integrator``.
        4. Retry loop (undelivered detection):
           - Reset per-execution state (``_pending_delivers``).
           - Execute (node custom logic — may call ``self.deliver()``),
             passing ``integrated`` as an explicit parameter.
           - Collect delivers. If any accumulated -> break (normal flow).
           - If no delivers: create a NEW ``IntegratedInput`` with error
             feedback prepended (the original is never mutated) and
             re-execute. Repeat up to ``max_retry`` times. After
             ``max_retry`` retries without delivers, raise ``RoutingError``
             (safety net).
        5. Submit (framework auto-dispatch by ``next_node`` grouping).
        6. ``complete_invocation`` — save COMPLETED + promote delivers.

        Exception handling:

        - ``GraphInterrupt``: checkpoint state via
          ``ctx.state.checkpoint()``, call ``suspend_invocation``, re-raise.
        - ``GraphBubbleUp`` (other cooperative-control): call
          ``cancel_invocation``, re-raise.
        - Other ``Exception``: call ``crash_invocation``, re-raise.
        - ``finally``: ``finalize_invocation`` (safety net for orphan
           non-suspended RUNNING).
        """
        coordinator = ctx.coordinator
        store = ctx.node_state_store

        # Resume check — before begin_invocation (read-only query).
        # If the latest invocation is suspended, this is a resume from
        # suspend — use the snapshot as integrated input base, then
        # append any PENDING delivers that arrived after suspend
        # (CONSUMED_PENDING are skipped — already consumed pre-suspend).
        prev = store.load_latest(self.node_id)
        is_resume = prev is not None and prev.suspended

        # Begin invocation (parent_version computed internally).
        invocation = store.begin_invocation(self.node_id)
        ctx.current_invocation = invocation
        coordinator.emit_output(
            GraphOutputKind.NODE_STARTED,
            node_id=self.node_id,
            node_name=self.name,
            invocation_id=invocation.invocation_id,
        )

        self._submit_result = {}
        self._graph_ref = graph

        try:
            resume_snapshot: dict[str, Any] | None = None
            if is_resume:
                assert prev is not None
                resume_snapshot = prev.state_json
            integrated = self._integrate_upstream(
                coordinator,
                invocation,
                resume_snapshot=resume_snapshot,
            )

            # Execute with undelivered detection retry.
            retry_count = 0
            while True:
                self._pending_delivers = []

                await self.execute(ctx, integrated)

                collected = self._collect_delivers(ctx)

                if collected:
                    break

                if self.name == GraphNode.END:
                    break

                if retry_count >= self.max_retry:
                    raise RoutingError(
                        f"Node {self.name!r} produced no delivers after "
                        f"{retry_count + 1} executions (max_retry={self.max_retry}). "
                        f"The node forgot to call deliver() during execute()."
                    )

                retry_count += 1
                error_feedback = IntegratedPayload(
                    source_node=FrameworkPayloadSource.FRAMEWORK,
                    content={
                        "error": "undelivered",
                        "message": (
                            f"Previous execution of node {self.name!r} produced no "
                            f"delivers. You MUST call deliver(content, next_node, ctx) "
                            f"during execute(). Retry {retry_count}/{self.max_retry}."
                        ),
                        "retry_count": retry_count,
                        "max_retry": self.max_retry,
                    },
                    metadata={"error_type": "undelivered", "retry": retry_count},
                )
                integrated = self.input_integrator.integrate([error_feedback])

            # Submit (framework auto-dispatch by next_node grouping).
            if self.name != GraphNode.END:
                self.submit(ctx)

            # Complete: save COMPLETED + promote delivers.
            store.complete_invocation(invocation, ctx.state.checkpoint())
            coordinator.promote_delivers(self.node_id, invocation.invocation_id)
            coordinator.emit_output(
                GraphOutputKind.NODE_COMPLETED,
                node_id=self.node_id,
                node_name=self.name,
                invocation_id=invocation.invocation_id,
            )
            return None

        except GraphInterrupt:
            # Checkpoint state directly, then suspend.
            snapshot = ctx.state.checkpoint()
            store.suspend_invocation(invocation, snapshot)
            raise
        except GraphBubbleUp:
            # Cancel cooperative-control exceptions. GraphInterrupt is
            # caught above (suspend path).
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
            store.finalize_invocation(invocation)

    def _integrate_upstream(
        self,
        coordinator: GraphPersistenceCoordinator,
        invocation: InvocationContext,
        *,
        resume_snapshot: dict[str, Any] | None,
    ) -> IntegratedInput:
        """Collect upstream delivers, mark consumed, and integrate into input.

        Converged pipeline for both normal and resume paths (rule 15).
        When ``resume_snapshot`` is provided (resume from suspend), only
        PENDING delivers are consumed (CONSUMED_PENDING were already
        consumed pre-suspend) and the snapshot is prepended to the
        integrated payloads.
        """
        is_resume = resume_snapshot is not None
        delivers = coordinator.collect_consumable_delivers(
            self.node_id, invocation.invocation_id
        )
        if is_resume:
            delivers = [
                d for d in delivers if d.status == DeliverConsumptionStatus.PENDING
            ]
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
                )
                for r in delivers
            ]
        else:
            payloads = []
        if is_resume:
            payloads = [
                IntegratedPayload(
                    source_node=FrameworkPayloadSource.RESUME,
                    content=resume_snapshot,
                )
            ] + payloads
        return self.input_integrator.integrate(payloads)

    def _deliver(
        self,
        content: Any,
        next_node: str | None,
        ctx: GraphContext[S],
    ) -> None:
        """Framework: accumulate a deliver in-memory.

        The ``deliver_store`` / ``graph_instance_id`` persistence
        branch is removed — delivers are always in-memory during execute.
        Persistence routing happens via the coordinator's ``route_deliver``
        in the dispatch handler.

        ``next_node`` resolution (default edge / downstream / END) is deferred
        to ``_submit`` — here we store the raw ``next_node`` (``None`` or
        ``str``).
        """
        if self._pending_delivers is None:
            self._pending_delivers = []
        self._pending_delivers.append((content, next_node))

    def deliver(
        self,
        content: Any,
        next_node: str | None,
        ctx: GraphContext[S],
    ) -> None:
        """Node-facing API. Call during `execute()` to accumulate a deliver.

        ``ctx`` is required — pass the ``ctx`` received by ``execute()``.
        """
        self._deliver(content, next_node, ctx)

    def _collect_delivers(self, ctx: GraphContext[S]) -> list[tuple[Any, str | None]]:
        """Collect all accumulated delivers for this execution (in-memory).

        The ``deliver_store`` / ``graph_instance_id`` read branch
        is removed — delivers are always read from in-memory
        ``_pending_delivers``.

        Returns:
            A list of ``(content, next_node)`` tuples.
        """
        return list(self._pending_delivers or [])

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

    def _submit(self, ctx: GraphContext[S]) -> None:
        """Framework: after execute returns, dispatch accumulated delivers
        by `next_node` grouping.

        Groups all accumulated delivers by `next_node`. For each group,
        each deliver is dispatched individually via
        `ctx.dispatch(target, state_update={"delivered": content})` so
        downstream receives one `IntegratedPayload` per deliver.

        Both `LINEAR` and `PARALLEL` schedulers register a dispatch handler,
        so this is the single dispatch path — no `scheduler_kind` branch
        (rule 15 convergence).

        `next_node=None` entries call `_resolve_default_target(ctx)`, which
        returns a list of targets (default edges / downstream / END).

        `self._submit_result` is also set (test-observation seam — no
        production code reads it; tests inspect it to verify dispatch
        grouping without mocking the scheduler).
        """
        delivers = self._collect_delivers(ctx)
        groups: dict[str, list[Any]] = {}
        for content, next_node in delivers:
            targets = [next_node] if next_node is not None else self._resolve_default_target(ctx)
            for t in targets:
                groups.setdefault(t, []).append(content)

        inv_ctx = ctx.current_invocation
        for target, contents in groups.items():
            for content in contents:
                ctx.dispatch(
                    target,
                    state_update={
                        "delivered": content,
                        "_source_node": inv_ctx.node_id if inv_ctx else self.node_id,
                        "_source_inv_id": inv_ctx.invocation_id if inv_ctx else 0,
                    },
                )

        self._submit_result = groups

    def submit(self, ctx: GraphContext[S]) -> None:
        """Node-facing customization point. Default: delegates to `_submit`.

        Override for custom dispatch logic (e.g. custom grouping, custom
        payload shaping, conditional dispatch). The default `_submit`
        groups by `next_node` and dispatches each group as an integrated
        payload.
        """
        self._submit(ctx)


__all__ = ["Node", "S"]
