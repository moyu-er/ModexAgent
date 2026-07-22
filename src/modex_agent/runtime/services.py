"""AgentRuntime and AgentRuntimeServices — process-level services + turn-level state.

Services are not serialized into snapshots.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.control.types import ControlCommand, ControlScope
from modex_agent.ioc.configs.llm import ModelCapabilities

from .enums import SnapshotReason, TurnCustomKey
from .models import TurnStateBase, TurnSnapshot

if TYPE_CHECKING:
    import asyncio

    from modex_agent.agents.react.approval import ApprovalRuntime
    from modex_agent.agents.react.runtime import ReactGraphRuntime
    from modex_agent.control.channel import InMemoryControlChannel
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.runtime_context import RuntimeContextManager
    from modex_agent.hook import HookRunner
    from modex_agent.interceptor.chain import InterceptorChain
    from modex_agent.memory.context_governance import ContextGovernance

    from .store import TurnStateStore
    from modex_agent.trace.otel_store import OtelSpanTraceStore

logger = logging.getLogger(__name__)

TState = TypeVar("TState", bound=TurnStateBase)


@dataclass
class AgentRuntimeServices:
    """Process-scope services. Not persisted — absent in snapshots."""

    hooks: HookRunner | None = None
    interceptors: InterceptorChain | None = None
    approval: ApprovalRuntime | None = None
    governance: ContextGovernance | None = None
    turn_store: TurnStateStore | None = None
    trace_store: OtelSpanTraceStore | None = None
    pending_input_queue: asyncio.Queue[str] | None = None
    safety: RuntimeSafetyPolicy = field(default_factory=RuntimeSafetyPolicy)
    runtime_context_manager: RuntimeContextManager | None = None
    control_channel: InMemoryControlChannel | None = None
    model_capabilities: ModelCapabilities | None = None


@dataclass
class AgentRuntime:
    """Runtime = services (process-local) + state (turn-local, snapshot-able).

    Field-access properties (hooks, interceptors, etc.) remain for backward
    compatibility, but callers should prefer the operation methods below
    (snapshot_turn, drain_control) which concentrate the common patterns
    and handle the absent-subsystem case internally.

    ``graph_runtime`` carries the ``ReactGraphRuntime`` adapter (ADR-0033 D5 +
    ticket 04). Nodes call ``ctx.runtime.graph_runtime.dispatch_hook(...)`` /
    ``around(...)`` / ``drain_control(...)`` / ``apply_governance(...)`` /
    ``capture_snapshot(...)`` / ``emit(...)`` instead of reaching directly
    into ``hooks`` / ``interceptors`` / ``governance`` / ``control_channel``
    / ``turn_store`` / ``emitter``. ``ReActAgent.run()`` constructs and
    assigns it once per turn; tests that bypass ``run()`` must assign it
    themselves (or use the no-op default ``ReactGraphRuntime()``).
    """

    services: AgentRuntimeServices
    state: TurnStateBase
    _runtime_context: Any = field(default=None, repr=False)
    # ADR-0033 D5 + ticket 04: graph-runtime AOP bridge. Set by ReActAgent.run().
    graph_runtime: ReactGraphRuntime | None = None

    # ------------------------------------------------------------------
    # Field-access properties (backward compat)
    # ------------------------------------------------------------------

    @property
    def hooks(self) -> HookRunner | None:
        return self.services.hooks

    @property
    def interceptors(self) -> InterceptorChain | None:
        return self.services.interceptors

    @property
    def approval(self) -> ApprovalRuntime | None:
        return self.services.approval

    @property
    def governance(self) -> ContextGovernance | None:
        return self.services.governance

    @property
    def model_capabilities(self) -> ModelCapabilities | None:
        return self.services.model_capabilities

    @property
    def turn_store(self) -> TurnStateStore | None:
        return self.services.turn_store

    @property
    def trace_store(self) -> OtelSpanTraceStore | None:
        return self.services.trace_store

    @property
    def injection_queue(self) -> asyncio.Queue[str] | None:
        return self.services.pending_input_queue

    @property
    def pending_input_queue(self) -> asyncio.Queue[str] | None:
        return self.services.pending_input_queue

    @property
    def safety(self) -> RuntimeSafetyPolicy:
        return self.services.safety

    @property
    def turn_uuid(self) -> str | None:
        """Current turn UUID for control command scoping."""
        return self.state.custom.get(TurnCustomKey.TURN_UUID)

    @property
    def control_channel(self) -> InMemoryControlChannel | None:
        """InMemoryControlChannel for control command consumption."""
        return self.services.control_channel

    # ------------------------------------------------------------------
    # Operation methods — prefer these over field access
    # ------------------------------------------------------------------
    async def save_snapshot(self, snapshot: TurnSnapshot) -> bool:
        """Persist a turn snapshot to the store.

        Delegates to turn_store when present; returns False when no store
        is attached. Replaces the ``if runtime.turn_store: await ...save_turn()``
        pattern. The snapshot is built by the caller (e.g. via
        ReActSnapshotPolicy().capture()) since construction is agent-specific.
        """
        store = self.services.turn_store
        if store is None:
            return False
        await store.save_turn(snapshot)
        return True

    async def drain_control(
        self,
        ctx: AgentContext,
        *,
        turn_uuid: str | None = None,
    ) -> bool:
        """Drain cancel/inject control commands at a safe point.

        Delegates to the shared ``drain_control_channel`` utility, passing the
        runtime's control channel. Returns True if any command was consumed.
        Returns False when no channel is attached.
        """
        channel = self.services.control_channel
        if channel is None:
            return False
        from modex_agent.hook.builtin.control_drain import drain_control_channel

        return await drain_control_channel(
            channel,
            ctx,
            turn_uuid=turn_uuid or self.turn_uuid,
        )


def require_runtime_state(runtime: AgentRuntime, state_type: type[TState]) -> TState:
    """Validate and narrow runtime state to the expected mode-specific type."""
    if isinstance(runtime.state, state_type):
        return runtime.state
    raise TypeError(
        f"runtime state must be {state_type.__name__}, got {type(runtime.state).__name__}"
    )
