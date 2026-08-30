"""Hook 抽象基类与核心类型。

定义 HookPoint 枚举、Hook 协议、HookPayload / HookSpec 等核心数据类。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.emitter import AgentResult
    from modex_agent.core.message import ChatMessage
    from modex_agent.core.tool_manager import ToolResult
    from modex_agent.core.types import LLMResponse, ToolCall
    from modex_agent.runtime.models import ApprovalTransaction


class HookPoint(StrEnum):
    """Hook 调度点枚举。

    HookRunner.dispatch 通过 _HOOK_DISPATCH 字典将每个 HookPoint 映射到对应的
    ABC 类与调用函数，再用 isinstance(hook, dispatch_cls) 检查决定是否调用该 hook。
    """

    BEFORE_GRAPH = "before_graph"
    AFTER_GRAPH = "after_graph"
    FINALLY_GRAPH = "finally_graph"
    START_NODE_TURN = "start_node_turn"
    END_NODE_TURN = "end_node_turn"
    BEFORE_TURN = "before_turn"
    AFTER_TURN = "after_turn"
    BEFORE_ITERATION = "before_iteration"
    AFTER_ITERATION = "after_iteration"
    BEFORE_TOOL_EXECUTION = "before_tool_execution"
    AFTER_TOOL_EXECUTION = "after_tool_execution"
    AFTER_LLM_RESPONSE = "after_llm_response"
    FINALIZE_CONTENT = "finalize_content"
    BEFORE_LLM = "before_llm"
    AFTER_APPROVAL = "after_approval"


class HookErrorPolicy(StrEnum):
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
class HookSpec:
    """Hook 注册规格。

    包含 hook 实例和错误处理策略，后续用于代码装配。
    """

    hook: Hook
    on_error: HookErrorPolicy = HookErrorPolicy.LOG
    priority: int = 0


class Hook(ABC):  # noqa: B024
    """All hooks' public base class.

    Replaces the old Protocol. Each concrete hook inherits from one or more
    per-point ABCs (BeforeTurnHook, AfterTurnHook, etc.).
    """

    @property
    def name(self) -> str:
        """Unique hook name for logging and diagnostics."""
        return type(self).__name__


class ClosableHook(Hook):
    """Hook that owns process-lifetime resources released at pipeline stop."""

    @abstractmethod
    async def aclose(self) -> None: ...


class BeforeGraphHook(Hook):
    """Graph-level hook — fires once per actual_turn() call.

    ⚠️ Approval resume re-enters actual_turn(), causing this hook to fire again.
    Avoid mutating ctx.history — use StartNodeTurnHook or BeforeTurnHook instead.
    """

    _hook_point = HookPoint.BEFORE_GRAPH

    @abstractmethod
    async def before_graph(self, ctx: AgentContext) -> None: ...


class AfterGraphHook(Hook):
    """Graph-level hook — fires once per actual_turn() call.

    ⚠️ Approval resume re-enters actual_turn(), causing this hook to fire again.
    Avoid mutating ctx.history — use StartNodeTurnHook or BeforeTurnHook instead.
    """

    _hook_point = HookPoint.AFTER_GRAPH

    @abstractmethod
    async def after_graph(self, ctx: AgentContext, result: AgentResult) -> None: ...


class FinallyGraphHook(Hook):
    """Graph-level hook — fires once per actual_turn() call.

    ⚠️ Approval resume re-enters actual_turn(), causing this hook to fire again.
    Avoid mutating ctx.history — use StartNodeTurnHook or BeforeTurnHook instead.

    ``result=None`` is the GraphInterrupt (approval suspend) signature: the
    turn has NOT ended and will re-enter actual_turn() on resume. Hooks whose
    side effects must fire once per logical turn (notifications, deliveries)
    should inherit OutcomeFinallyHook instead of guarding manually.
    """

    _hook_point = HookPoint.FINALLY_GRAPH

    @abstractmethod
    async def finally_graph(self, ctx: AgentContext, result: AgentResult | None) -> None: ...


class OutcomeFinallyHook(FinallyGraphHook):
    """Template-method base for outcome-dependent FINALLY_GRAPH hooks.

    ``finally_graph`` skips the suspend leg (``result is None``) and only
    calls ``on_outcome`` for terminal legs — the safety default that removes
    the interpretation of ``None`` from every consumer. Forgetting the guard
    becomes structurally impossible: a subclass never sees a suspend dispatch.
    """

    async def finally_graph(self, ctx: AgentContext, result: AgentResult | None) -> None:
        if result is None:
            return
        await self.on_outcome(ctx, result)

    @abstractmethod
    async def on_outcome(self, ctx: AgentContext, result: AgentResult) -> None: ...


def is_suspend_leg(result: AgentResult | None, error: Exception | None = None) -> bool:
    """Whether a FINALLY_GRAPH dispatch is the approval-suspend leg.

    ``result is None`` with no ``error`` is the GraphInterrupt signature: the
    turn has not ended and re-enters actual_turn() on resume. A terminal leg
    always carries a concrete ``AgentResult``; a genuine crash dispatches
    ``error`` alongside ``result=None``. The single authority for this
    interpretation — RootSpanHook uses it directly because it also handles
    the error variant and cannot inherit the template method.
    """
    return result is None and error is None


class StartNodeTurnHook(Hook):
    _hook_point = HookPoint.START_NODE_TURN

    @abstractmethod
    async def start_node_turn(self, ctx: AgentContext) -> None: ...


class EndNodeTurnHook(Hook):
    _hook_point = HookPoint.END_NODE_TURN

    @abstractmethod
    async def end_node_turn(self, ctx: AgentContext) -> None: ...


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


class BeforeLLMHook(Hook):
    """Pre-LLM-call observation hook.

    Fires before the LLM provider is called within the ReAct LLM node.
    The ``request`` payload carries the typed messages being sent to the
    provider, enabling prompt capture (G2) and LLM-call duration timing (G1).
    Observation-only — does NOT veto or modify the request.
    """

    _hook_point = HookPoint.BEFORE_LLM

    @abstractmethod
    async def before_llm(self, ctx: AgentContext, request: Sequence[ChatMessage]) -> None: ...


class AfterApprovalHook(Hook):
    """Post-approval-decision observation hook.

    Fires after the approval decision is applied to ``ApprovalTransaction``
    and before the graph resumes execution. Enables approval-span measurement
    (G3). Observation-only — does NOT veto or modify the decision.
    """

    _hook_point = HookPoint.AFTER_APPROVAL

    @abstractmethod
    async def after_approval(self, ctx: AgentContext, transaction: ApprovalTransaction) -> None: ...
