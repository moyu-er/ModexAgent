"""AgentRuntime and AgentRuntimeServices — process-level services + turn-level state.

Services are not serialized into snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

from framework.core.llm_struct import RuntimeSafetyPolicy

from .enums import TurnCustomKey
from .models import TurnStateBase

if TYPE_CHECKING:
    import asyncio

    from framework.agents.react.approval import ApprovalRuntime
    from framework.control.channel import InMemoryControlChannel
    from framework.core.runtime_context import RuntimeContextManager
    from framework.hook import HookRunner
    from framework.interceptor.chain import InterceptorChain
    from framework.memory.context_governance import ContextGovernance

    from .store import RuntimeCommandStore, TurnStateStore

TState = TypeVar("TState", bound=TurnStateBase)


@dataclass
class AgentRuntimeServices:
    """Process-scope services. Not persisted — absent in snapshots."""

    hooks: HookRunner | None = None
    interceptors: InterceptorChain | None = None
    approval: ApprovalRuntime | None = None
    governance: ContextGovernance | None = None
    turn_store: TurnStateStore | None = None
    command_store: RuntimeCommandStore | None = None
    pending_input_queue: asyncio.Queue[str] | None = None
    safety: RuntimeSafetyPolicy = field(default_factory=RuntimeSafetyPolicy)
    runtime_context_manager: RuntimeContextManager | None = None
    control_channel: InMemoryControlChannel | None = None


@dataclass
class AgentRuntime:
    """Runtime = services (process-local) + state (turn-local, snapshot-able)."""

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

    @property
    def turn_uuid(self) -> str | None:
        """Current turn UUID for control command scoping."""
        return self.state.custom.get(TurnCustomKey.TURN_UUID)

    @property
    def control_channel(self) -> InMemoryControlChannel | None:
        """InMemoryControlChannel for control command consumption."""
        return self.services.control_channel


def require_runtime_state(runtime: AgentRuntime, state_type: type[TState]) -> TState:
    """Validate and narrow runtime state to the expected mode-specific type."""
    if isinstance(runtime.state, state_type):
        return runtime.state
    raise TypeError(
        f"runtime state must be {state_type.__name__}, got {type(runtime.state).__name__}"
    )
