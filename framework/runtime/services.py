"""AgentRuntime and AgentRuntimeServices — process-level services + turn-level state.

Services are not serialized into snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

from framework.core.llm_error import RuntimeSafetyPolicy

from .models import TurnStateBase

if TYPE_CHECKING:
    import asyncio
    from framework.hook import HookRunner
    from framework.interceptor.chain import InterceptorChain
    from framework.control.runtime import ControlRuntime
    from framework.agents.react.approval import ApprovalRuntime
    from framework.core.runtime_context import RuntimeContext, RuntimeContextManager
    from framework.memory.context_governance import ContextGovernance
    from .store import TurnStateStore, RuntimeCommandStore

TState = TypeVar("TState", bound=TurnStateBase)


@dataclass
class AgentRuntimeServices:
    """Process-scope services. Not persisted — absent in snapshots."""

    hooks: HookRunner | None = None
    interceptors: InterceptorChain | None = None
    control: ControlRuntime | None = None
    approval: ApprovalRuntime | None = None
    governance: ContextGovernance | None = None
    turn_store: TurnStateStore | None = None
    command_store: RuntimeCommandStore | None = None
    pending_input_queue: asyncio.Queue[str] | None = None
    safety: RuntimeSafetyPolicy = field(default_factory=RuntimeSafetyPolicy)
    runtime_context_manager: RuntimeContextManager | None = None


@dataclass
class AgentRuntime:
    """Runtime = services (process-local) + state (turn-local, snapshot-able).

    Delegation properties (hooks, interceptors, control, etc.) are provided
    for backward compatibility with code that accesses services directly on
    the runtime object.
    """

    services: AgentRuntimeServices
    state: TurnStateBase
    _runtime_context: Any = field(default=None, repr=False)

    @property
    def hooks(self) -> HookRunner | None:
        return self.services.hooks

    @property
    def interceptors(self) -> InterceptorChain | None:
        return self.services.interceptors

    @property
    def control(self) -> ControlRuntime | None:
        return self.services.control

    @property
    def approval(self) -> ApprovalRuntime | None:
        return self.services.approval

    @property
    def governance(self) -> ContextGovernance | None:
        return self.services.governance

    @property
    def turn_store(self) -> TurnStateStore | None:
        return self.services.turn_store

    @property
    def command_store(self) -> RuntimeCommandStore | None:
        return self.services.command_store

    @property
    def injection_queue(self) -> asyncio.Queue[str] | None:
        return self.services.pending_input_queue

    @property
    def pending_input_queue(self) -> asyncio.Queue[str] | None:
        return self.services.pending_input_queue

    @property
    def safety(self) -> RuntimeSafetyPolicy:
        return self.services.safety

    def validate(self) -> None:
        """Validate runtime configuration. No-op for new AgentRuntime."""
        if self.services.interceptors is not None and self.services.control is None:
            import logging
            logger = logging.getLogger(__name__)
            from framework.interceptor.builtin import ControlDrainInterceptor
            for interceptor in self.services.interceptors.interceptors:
                if isinstance(interceptor, ControlDrainInterceptor):
                    from framework.control.exceptions import PolicyViolation
                    raise PolicyViolation(
                        "ControlDrainInterceptor configured but no ControlRuntime present"
                    )


def require_runtime_state(runtime: AgentRuntime, state_type: type[TState]) -> TState:
    """Validate and narrow runtime state to the expected mode-specific type."""
    if isinstance(runtime.state, state_type):
        return runtime.state
    raise TypeError(
        f"runtime state must be {state_type.__name__}, got {type(runtime.state).__name__}"
    )
