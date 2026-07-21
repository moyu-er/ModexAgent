# ruff: noqa: ANN401

"""``ReactGraphRuntime`` — bridges ``modex_graph``'s ``GraphRuntime`` ABC to
``modex_agent``'s AOP services.

Per ADR-0033 D5 + D13 Stage 1: the ReAct-side adapter that maps business
``StrEnum`` values (``ReActHookPoint`` / ``ReActScope`` / ``ReActEvent``) to
``modex_agent`` enums (``HookPoint`` / ``InterceptorScope`` /
``ReActEvent``) and bridges ``GraphContext`` to ``AgentContext`` for the
underlying services.

CRITICAL design rules (ADR-0033 D5):

1. ``before_iteration`` / ``after_iteration`` are NOT on ``GraphRuntime``.
   ReAct nodes dispatch ``BEFORE_ITERATION`` / ``AFTER_ITERATION`` explicitly
   via ``ctx.runtime.dispatch_hook(ReActHookPoint.BEFORE_ITERATION, ctx)``
   at the exact same code points as today. This preserves hook timing
   exactly (eliminates the highest migration risk).

2. ``dispatch_hook`` carries a ``data: dict | None`` parameter. This wraps
   it into ``HookPayload(data=data)`` when calling ``hook_runner.dispatch``.

3. ``ReactGraphRuntime`` bridges ``GraphContext`` to ``AgentContext``: all
   methods receive ``GraphContext`` but extract ``ctx.user_data`` (which
   holds ``AgentContext``) and pass it to the underlying services. Hook
   implementations are completely unaware of the migration.

4. ``around`` constructs interceptor context internally from
   ``ctx.user_data`` (AgentContext).

Stage 1 status: this class exists but is NOT referenced by ReAct runtime
code yet. ReAct still uses the old ``core/graph/`` engine. Stage 3 wires
this class into the ReAct graph nodes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, cast

from modex_agent.agents.react.constants import ReActHookPoint, ReActScope
from modex_agent.hook.abc import HookPayload, HookPoint
from modex_graph.runtime import GraphRuntime

if TYPE_CHECKING:
    from modex_agent.agents.react.agent import ReActEvent
    from modex_agent.control.channel import InMemoryControlChannel
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.emitter import ContentEmitter
    from modex_agent.core.governance import ContextGovernance
    from modex_agent.hook import HookRunner
    from modex_agent.interceptor.abc import IterationNext
    from modex_agent.interceptor.chain import InterceptorChain
    from modex_agent.runtime.models import TurnStateBase
    from modex_agent.runtime.policy import SnapshotPolicy
    from modex_agent.runtime.store import TurnStateStore
    from modex_graph.context import GraphContext


class ReactGraphRuntime(GraphRuntime):
    """ReAct adapter for ``modex_graph``'s ``GraphRuntime`` ABC.

    Maps business ``StrEnum`` values to ``modex_agent`` enums and bridges
    ``GraphContext`` (from the graph engine) to ``AgentContext`` (from the
    agent framework) for the underlying AOP services.

    The constructor accepts all 7 AOP services as keyword-only arguments.
    ``None`` services are treated as "not configured" — the corresponding
    method becomes a no-op or pass-through, mirroring the existing
    ``if runtime.hooks:`` / ``if runtime.interceptors:`` guard pattern.
    """

    HOOK_POINT_MAP: Mapping[str, HookPoint] = {
        ReActHookPoint.BEFORE_ITERATION: HookPoint.BEFORE_ITERATION,
        ReActHookPoint.AFTER_ITERATION: HookPoint.AFTER_ITERATION,
        ReActHookPoint.AFTER_LLM_RESPONSE: HookPoint.AFTER_LLM_RESPONSE,
        ReActHookPoint.BEFORE_TOOL_EXECUTION: HookPoint.BEFORE_TOOL_EXECUTION,
        ReActHookPoint.AFTER_TOOL_EXECUTION: HookPoint.AFTER_TOOL_EXECUTION,
        ReActHookPoint.FINALIZE_CONTENT: HookPoint.FINALIZE_CONTENT,
    }

    def __init__(
        self,
        *,
        hook_runner: HookRunner | None = None,
        interceptor_chain: InterceptorChain | None = None,
        governance: ContextGovernance | None = None,
        control_channel: InMemoryControlChannel | None = None,
        snapshot_policy: SnapshotPolicy | None = None,
        turn_state_store: TurnStateStore | None = None,
        emitter: ContentEmitter[ReActEvent] | None = None,
    ) -> None:
        self._hook_runner = hook_runner
        self._interceptor_chain = interceptor_chain
        self._governance = governance
        self._control_channel = control_channel
        self._snapshot_policy = snapshot_policy
        self._turn_state_store = turn_state_store
        self._emitter = emitter

    # ── Engine-auto-invoked (2, node-level universal) ──────────────────

    async def before_node(self, ctx: GraphContext[Any], node_name: str) -> None:
        """Engine calls this before each node's ``execute(ctx)``.

        No-op for ReAct — ReAct does not use node-level engine-auto hooks.
        Node-specific hooks (``BEFORE_ITERATION`` etc.) are dispatched
        explicitly by the nodes via ``dispatch_hook``.
        """

    async def after_node(self, ctx: GraphContext[Any], node_name: str, result: Any) -> None:
        """Engine calls this after each node's ``execute(ctx)`` returns.

        No-op for ReAct — same rationale as ``before_node``.
        """

    # ── Node-explicit (6, business-specific) ───────────────────────────

    async def dispatch_hook(
        self,
        hook_point: str,
        ctx: GraphContext[Any],
        data: dict[str, Any] | None = None,
    ) -> None:
        """Dispatch a lifecycle hook via ``HookRunner``.

        Maps ``hook_point`` string (a ``ReActHookPoint`` value) to the
        corresponding ``HookPoint`` enum. Wraps ``data: dict`` into
        ``HookPayload(data=data)`` — preserves existing hook payload
        contract. Extracts ``AgentContext`` from ``ctx.user_data`` and
        passes it to ``hook_runner.dispatch``.
        """
        if self._hook_runner is None:
            return
        mapped = self.HOOK_POINT_MAP.get(hook_point)
        if mapped is None:
            return
        agent_ctx: AgentContext = ctx.user_data
        payload = HookPayload(data=data) if data else None
        await self._hook_runner.dispatch(mapped, agent_ctx, payload=payload)

    async def around(
        self,
        scope: str,
        ctx: GraphContext[Any],
        body: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Wrap ``body`` in an interceptor chain for ``scope``.

        ``around`` routes ``ITERATION`` only. ``TOOL_CALL`` and
        ``LLM_STREAM`` are node-local AOP invoked directly via
        ``InterceptorChain`` because their typed contexts are not
        constructible from ``GraphContext`` — see ADR-0034 D2.
        ``LLM_CALL`` is a reserved scope with no wiring
        (``InterceptorChain`` has no ``around_llm_call``).

        ``ITERATION`` scope is fully wired: constructs ``IterationContext``
        from the react state and calls ``around_iteration``.
        """
        if self._interceptor_chain is None:
            return await body()

        if scope == ReActScope.ITERATION:
            return await self._around_iteration(self._interceptor_chain, ctx, body)

        # Unknown scope: defensive fallback. TOOL_CALL / LLM_STREAM /
        # LLM_CALL are not routed through `around` — see ADR-0034 D2.
        return await body()

    async def _around_iteration(
        self,
        chain: InterceptorChain,
        ctx: GraphContext[Any],
        body: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Construct ``IterationContext`` from ``ctx.user_data`` and call
        ``interceptor_chain.around_iteration``.
        """
        from modex_agent.agents.react.state import get_react_state
        from modex_agent.interceptor.abc import IterationContext

        agent_ctx: AgentContext = ctx.user_data
        react_state = get_react_state(agent_ctx)
        iteration = react_state.iteration if react_state is not None else 0
        turn_state: TurnStateBase | None = (
            agent_ctx.runtime.state if agent_ctx.runtime is not None else None
        )
        ic = IterationContext(
            iteration=iteration,
            turn_id=str(agent_ctx.session),
            turn_state=turn_state,
        )
        # ``body`` is ``Callable[[], Awaitable[Any]]`` (from the ABC) but
        # ``around_iteration`` expects ``IterationNext = Callable[[], None]``.
        # The existing ``IterationNext`` alias is imprecise — at runtime
        # ``body`` is an async callable that returns a coroutine, which
        # ``around_iteration`` awaits internally. The cast bridges the type
        # gap without changing the ``IterationNext`` alias (out of scope).
        return await chain.around_iteration(agent_ctx, ic, cast("IterationNext", body))

    async def apply_governance(self, messages: list[Any], ctx: GraphContext[Any]) -> list[Any]:
        """Apply governance (filtering / rewriting) to ``messages`` before LLM call.

        Delegates to the injected ``ContextGovernance`` service. Returns
        ``messages`` unchanged when no governance is configured.
        """
        if self._governance is None:
            return messages
        return await self._governance.apply(messages)

    async def drain_control(self, ctx: GraphContext[Any]) -> None:
        """Drain the control channel for cancellation / injection signals.

        Calls the existing ``drain_control_channel`` helper with the
        ``AgentContext`` extracted from ``ctx.user_data``. Raises
        ``AgentCancelled`` if a ``CANCEL_TURN`` command is pending.
        """
        if self._control_channel is None:
            return
        from modex_agent.hook.builtin.control_drain import drain_control_channel

        agent_ctx: AgentContext = ctx.user_data
        turn_uuid: str | None = None
        if agent_ctx.runtime is not None:
            turn_uuid = agent_ctx.runtime.turn_uuid
        await drain_control_channel(
            self._control_channel,
            agent_ctx,
            turn_uuid=turn_uuid,
        )

    async def capture_snapshot(self, ctx: GraphContext[Any], reason: str) -> None:
        """Capture a turn state snapshot for suspend/resume.

        Calls ``SnapshotPolicy.capture(state, SnapshotReason(reason))`` and
        persists the snapshot via ``TurnStateStore.save_turn(snapshot)``.
        No-op when either ``snapshot_policy`` or ``turn_state_store`` is
        not configured.
        """
        if self._snapshot_policy is None or self._turn_state_store is None:
            return
        from modex_agent.runtime.enums import SnapshotReason

        agent_ctx: AgentContext = ctx.user_data
        if agent_ctx.runtime is None:
            return
        state = agent_ctx.runtime.state
        snapshot = self._snapshot_policy.capture(state, SnapshotReason(reason))
        await self._turn_state_store.save_turn(snapshot)

    async def emit(self, event_type: str, data: Any, ctx: GraphContext[Any]) -> None:
        """Emit a streaming event via ``ContentEmitter``.

        Maps ``event_type`` string (a ``ReActEvent`` value from
        ``constants.py``) to the existing ``ReActEvent`` enum (from
        ``agent.py``) and calls ``emitter.emit``. Unknown event types are
        silently skipped.
        """
        if self._emitter is None:
            return
        from modex_agent.agents.react.agent import ReActEvent

        try:
            react_event = ReActEvent(event_type)
        except ValueError:
            return
        await self._emitter.emit(react_event, data)


__all__ = ["ReactGraphRuntime"]
