"""Runtime 装配器 — ReActRuntime + ApprovalRuntime + ControlRuntime 的唯一构造入口。

contract invariant (design_doc/2026-05-04-runtime-contract-design.md §3.3):
    全仓只有 RuntimeAssembler.assemble() 构造 ReActRuntime / ApprovalRuntime 实例。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from framework.agents.react.approval import ApprovalRuntime
from framework.agents.react.runtime import ReActRuntime
from framework.control.channel import ControlChannel
from framework.control.runtime import ControlRuntime
from framework.control.store import ControlStore
from framework.control.types import ControlCommandType
from framework.hook.runner import HookRunner
from framework.interceptor.abc import Interceptor
from framework.interceptor.chain import InterceptorChain
from framework.interceptor.handler import CommandHandlerRegistry


@dataclass
class RuntimeServicesConfig:
    """Runtime 装配配置 — 框架通用, 不含业务特化逻辑。

    所有字段默认为 None: clean mode 下全部跳过, full mode 下由 consumer 按需填写。
    """

    mode: Literal["clean", "full"] = "full"
    hooks: HookRunner | None = None
    interceptors: list[Interceptor] | None = None
    approval_classifier: Any = None            # ApprovalClassifier (Protocol)
    approval_strategy: Any = None              # SuspendStrategy (ABC)
    project_root: Path | None = None           # NEW: for ArgumentMatcher path resolution
    control_channel: ControlChannel | None = None
    control_store: ControlStore | None = None
    command_handlers: list[tuple[ControlCommandType, Any]] | None = None
    checkpoint_store: Any = None               # RuntimeStateStore
    injection_queue: asyncio.Queue[str] | None = None
    governance: Any = None                     # ContextGovernance
    safety: Any = None                         # RuntimeSafetyPolicy


class RuntimeAssembler:
    """ReActRuntime + ApprovalRuntime + ControlRuntime 唯一装配入口。"""

    @staticmethod
    async def assemble(config: RuntimeServicesConfig) -> ReActRuntime:
        if config.mode == "clean":
            return ReActRuntime.clean()

        interceptor_chain = (
            InterceptorChain(list(config.interceptors))
            if config.interceptors is not None
            else None
        )

        approval = None
        if config.approval_classifier and config.approval_strategy:
            approval = ApprovalRuntime(
                classifier=config.approval_classifier,
                suspend_strategy=config.approval_strategy,
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

        return ReActRuntime(
            mode="full",
            hooks=config.hooks,
            interceptors=interceptor_chain,
            approval=approval,
            control=control,
            checkpoint_store=config.checkpoint_store,
            injection_queue=config.injection_queue,
            governance=config.governance,
            safety=config.safety,
        )
