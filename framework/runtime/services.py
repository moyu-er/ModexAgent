"""AgentRuntime and AgentRuntimeServices — process-level services + turn-level state.

Services are not serialized into snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeVar

from framework.core.llm_error import RuntimeSafetyPolicy

from .models import TurnStateBase

if TYPE_CHECKING:
    import asyncio
    from framework.hook import HookRunner
    from framework.interceptor.chain import InterceptorChain
    from framework.control.runtime import ControlRuntime
    from framework.agents.react.approval import ApprovalRuntime as _ApprovalRuntime
    from framework.memory.context_governance import ContextGovernance
    from .store import TurnStateStore, RuntimeCommandStore

TState = TypeVar("TState", bound=TurnStateBase)


@dataclass
class AgentRuntimeServices:
    """Process-scope services. Not persisted — absent in snapshots."""

    hooks: HookRunner | None = None
    interceptors: InterceptorChain | None = None
    control: ControlRuntime | None = None
    approval: _ApprovalRuntime | None = None
    governance: ContextGovernance | None = None
    turn_store: TurnStateStore | None = None
    command_store: RuntimeCommandStore | None = None
    pending_input_queue: asyncio.Queue[str] | None = None
    safety: RuntimeSafetyPolicy = field(default_factory=RuntimeSafetyPolicy)


@dataclass
class AgentRuntime:
    """Runtime = services (process-local) + state (turn-local, snapshot-able)."""

    services: AgentRuntimeServices
    state: TurnStateBase


def require_runtime_state(runtime: AgentRuntime, state_type: type[TState]) -> TState:
    """Validate and narrow runtime state to the expected mode-specific type."""
    if isinstance(runtime.state, state_type):
        return runtime.state
    raise TypeError(
        f"runtime state must be {state_type.__name__}, got {type(runtime.state).__name__}"
    )
