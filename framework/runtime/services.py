"""AgentRuntime and AgentRuntimeServices — process-level services + turn-level state.

Services are not serialized into snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeVar

from framework.core.llm_error import RuntimeSafetyPolicy

from .models import TurnStateBase

TState = TypeVar("TState", bound=TurnStateBase)


@dataclass
class AgentRuntimeServices:
    """Process-scope services. Not persisted — absent in snapshots."""

    hooks: object | None = None
    interceptors: object | None = None
    control: object | None = None
    approval: object | None = None
    governance: object | None = None
    turn_store: object | None = None
    command_store: object | None = None
    pending_input_queue: object | None = None
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
