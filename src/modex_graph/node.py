# ruff: noqa: ANN401

"""`Node[S]` ABC — single-method `execute` with structured `NodeResult`,
plus the additive deliver/submit dual-method API (ticket 07).

Per ADR-0033 D2: the `execute` method is declared as `def` (NOT `async def`).
Subclasses may override with either `def` or `async def`. The engine unifies
both via `inspect.isawaitable(result)` — if the return value is awaitable
(coroutine), the engine awaits it; otherwise it uses the value directly.

This dual-mode design (borrowed from anyio/httpx/starlette precedent) avoids
splitting the node library into `SyncNode` + `AsyncNode` and duplicating the
engine loop. The cost is one `inspect.isawaitable` call per node execution
(negligible).

`S` is bound to `GraphState` — the typed Pydantic state the node reads from
and writes to via `ctx.state`.

---

Ticket 07 — deliver/submit dual-method API:

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

import inspect
from abc import ABC, abstractmethod
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any

from typing_extensions import TypeVar

from .constants import GraphNode, NodeTrigger
from .deliver_store import DeliverStore
from .exceptions import RoutingError
from .integration import (
    DefaultInputIntegrator,
    InputIntegrator,
    IntegratedInput,
    IntegratedPayload,
)

if TYPE_CHECKING:
    from .compiled_graph import CompiledGraph
    from .context import GraphContext
    from .result import NodeResult
    from .state import GraphState

S = TypeVar("S", bound="GraphState")


class Node[S: "GraphState"](ABC):
    """Abstract graph node. Executes logic and returns a `NodeResult`.

    Subclasses implement `execute(ctx) -> NodeResult`. The method is declared
    as `def` (sync); subclasses MAY override with `async def` for async I/O
    (LLM calls, tool execution, network requests). The engine detects the
    return type via `inspect.isawaitable` and awaits if needed.

    Convention: each `Node` instance has a `name` attribute matching its
    registration key in the `Graph`. The `Graph.add_node(name, node)` call
    sets it; subclasses may also set it in `__init__`.

    `trigger` (Task 06) is the per-node trigger mode under
    `ParallelScheduler`. `None` means "use the compiled graph's
    `default_trigger`". Subclasses may override to force a mode.

    Ticket 07 additive attributes:

    - `input_integrator: InputIntegrator` — default `DefaultInputIntegrator()`.
      Subclasses may override with a custom integrator.
    - `deliver_store: DeliverStore | None` — default `None` (in-memory
      accumulation only). Set to `InMemoryDeliverStore` or
      `SqliteDeliverStore` for persistence.
    """

    name: str = ""
    trigger: NodeTrigger | None = None

    # ── Ticket 07: deliver/submit attributes ───────────────────────────
    # `input_integrator` has a real runtime default (DefaultInputIntegrator
    # instance). `deliver_store` defaults to None (in-memory accumulation).
    input_integrator: InputIntegrator = DefaultInputIntegrator()
    deliver_store: DeliverStore | None = None

    # Ticket 03: max retries for undelivered detection. If a node's `execute`
    # produces no delivers, the framework retries with error feedback injected
    # into the integrated input. After `max_retry` retries (so max_retry + 1
    # total executions), `RoutingError` is raised as a safety net.
    max_retry: int = 3

    # Per-execution state (reset by `run`). `_pending_delivers` and
    # `_submit_result` are the only instance attributes — they are reset at
    # the start of each `run()` call. NOT concurrency-safe — a single Node
    # instance shared across concurrent executions would race.
    _pending_delivers: list[tuple[Any, str | None]] = []
    _submit_result: dict[str, list[Any]] = {}
    # Topology reference (per-execution, set by `run(graph=...)`). Schedulers
    # pass the CompiledGraph so `_resolve_default_target` can resolve
    # `next_node=None` via default edges / downstream / END.
    _graph_ref: CompiledGraph[S] | None = None

    @abstractmethod
    def execute(
        self,
        ctx: GraphContext[S],
        integrated_input: IntegratedInput,
    ) -> NodeResult | Awaitable[NodeResult]:
        """Execute node logic and return a `NodeResult`.

        Declared as `def` (not `async def`). Subclasses may override with
        `async def` — the engine unifies both via `inspect.isawaitable`.

        ``integrated_input`` carries the upstream delivered data, integrated
        by ``InputIntegrator``. It is an explicit parameter — NOT an instance
        attribute. Nodes read ``integrated_input.integrated_content`` (the
        integrated payload) or ``integrated_input.payloads`` (raw upstream
        payloads) to access data delivered by upstream nodes.

        Return type is `NodeResult | Awaitable[NodeResult]` to honestly
        reflect the dual-mode design: a `def` override returns `NodeResult`
        directly; an `async def` override returns a coroutine that yields
        `NodeResult`. The engine's `inspect.isawaitable` check handles both.

        Implementations may:
        - Read/write `ctx.state` imperatively (`ctx.state.x = y`).
        - Return `NodeResult(state_update={...})` for declarative updates.
        - Call `ctx.interrupt(value)` to suspend for HITL.
        - Call `ctx.runtime.dispatch_hook(...)` for AOP concerns.
        - Call `self.deliver(content, next_node, ctx)` to accumulate delivers
          for the deliver/submit model.
        """
        ...

    # ── Ticket 07: deliver/submit dual-method API ───────────────────────

    async def run(
        self,
        ctx: GraphContext[S],
        upstream_payloads: list[IntegratedPayload] | None = None,
        *,
        enforce_deliver: bool = True,
        graph: CompiledGraph[S] | None = None,
    ) -> NodeResult:
        """Framework entry point. Calls integrate -> execute (with undelivered
        detection retry) -> _submit.

        Called by the scheduler (``enforce_deliver=True``) and by direct test
        callers (``enforce_deliver=True``, the default).

        ``graph`` is the CompiledGraph topology, passed by the scheduler so
        ``_resolve_default_target`` can resolve ``next_node=None`` via default
        edges / downstream / END. Direct callers (tests) may omit it — nodes
        that deliver with explicit ``next_node`` never need topology.

        Steps:

        1. Integrate upstream payloads via `input_integrator.integrate(...)`.
           The result is a LOCAL variable — never stored on the instance.
        2. Retry loop (ticket 03 — undelivered detection):
           - Reset per-execution state (`_pending_delivers`).
           - Execute (node custom logic — may call `self.deliver()`), passing
             ``integrated`` as an explicit parameter.
           - Collect delivers. If any accumulated -> break (normal flow).
           - If no delivers: bridge check (see below). If the bridge skips
             retry, break. Otherwise create a NEW `IntegratedInput` with error
             feedback prepended (the original is never mutated) and re-execute.
             Repeat up to `max_retry` times. After `max_retry` retries
             without delivers, raise `RoutingError` (safety net).
        3. Submit (framework auto-dispatch by `next_node` grouping).
        4. Return the `NodeResult` (for compatibility — scheduler still uses it).

        Bridge (P3.4b — deliver-only convergence):

        When ``enforce_deliver=True`` (all callers post-convergence), nodes
        that produce no delivers retry with error feedback injected into the
        integrated input. After ``max_retry`` retries without delivers,
        ``RoutingError`` is raised as a safety net. There is no
        command/transition skip — every node MUST deliver.
        """
        integrated = self.input_integrator.integrate(upstream_payloads or [])
        self._submit_result = {}
        self._graph_ref = graph

        retry_count = 0
        while True:
            self._pending_delivers = []

            raw_result = self.execute(ctx, integrated)
            if inspect.isawaitable(raw_result):
                result: NodeResult = await raw_result
            else:
                result = raw_result

            delivers = self._collect_delivers(ctx)

            if delivers:
                break

            if not enforce_deliver:
                break

            if retry_count >= self.max_retry:
                raise RoutingError(
                    f"Node {self.name!r} produced no delivers after "
                    f"{retry_count + 1} executions (max_retry={self.max_retry}). "
                    f"The node forgot to call deliver() during execute()."
                )

            retry_count += 1
            error_feedback = IntegratedPayload(
                source_node="__framework__",
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
            integrated = self.input_integrator.integrate(
                [error_feedback] + (upstream_payloads or [])
            )

        if delivers:
            self.submit(ctx)

        return result

    def _deliver(
        self,
        content: Any,
        next_node: str | None,
        ctx: GraphContext[S],
    ) -> int | None:
        """Framework: accumulate a deliver. Returns `deliver_id` if persisted,
        `None` if in-memory only.

        If `deliver_store` is set AND `ctx.graph_instance_id` is not None:
        persists via `DeliverStore.accumulate(...)`. Otherwise: in-memory
        accumulation (append to `_pending_delivers`).

        `next_node` resolution (default edge / downstream / END) is deferred
        to `_submit` — here we store the raw `next_node` (`None` or `str`).
        When persisting, `None` is stored as `""` (empty string) per the
        `DeliverRecord.next_node` field contract.
        """
        if self.deliver_store is not None and ctx.graph_instance_id is not None:
            deliver_id = self.deliver_store.accumulate(
                graph_instance_id=ctx.graph_instance_id,
                node_name=self.name,
                next_node=next_node or "",
                content=content,
            )
            return deliver_id
        # In-memory accumulation only
        self._pending_delivers.append((content, next_node))
        return None

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
        """Collect all accumulated delivers for this execution.

        If `deliver_store` is set and `ctx.graph_instance_id` is not None:
        reads from the store (`query_pending`). Otherwise: reads from
        in-memory `_pending_delivers`.

        Returns:
            A list of `(content, next_node)` tuples. `next_node` is `None`
            for unresolved entries (stored as `""` in the DB, converted back
            to `None` here).
        """
        if self.deliver_store is not None and ctx.graph_instance_id is not None:
            records = self.deliver_store.query_pending(ctx.graph_instance_id, self.name)
            return [(r.content, r.next_node or None) for r in records]
        return list(self._pending_delivers)

    def _resolve_default_target(self, ctx: GraphContext[S]) -> list[str]:
        """Resolve `next_node=None` to concrete target(s) via graph topology.

        Returns all downstream edge targets from the current node. If no
        downstream edges exist, returns ``[GraphNode.END]``.

        Requires ``self._graph_ref`` (set by ``run(graph=...)``). Raises
        ``RoutingError`` if no topology was passed — callers must either pass
        explicit ``next_node`` to ``deliver()`` or pass ``graph=`` to ``run()``.
        """
        graph = self._graph_ref
        if graph is None:
            raise RoutingError(
                "next_node=None requires graph topology — pass graph= to run()"
            )
        targets = [e.target for e in graph.edges_from(self.name)]
        if targets:
            return targets
        return [GraphNode.END]

    def _submit(self, ctx: GraphContext[S]) -> None:
        """Framework: after execute returns, dispatch accumulated delivers
        by `next_node` grouping.

        Groups all accumulated delivers by `next_node`. For each group,
        calls `ctx.dispatch(target, state_update={"delivered": payload})`.
        Both `LINEAR` and `PARALLEL` schedulers register a dispatch handler,
        so this is the single dispatch path — no `scheduler_kind` branch
        (rule 15 convergence).

        `next_node=None` entries call `_resolve_default_target(ctx)`, which
        returns a list of targets (default edges / downstream / END).

        Payload shaping: a group with one entry dispatches the content
        directly; a group with multiple entries dispatches a list.

        `self._submit_result` is also set (LINEAR-only fallback for
        next-node selection when a custom `submit` override doesn't call
        `_submit`; PARALLEL uses `ctx.dispatch` exclusively).
        """
        delivers = self._collect_delivers(ctx)
        groups: dict[str, list[Any]] = {}
        for content, next_node in delivers:
            targets = (
                [next_node] if next_node is not None
                else self._resolve_default_target(ctx)
            )
            for t in targets:
                groups.setdefault(t, []).append(content)

        for target, contents in groups.items():
            payload: Any = contents[0] if len(contents) == 1 else contents
            ctx.dispatch(target, state_update={"delivered": payload})

        # LINEAR-only: scheduler reads this for next-node selection. PARALLEL
        # uses ctx.dispatch. Kept as a fallback for custom submit overrides.
        self._submit_result = groups

    def submit(self, ctx: GraphContext[S]) -> None:
        """Node-facing customization point. Default: delegates to `_submit`.

        Override for custom dispatch logic (e.g. custom grouping, custom
        payload shaping, conditional dispatch). The default `_submit`
        groups by `next_node` and dispatches each group as an integrated
        payload.
        """
        self._submit(ctx)

    @property
    def result(self) -> dict[str, list[Any]]:
        return self._submit_result


__all__ = ["Node", "S"]
