"""Assembly output + mutable accumulator builder (SPEC §6.1).

Two types:

- :class:`AssembledAgent` — the immutable output of the assembly pipeline.
  A frozen dataclass carrying Python object references (the agent instance,
  its pool, the strategy result, per-workspace infrastructure, and the
  subagent lazy-load slot). Rule 11 leaf value-object escape hatch (like
  :class:`~modex_agent.plugins.assembly.context.PoolRuntimeDeps`): pure data
  carrier, no behavior, frozen.
- :class:`AssemblyBuilder` — the mutable accumulator that assembles an
  :class:`AssembledAgent` incrementally and owns the async cleanup contract.
  A regular class (NOT a frozen dataclass — rule 11: classes with behavior
  must not be ``@dataclass(frozen=True)``). Fields start as ``None`` and are
  filled by the assembly stages; cleanup callbacks are registered as each
  runtime resource is created.

Cleanup contract (SPEC §6.1):

    builder.cleanup() 按逆序销毁已累积的资源（先 agent → pool → infra →
    workspace_resources），释放 DB 连接/文件句柄/线程，确保装配失败不泄漏资源。

Three invariants:

1. **Reverse order** — cleanup callbacks run in reverse registration order.
   The last resource registered (agent, top of the stack) is torn down first;
   the first registered (workspace_resources, base) is torn down last.
2. **Exception isolation** — each callback is wrapped in ``try/except``. One
   failing cleanup does not block subsequent ones (a failing broker ``stop()``
   must not prevent DB connection release).
3. **Idempotency** — a second ``cleanup()`` call is a no-op. Prevents
   double-free when assembly failure triggers cleanup and the caller's
   ``finally`` block also calls cleanup.

The cleanup callback type is ``Callable[[], Awaitable[None]]`` — a zero-arg
async callable. Callers pass bound async methods directly (e.g.
``pool.shutdown_all``, ``bridge.stop``, ``poller.stop``). The implementation
calls ``await cb()`` for each. This matches the existing
``StrategyAssembly.extra_cleanup: tuple[Callable[[], Awaitable[None]], ...]``
contract in ``multi_agent/execution_strategy.py``.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

    from modex_agent.multi_agent.pool import AgentPool
    from modex_agent.plugins.assembly.context import AssemblyContext, SupplyInfra
    from modex_agent.plugins.capability import CapabilityWiring

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AssembledAgent:
    """Immutable output of the assembly pipeline (SPEC §6.1).

    Carries the final assembled agent and its supporting runtime objects.
    All fields are Python object references (not serialized across module
    boundaries). ``None`` fields mean "not applicable for this agent kind":

    - ``pool`` — main agent has a pool; subagent does not.
    - ``workspace_resources`` — main agent builds per-workspace infra first;
      subagent shares the parent's.
    - ``subagent_slot`` — main agent has a lazy-load slot; subagent does not.

    Runtime-object container per rule 12 — NOT Pydantic ``BaseModel``.
    Frozen per rule 11 (leaf value-object, no behavior).

    ``agent`` is typed ``Any`` because :class:`~modex_agent.agents.react.ReActAgent`
    and :class:`~modex_agent.agents.external.agent.ExternalAgent` share no
    common framework base class. This is the documented ``Any`` escape hatch
    at the agent-kind boundary (same as ``PoolAssemblyContext.workspace_handle``).
    """

    agent: Any | None = None
    pool: AgentPool | None = None
    strategy_result: Any | None = None
    workspace_resources: Any | None = None
    infra: SupplyInfra | None = None
    subagent_slot: dict[str, Any] | None = None
    descriptor: Any | None = None
    propagated_context: AssemblyContext | None = None
    # Live MCP backend loaded at Stage 4 (ticket 10) — the connection-
    # lifecycle handle the orchestrator harvests into PoolInstance so
    # teardown can release it.
    mcp_manager: Any | None = None
    # The main agent's capability wiring products (Stage 4's native
    # capability dispatch) — the orchestrator harvests per-agent wiring
    # artifacts (e.g. the pool root's communication target store) for
    # the post-pipeline faces. ``None`` when the agent compiles no
    # capabilities or the pipeline never reached Stage 4.
    capability_wirings: Mapping[str, CapabilityWiring] | None = None


class AssemblyBuilder:
    """Mutable accumulator for :class:`AssembledAgent` (SPEC §6.1).

    Assembly stages fill the same-named fields incrementally and register
    async cleanup callbacks as each runtime resource is created. After all
    stages complete, :meth:`build_agent` produces the immutable
    :class:`AssembledAgent`. On failure, :meth:`cleanup` tears down all
    registered resources in reverse order.

    Regular class (NOT ``@dataclass(frozen=True)``) — rule 11: classes with
    behavior must not be frozen. The behavior is :meth:`register_cleanup`,
    :meth:`build_agent`, and :meth:`cleanup`.

    Cleanup registration mirrors the resource lifecycle:

    1. ``workspace_resources`` built → register ``poller.stop`` (base of stack).
    2. ``infra`` built → register ``bridge.stop``.
    3. ``pool`` built → register ``pool.shutdown_all``.
    4. ``agent`` built → register ``agent.stop`` (top of stack).

    :meth:`cleanup` runs these in reverse: agent → pool → infra →
    workspace_resources.
    """

    # Same 7 fields as AssembledAgent, declared at class level for type
    # checkers. Initialized to None in __init__ (mutable instance state,
    # not class-level mutable defaults).
    agent: Any | None
    pool: AgentPool | None
    strategy_result: Any | None
    workspace_resources: Any | None
    infra: SupplyInfra | None
    subagent_slot: dict[str, Any] | None
    descriptor: Any | None
    # Propagated context — stages that replace ctx (e.g. PoolAssembleStage
    # fills pool_runtime) set this; pipeline.run passes it to the next
    # stage and build_agent exposes it on AssembledAgent for post-pipeline
    # consumers (interceptor/command factory resolution).
    propagated_context: AssemblyContext | None
    mcp_manager: Any | None
    capability_wirings: Mapping[str, CapabilityWiring] | None

    def __init__(self) -> None:
        self.agent = None
        self.pool = None
        self.strategy_result = None
        self.workspace_resources = None
        self.infra = None
        self.subagent_slot = None
        self.descriptor = None
        self.propagated_context = None
        self.mcp_manager = None
        self.capability_wirings = None
        self._cleanups: list[Callable[[], Awaitable[None]]] = []
        self._cleaned_up: bool = False

    def register_cleanup(self, coro_fn: Callable[[], Awaitable[None]]) -> None:
        """Register an async cleanup callback.

        Each callback wraps a runtime resource's async stop method (e.g.
        ``pool.shutdown_all``, ``broker_bridge.stop``, ``inbox_poller.stop``).
        Callbacks are called in reverse registration order during
        :meth:`cleanup`.

        The callable is a zero-arg async function: calling it returns a
        coroutine, which ``cleanup`` awaits. Pass bound methods directly
        (``pool.shutdown_all``) or wrap in an async closure.
        """
        self._cleanups.append(coro_fn)

    async def build_agent(self) -> AssembledAgent:
        """Assemble the final :class:`AssembledAgent` from accumulated fields.

        Async to match the fully-async assembly pipeline (SPEC: "管线全异步").
        The body is pure construction — no validation, no I/O — but the async
        signature allows future async finalization steps.
        """
        return AssembledAgent(
            agent=self.agent,
            pool=self.pool,
            strategy_result=self.strategy_result,
            workspace_resources=self.workspace_resources,
            infra=self.infra,
            subagent_slot=self.subagent_slot,
            descriptor=self.descriptor,
            propagated_context=self.propagated_context,
            mcp_manager=self.mcp_manager,
            capability_wirings=self.capability_wirings,
        )

    async def cleanup(self) -> None:
        """Run all registered cleanup callbacks in reverse order.

        Three invariants (SPEC §6.1):

        1. **Reverse order** — last registered runs first (LIFO stack).
        2. **Exception isolation** — each callback is wrapped in
           ``try/except``; one failure does not block subsequent cleanups.
        3. **Idempotency** — the second call is a no-op (no double-free).

        The idempotency flag is set BEFORE running cleanups so that re-entry
        (concurrent or sequential) is a no-op. If a cleanup raises, it is
        logged at WARNING level and the next callback still runs.
        """
        if self._cleaned_up:
            return
        self._cleaned_up = True
        # Take a local copy and clear the original list so we iterate over a
        # stable snapshot and release references early.
        cleanups = list(self._cleanups)
        self._cleanups.clear()
        for cb in reversed(cleanups):
            try:
                await cb()
            except Exception:
                logger.warning(
                    "Assembly cleanup callback failed; continuing with remaining cleanups",
                    exc_info=True,
                )
