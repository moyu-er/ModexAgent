"""HookRunner —— Hook 调度执行器。

按 HookPoint 调度所有注册的 Hook，使用 isinstance 检查替代 getattr 实现类型安全的分发。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, TypedDict, Unpack

from modex_agent.control.exceptions import AgentControlError
from modex_agent.hook.abc import (
    AfterApprovalHook,
    AfterIterationHook,
    AfterLLMResponseHook,
    AfterToolExecutionHook,
    AfterTurnHook,
    BeforeIterationHook,
    BeforeLLMHook,
    BeforeToolExecutionHook,
    BeforeTurnHook,
    FinalizeContentHook,
    FinallyTurnHook,
    HookErrorPolicy,
    HookPayload,
    HookPoint,
    HookSpec,
)

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.emitter import AgentResult
    from modex_agent.core.message import ChatMessage
    from modex_agent.core.tool_manager import ToolResult
    from modex_agent.core.types import LLMResponse, ToolCall
    from modex_agent.runtime.models import ApprovalTransaction

logger = logging.getLogger(__name__)

# 默认超时：每个 hook 方法调用的最大时间（秒）
_DEFAULT_HOOK_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# Per-point typed payloads (TypedDict for static checking, no runtime cost)
# ---------------------------------------------------------------------------


class _EmptyPayload(TypedDict, total=False):
    """No extra data."""


class _AfterTurnPayload(TypedDict, total=False):
    result: AgentResult | None


class _ToolExecutionPayload(TypedDict, total=False):
    tool_calls: Sequence[ToolCall] | None


class _ToolResultsPayload(TypedDict, total=False):
    results: Sequence[ToolResult] | None


class _AfterLLMResponsePayload(TypedDict, total=False):
    response: LLMResponse | None


class _FinalizeContentPayload(TypedDict, total=False):
    content: str | None


class _FinallyTurnPayload(TypedDict, total=False):
    result: AgentResult | None


class _BeforeLLMPayload(TypedDict, total=False):
    request: Sequence[ChatMessage] | None


class _AfterApprovalPayload(TypedDict, total=False):
    transaction: ApprovalTransaction | None


# ---------------------------------------------------------------------------
# Per-point dispatch helpers — eliminate getattr from the hot path
# ---------------------------------------------------------------------------


async def _call_before_turn(
    hook: BeforeTurnHook, ctx: AgentContext, **_: Unpack[_EmptyPayload]
) -> None:
    await hook.before_turn(ctx)


async def _call_after_turn(
    hook: AfterTurnHook, ctx: AgentContext, **kw: Unpack[_AfterTurnPayload]
) -> None:
    await hook.after_turn(ctx, kw.get("result"))  # type: ignore[arg-type]


async def _call_before_iteration(
    hook: BeforeIterationHook, ctx: AgentContext, **_: Unpack[_EmptyPayload]
) -> None:
    await hook.before_iteration(ctx)


async def _call_after_iteration(
    hook: AfterIterationHook, ctx: AgentContext, **_: Unpack[_EmptyPayload]
) -> None:
    await hook.after_iteration(ctx)


async def _call_before_tool_execution(
    hook: BeforeToolExecutionHook, ctx: AgentContext, **kw: Unpack[_ToolExecutionPayload]
) -> None:
    await hook.before_tool_execution(ctx, kw.get("tool_calls"))  # type: ignore[arg-type]


async def _call_after_tool_execution(
    hook: AfterToolExecutionHook, ctx: AgentContext, **kw: Unpack[_ToolResultsPayload]
) -> None:
    await hook.after_tool_execution(ctx, kw.get("results"))  # type: ignore[arg-type]


async def _call_after_llm_response(
    hook: AfterLLMResponseHook, ctx: AgentContext, **kw: Unpack[_AfterLLMResponsePayload]
) -> None:
    await hook.after_llm_response(ctx, kw.get("response"))  # type: ignore[arg-type]


async def _call_finalize_content(
    hook: FinalizeContentHook, ctx: AgentContext, **kw: Unpack[_FinalizeContentPayload]
) -> str | None:
    return hook.finalize_content(ctx, kw.get("content"))


async def _call_finally_turn(
    hook: FinallyTurnHook, ctx: AgentContext, **kw: Unpack[_FinallyTurnPayload]
) -> None:
    await hook.finally_turn(ctx, kw.get("result"))


async def _call_before_llm(
    hook: BeforeLLMHook, ctx: AgentContext, **kw: Unpack[_BeforeLLMPayload]
) -> None:
    await hook.before_llm(ctx, kw.get("request"))  # type: ignore[arg-type]


async def _call_after_approval(
    hook: AfterApprovalHook, ctx: AgentContext, **kw: Unpack[_AfterApprovalPayload]
) -> None:
    await hook.after_approval(ctx, kw.get("transaction"))  # type: ignore[arg-type]


_HOOK_DISPATCH: dict[HookPoint, tuple[type, Callable[..., Any]]] = {
    HookPoint.BEFORE_TURN: (BeforeTurnHook, _call_before_turn),
    HookPoint.AFTER_TURN: (AfterTurnHook, _call_after_turn),
    HookPoint.BEFORE_ITERATION: (BeforeIterationHook, _call_before_iteration),
    HookPoint.AFTER_ITERATION: (AfterIterationHook, _call_after_iteration),
    HookPoint.BEFORE_TOOL_EXECUTION: (BeforeToolExecutionHook, _call_before_tool_execution),
    HookPoint.AFTER_TOOL_EXECUTION: (AfterToolExecutionHook, _call_after_tool_execution),
    HookPoint.AFTER_LLM_RESPONSE: (AfterLLMResponseHook, _call_after_llm_response),
    HookPoint.FINALIZE_CONTENT: (FinalizeContentHook, _call_finalize_content),
    HookPoint.FINALLY_TURN: (FinallyTurnHook, _call_finally_turn),
    HookPoint.BEFORE_LLM: (BeforeLLMHook, _call_before_llm),
    HookPoint.AFTER_APPROVAL: (AfterApprovalHook, _call_after_approval),
}


class HookRunner:
    """Hook 调度执行器。

    按配置顺序遍历 Hook 列表，使用 isinstance 检查替代 getattr 实现类型安全的分发。
    每个 hook 带独立超时保护，异常处理策略由 HookSpec.on_error 控制。
    """

    def __init__(self, hook_specs: list[HookSpec] | None = None) -> None:
        self._hook_specs: list[HookSpec] = list(hook_specs) if hook_specs else []

    @property
    def hook_specs(self) -> list[HookSpec]:
        """返回当前注册的 hook 规格列表。"""
        return list(self._hook_specs)

    def add(self, spec: HookSpec) -> None:
        """追加一个 hook 规格。"""
        self._hook_specs.append(spec)

    def insert(self, index: int, spec: HookSpec) -> None:
        """在指定位置插入 hook 规格。"""
        self._hook_specs.insert(index, spec)

    def remove(self, spec: HookSpec) -> None:
        """移除一个 hook 规格。"""
        self._hook_specs.remove(spec)

    def extend(self, specs: list[HookSpec]) -> None:
        """批量追加 hook 规格。"""
        self._hook_specs.extend(specs)

    async def dispatch(
        self,
        hook_point: HookPoint,
        ctx: AgentContext,
        payload: HookPayload | None = None,
        *,
        hook_timeout: float | None = None,
    ) -> None:
        """按顺序调度所有注册的 Hook 的指定 hook_point 方法。

        Args:
            hook_point: 调度点
            ctx: Agent 执行上下文
            payload: 可选统一承载数据，data 字段会作为 **kwargs 传递
            hook_timeout: 每个 hook 的超时秒数，None 使用默认值

        Raises:
            AgentControlError: 受控终止异常透传
            CancelledError: asyncio 取消透传
            Exception: HookErrorPolicy.ABORT 时会抛出
        """
        timeout = hook_timeout if hook_timeout is not None else _DEFAULT_HOOK_TIMEOUT
        hook_kwargs = dict(payload.data) if payload else {}

        entry = _HOOK_DISPATCH.get(hook_point)
        if entry is None:
            return

        dispatch_cls, caller = entry

        for spec in self._hook_specs:
            hook = spec.hook
            if not isinstance(hook, dispatch_cls):
                continue

            try:
                await asyncio.wait_for(
                    caller(hook, ctx, **hook_kwargs),
                    timeout=timeout,
                )
            except asyncio.CancelledError:
                raise
            except AgentControlError:
                # A controlled-exit signal raised by a hook (e.g.
                # LoopDetectedError) is intentional, NOT a hook failure.
                # Propagate it so ReActAgent.run()'s unified
                # ``except AgentControlError`` handler can render the result.
                raise
            except TimeoutError:
                logger.warning(
                    "Hook %s.%s timed out after %.1fs",
                    hook.name,
                    hook_point.value,
                    timeout,
                )
                self._handle_error(spec, hook_point, timeout, is_timeout=True)
            except Exception:
                logger.exception(
                    "Hook %s failed in %s",
                    hook.name,
                    hook_point.value,
                )
                self._handle_error(spec, hook_point, timeout, is_timeout=False)

    def _handle_error(
        self,
        spec: HookSpec,
        hook_point: HookPoint,
        timeout: float,
        *,
        is_timeout: bool,
    ) -> None:
        """根据 HookErrorPolicy 处理 hook 异常。"""
        hook_name = spec.hook.name

        if spec.on_error == HookErrorPolicy.IGNORE:
            return
        elif spec.on_error == HookErrorPolicy.LOG:
            # 已在上层记录日志
            pass
        elif spec.on_error == HookErrorPolicy.ABORT:
            error_type = "timeout" if is_timeout else "error"
            from modex_agent.control.exceptions import PolicyViolation

            raise PolicyViolation(
                f"Hook {hook_name}.{hook_point.value} {error_type} (policy=abort)"
            )

    def dispatch_finalize(
        self,
        ctx: AgentContext,
        content: str | None,
    ) -> str | None:
        """同步串行调用所有 hook 的 finalize_content 方法。

        finalize_content 是同步方法，在此依次链式调用。
        """
        result = content
        for spec in self._hook_specs:
            hook = spec.hook
            if not isinstance(hook, FinalizeContentHook):
                continue
            try:
                result = hook.finalize_content(ctx, result)
            except Exception:
                logger.exception(
                    "Hook %s.finalize_content failed",
                    hook.name,
                )
                if spec.on_error == HookErrorPolicy.ABORT:
                    raise
        return result
