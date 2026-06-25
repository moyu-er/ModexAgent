"""Runtime assembler — AgentRuntime + ApprovalRuntime assembly entry point.

Contract invariant:
    Only RuntimeAssembler.assemble() constructs AgentRuntime / ApprovalRuntime.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from modex_agent.agents.react.approval import ApprovalRuntime
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.session_id import SessionInfo
from modex_agent.hook.runner import HookRunner
from modex_agent.interceptor.abc import Interceptor
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.runtime.enums import AgentKind, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices


@dataclass
class RuntimeServicesConfig:
    """Runtime assembly config — framework-generic, no business logic.

    All fields default to None: clean mode skips everything,
    full mode fills in as needed.
    """

    mode: Literal["clean", "full"] = "full"
    hooks: HookRunner | None = None
    interceptors: list[Interceptor] | None = None
    approval_classifier: Any = None
    project_root: Path | None = None
    turn_store: Any = None
    injection_queue: asyncio.Queue[str] | None = None
    governance: Any = None
    safety: Any = None
    control_channel: Any = None  # InMemoryControlChannel — use Any to avoid circular import


class RuntimeAssembler:
    """AgentRuntime + ApprovalRuntime assembly entry point."""

    @staticmethod
    async def assemble(config: RuntimeServicesConfig) -> AgentRuntime:
        if config.mode == "clean":
            state = ReActTurnState(
                identity=TurnIdentity(agent_id="clean", session=SessionInfo.from_str("clean"), turn_id="clean"),
                agent_kind=AgentKind.REACT,
                phase=TurnPhase.CREATED,
            )
            return AgentRuntime(services=AgentRuntimeServices(), state=state)

        interceptor_chain = (
            InterceptorChain(list(config.interceptors)) if config.interceptors is not None else None
        )

        approval = None
        if config.approval_classifier:
            approval = ApprovalRuntime(
                classifier=config.approval_classifier,
            )

        state = ReActTurnState(
            identity=TurnIdentity(agent_id="react", session=SessionInfo.from_str("assembled"), turn_id="initial"),
            agent_kind=AgentKind.REACT,
            phase=TurnPhase.CREATED,
        )
        services = AgentRuntimeServices(
            hooks=config.hooks,
            interceptors=interceptor_chain,
            approval=approval,
            governance=config.governance,
            turn_store=config.turn_store,
            pending_input_queue=config.injection_queue,
            control_channel=config.control_channel,  # NEW: wire control channel
        )
        if config.safety is not None:
            services.safety = config.safety
        return AgentRuntime(services=services, state=state)
