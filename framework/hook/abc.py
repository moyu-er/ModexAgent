"""Hook 抽象基类与核心类型。

定义 HookPoint 枚举、Hook 协议、HookPayload / HookResult / HookSpec 等核心数据类。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Generic, Protocol
from typing_extensions import TypeVar

if TYPE_CHECKING:
    from framework.core.agent import AgentContext
    from framework.core.emitter import AgentResult
    from framework.core.tool_manager import ToolResult
    from framework.core.types import LLMResponse, ToolCall

R = TypeVar("R", default=Any)


class HookPoint(str, Enum):
    """Hook 调度点枚举。

    枚举值必须等于 Hook 对象上的方法名，HookRunner 通过 getattr(hook, hook_point.value) 调度。
    """

    BEFORE_TURN = "before_turn"
    AFTER_TURN = "after_turn"
    BEFORE_ITERATION = "before_iteration"
    AFTER_ITERATION = "after_iteration"
    BEFORE_TOOL_EXECUTION = "before_tool_execution"
    AFTER_TOOL_EXECUTION = "after_tool_execution"
    AFTER_LLM_RESPONSE = "after_llm_response"
    ON_CONTROL_COMMAND = "on_control_command"
    FINALIZE_CONTENT = "finalize_content"


class HookErrorPolicy(str, Enum):
    """Hook 执行错误处理策略。"""

    IGNORE = "ignore"
    LOG = "log"
    ABORT = "abort"


@dataclass(frozen=True)
class HookPayload:
    """Hook 统一承载结构。

    data 字段存放钩子点相关的任意数据。
    """

    data: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class HookResult:
    """Hook 执行结果，表达轻量级决策。

    veto=True 表示该 hook 否决当前操作（轻量拒绝，不退出 agent）。
    content_override 非空时覆盖 LLM 输出内容。
    """

    veto: bool = False
    content_override: str | None = None

    @classmethod
    def pass_through(cls) -> HookResult:
        """返回放行结果（默认）。"""
        return cls(veto=False, content_override=None)

    @classmethod
    def veto_result(cls) -> HookResult:
        """返回否决结果。"""
        return cls(veto=True, content_override=None)


@dataclass(frozen=True)
class HookSpec(Generic[R]):
    """Hook 注册规格。

    包含 hook 实例和错误处理策略，后续用于代码装配。
    """

    hook: Hook[R]
    on_error: HookErrorPolicy = HookErrorPolicy.LOG


class Hook(Protocol, Generic[R]):
    """Hook 协议 —— 生命周期扩展点。

    所有方法均为可选，HookRunner 通过 getattr 按 HookPoint 值调度。
    子类可选择性覆盖所需方法。
    """

    async def before_turn(self, ctx: AgentContext[R]) -> None:
        """在 Agent.run() 开始时、while 循环之前调用，且只调用一次。"""
        ...

    async def after_turn(
        self,
        ctx: AgentContext[R],
        result: AgentResult,
    ) -> None:
        """在 Agent.run() 结束后调用（无论成功、失败或达到最大迭代次数），且只调用一次。"""
        ...

    async def before_iteration(self, ctx: AgentContext[R]) -> None:
        """每次迭代开始前调用。"""
        ...

    async def after_iteration(self, ctx: AgentContext[R]) -> None:
        """每次迭代结束后调用。"""
        ...

    async def before_tool_execution(
        self,
        ctx: AgentContext[R],
        tool_calls: Sequence[ToolCall],
    ) -> None:
        """工具执行前调用。"""
        ...

    async def after_tool_execution(
        self,
        ctx: AgentContext[R],
        results: Sequence[ToolResult],
    ) -> None:
        """工具执行后调用。"""
        ...

    async def after_llm_response(
        self,
        ctx: AgentContext[R],
        response: LLMResponse,
    ) -> None:
        """LLM 完整响应返回后调用。"""
        ...

    async def on_control_command(
        self,
        ctx: AgentContext[R],
        command: Any,
    ) -> HookResult:
        """接收控制命令时调用，可返回 HookResult(veto=True) 拒绝命令。"""
        ...

    def finalize_content(
        self,
        ctx: AgentContext[R],
        content: str | None,
    ) -> str | None:
        """最终内容调整（同步）。"""
        ...
