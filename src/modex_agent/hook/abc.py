"""Hook 抽象基类与核心类型。

定义 HookPoint 枚举、Hook 协议、HookPayload / HookResult / HookSpec 等核心数据类。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.emitter import AgentResult
    from modex_agent.core.tool_manager import ToolResult
    from modex_agent.core.types import LLMResponse, ToolCall


class HookPoint(str, Enum):
    """Hook 调度点枚举。

    HookRunner.dispatch 通过 _HOOK_DISPATCH 字典将每个 HookPoint 映射到对应的
    ABC 类与调用函数，再用 isinstance(hook, dispatch_cls) 检查决定是否调用该 hook。
    """

    BEFORE_TURN = "before_turn"
    AFTER_TURN = "after_turn"
    BEFORE_ITERATION = "before_iteration"
    AFTER_ITERATION = "after_iteration"
    BEFORE_TOOL_EXECUTION = "before_tool_execution"
    AFTER_TOOL_EXECUTION = "after_tool_execution"
    AFTER_LLM_RESPONSE = "after_llm_response"
    FINALIZE_CONTENT = "finalize_content"
    FINALLY_TURN = "finally_turn"


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
class HookSpec:
    """Hook 注册规格。

    包含 hook 实例和错误处理策略，后续用于代码装配。
    """

    hook: Hook
    on_error: HookErrorPolicy = HookErrorPolicy.LOG


class Hook(ABC):
    """All hooks' public base class.

    Replaces the old Protocol. Each concrete hook inherits from one or more
    per-point ABCs (BeforeTurnHook, AfterTurnHook, etc.).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique hook name for logging and diagnostics."""
        ...


class BeforeTurnHook(Hook):
    _hook_point = HookPoint.BEFORE_TURN

    @abstractmethod
    async def before_turn(self, ctx: AgentContext) -> None: ...


class AfterTurnHook(Hook):
    _hook_point = HookPoint.AFTER_TURN

    @abstractmethod
    async def after_turn(self, ctx: AgentContext, result: AgentResult) -> None: ...


class BeforeIterationHook(Hook):
    _hook_point = HookPoint.BEFORE_ITERATION

    @abstractmethod
    async def before_iteration(self, ctx: AgentContext) -> None: ...


class AfterIterationHook(Hook):
    _hook_point = HookPoint.AFTER_ITERATION

    @abstractmethod
    async def after_iteration(self, ctx: AgentContext) -> None: ...


class BeforeToolExecutionHook(Hook):
    _hook_point = HookPoint.BEFORE_TOOL_EXECUTION

    @abstractmethod
    async def before_tool_execution(
        self, ctx: AgentContext, tool_calls: Sequence[ToolCall]
    ) -> None: ...


class AfterToolExecutionHook(Hook):
    _hook_point = HookPoint.AFTER_TOOL_EXECUTION

    @abstractmethod
    async def after_tool_execution(
        self, ctx: AgentContext, results: Sequence[ToolResult]
    ) -> None: ...


class AfterLLMResponseHook(Hook):
    _hook_point = HookPoint.AFTER_LLM_RESPONSE

    @abstractmethod
    async def after_llm_response(self, ctx: AgentContext, response: LLMResponse) -> None: ...


class FinalizeContentHook(Hook):
    _hook_point = HookPoint.FINALIZE_CONTENT

    @abstractmethod
    def finalize_content(self, ctx: AgentContext, content: str | None) -> str | None: ...


class FinallyTurnHook(Hook):
    _hook_point = HookPoint.FINALLY_TURN

    @abstractmethod
    async def finally_turn(self, ctx: AgentContext, result: AgentResult | None) -> None: ...
