"""Runtime 装配器 — AgentRuntime + ApprovalRuntime + ControlRuntime 的唯一构造入口。

contract invariant:
    全仓只有 RuntimeAssembler.assemble() 构造 AgentRuntime / ApprovalRuntime 实例。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from framework.agents.react.approval import ApprovalRuntime
from framework.agents.react.state import ReActTurnState
from framework.control.channel import ControlChannel
from framework.control.runtime import ControlRuntime
from framework.control.store import ControlStore
from framework.control.types import ControlCommandType
from framework.hook.runner import HookRunner
from framework.interceptor.abc import Interceptor
from framework.interceptor.chain import InterceptorChain
from framework.interceptor.handler import CommandHandlerRegistry
from framework.runtime.enums import AgentKind, TurnPhase
from framework.runtime.models import TurnIdentity
from framework.runtime.services import AgentRuntime, AgentRuntimeServices


@dataclass
class RuntimeServicesConfig:
    """Runtime 装配配置 — 框架通用, 不含业务特化逻辑。

    所有字段默认为 None: clean mode 下全部跳过, full mode 下由 consumer 按需填写。
    """

    mode: Literal["clean", "full"] = "full"
    hooks: HookRunner | None = None
    interceptors: list[Interceptor] | None = None
    approval_classifier: Any = None            # ApprovalClassifier (Protocol)
    project_root: Path | None = None           # NEW: for ArgumentMatcher path resolution
    control_channel: ControlChannel | None = None
    control_store: ControlStore | None = None
    command_handlers: list[tuple[ControlCommandType, Any]] | None = None
    checkpoint_store: Any = None               # TurnStateStore (DEPRECATED renamed to turn_store)
    turn_store: Any = None                     # TurnStateStore
    injection_queue: asyncio.Queue[str] | None = None
    governance: Any = None                     # ContextGovernance
    safety: Any = None                         # RuntimeSafetyPolicy


class RuntimeAssembler:
    """AgentRuntime + ApprovalRuntime + ControlRuntime 唯一装配入口。"""

    @staticmethod
    async def assemble(config: RuntimeServicesConfig) -> AgentRuntime:
        if config.mode == "clean":
            state = ReActTurnState(
                identity=TurnIdentity(agent_id="clean", session_id="clean", turn_id="clean"),
                agent_kind=AgentKind.REACT,
                phase=TurnPhase.CREATED,
            )
            return AgentRuntime(services=AgentRuntimeServices(), state=state)

        interceptor_chain = (
            InterceptorChain(list(config.interceptors))
            if config.interceptors is not None
            else None
        )

        approval = None
        if config.approval_classifier:
            approval = ApprovalRuntime(
                classifier=config.approval_classifier,
            )

        control = None
        if config.control_channel and config.control_store:
            registry = CommandHandlerRegistry()
            for cmd_type, handler in (config.command_handlers or []):
                registry.register_for(cmd_type, handler)
            control = ControlRuntime(
                channel=config.control_channel,
                store=config.control_store,
                registry=registry,
            )

        state = ReActTurnState(
            identity=TurnIdentity(agent_id="react", session_id="assembled", turn_id="initial"),
            agent_kind=AgentKind.REACT,
            phase=TurnPhase.CREATED,
        )
        services = AgentRuntimeServices(
            hooks=config.hooks,
            interceptors=interceptor_chain,
            control=control,
            approval=approval,
            governance=config.governance,
            turn_store=config.turn_store if config.turn_store is not None else config.checkpoint_store,
            pending_input_queue=config.injection_queue,
        )
        if config.safety is not None:
            services.safety = config.safety
        return AgentRuntime(services=services, state=state)
