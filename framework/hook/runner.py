"""HookRunner —— Hook 调度执行器。

按 HookPoint 调度所有注册的 Hook，使用 isinstance 检查替代 getattr 实现类型安全的分发。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Generic, TypedDict

from typing_extensions import TypeVar, Unpack

from framework.hook.abc import (
    AfterIterationHook,
    AfterLLMResponseHook,
    AfterToolExecutionHook,
    AfterTurnHook,
    BeforeIterationHook,
    BeforeToolExecutionHook,
    BeforeTurnHook,
    FinalizeContentHook,
    FinallyTurnHook,
    HookErrorPolicy,
    HookPayload,
    HookPoint,
    HookResult,
    HookSpec,
    OnControlCommandHook,
)

if TYPE_CHECKING:
    from framework.core.agent import AgentContext
    from framework.core.emitter import AgentResult
    from framework.core.tool_manager import ToolResult
    from framework.core.types import LLMResponse, ToolCall

R = TypeVar("R", default=Any)

logger = logging.getLogger(__name__)

# 默认超时：每个 hook 方法调用的最大时间（秒）
_DEFAULT_HOOK_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# Per-point typed payloads (TypedDict for static checking, no runtime cost)
# ---------------------------------------------------------------------------


class _EmptyPayload(TypedDict, total=False):
    """No extra data."""


class _AfterTurnPayload(TypedDict, total=False):
    result: "AgentResult | None"


class _ToolExecutionPayload(TypedDict, total=False):
    tool_calls: "Sequence[ToolCall] | None"


class _ToolResultsPayload(TypedDict, total=False):
    results: "Sequence[ToolResult] | None"


class _AfterLLMResponsePayload(TypedDict, total=False):
    response: "LLMResponse | None"


class _ControlCommandPayload(TypedDict, total=False):
    command: "Mapping[str, object]"


class _FinalizeContentPayload(TypedDict, total=False):
    content: "str | None"


class _FinallyTurnPayload(TypedDict, total=False):
    result: "AgentResult | None"


# ---------------------------------------------------------------------------
# Per-point dispatch helpers — eliminate getattr from the hot path
# ---------------------------------------------------------------------------


async def _call_before_turn(
    hook: BeforeTurnHook, ctx: AgentContext[R], **_: Unpack[_EmptyPayload]
) -> None:
    await hook.before_turn(ctx)


async def _call_after_turn(
    hook: AfterTurnHook, ctx: AgentContext[R], **kw: Unpack[_AfterTurnPayload]
) -> None:
    await hook.after_turn(ctx, kw.get("result"))  # type: ignore[arg-type]


async def _call_before_iteration(
    hook: BeforeIterationHook, ctx: AgentContext[R], **_: Unpack[_EmptyPayload]
) -> None:
    await hook.before_iteration(ctx)


async def _call_after_iteration(
    hook: AfterIterationHook, ctx: AgentContext[R], **_: Unpack[_EmptyPayload]
) -> None:
    await hook.after_iteration(ctx)


async def _call_before_tool_execution(
    hook: BeforeToolExecutionHook, ctx: AgentContext[R], **kw: Unpack[_ToolExecutionPayload]
) -> None:
    await hook.before_tool_execution(ctx, kw.get("tool_calls"))  # type: ignore[arg-type]


async def _call_after_tool_execution(
    hook: AfterToolExecutionHook, ctx: AgentContext[R], **kw: Unpack[_ToolResultsPayload]
) -> None:
    await hook.after_tool_execution(ctx, kw.get("results"))  # type: ignore[arg-type]


async def _call_after_llm_response(
    hook: AfterLLMResponseHook, ctx: AgentContext[R], **kw: Unpack[_AfterLLMResponsePayload]
) -> HookResult | None:
    return await hook.after_llm_response(ctx, kw.get("response"))  # type: ignore[arg-type]


async def _call_on_control_command(
    hook: OnControlCommandHook, ctx: AgentContext[R], **kw: Unpack[_ControlCommandPayload]
) -> HookResult:
    return await hook.on_control_command(ctx, kw.get("command", {}))  # type: ignore[arg-type]


async def _call_finalize_content(
    hook: FinalizeContentHook, ctx: AgentContext[R], **kw: Unpack[_FinalizeContentPayload]
) -> str | None:
    return hook.finalize_content(ctx, kw.get("content"))


async def _call_finally_turn(
    hook: FinallyTurnHook, ctx: AgentContext[R], **kw: Unpack[_FinallyTurnPayload]
) -> None:
    await hook.finally_turn(ctx, kw.get("result"))


_HOOK_DISPATCH: dict[HookPoint, tuple[type, Callable[..., Any]]] = {
    HookPoint.BEFORE_TURN: (BeforeTurnHook, _call_before_turn),
    HookPoint.AFTER_TURN: (AfterTurnHook, _call_after_turn),
    HookPoint.BEFORE_ITERATION: (BeforeIterationHook, _call_before_iteration),
    HookPoint.AFTER_ITERATION: (AfterIterationHook, _call_after_iteration),
    HookPoint.BEFORE_TOOL_EXECUTION: (BeforeToolExecutionHook, _call_before_tool_execution),
    HookPoint.AFTER_TOOL_EXECUTION: (AfterToolExecutionHook, _call_after_tool_execution),
    HookPoint.AFTER_LLM_RESPONSE: (AfterLLMResponseHook, _call_after_llm_response),
    HookPoint.ON_CONTROL_COMMAND: (OnControlCommandHook, _call_on_control_command),
    HookPoint.FINALIZE_CONTENT: (FinalizeContentHook, _call_finalize_content),
    HookPoint.FINALLY_TURN: (FinallyTurnHook, _call_finally_turn),
}


class HookRunner(Generic[R]):
    """Hook 调度执行器。

    按配置顺序遍历 Hook 列表，使用 isinstance 检查替代 getattr 实现类型安全的分发。
    每个 hook 带独立超时保护，异常处理策略由 HookSpec.on_error 控制。
    """

    def __init__(self, hook_specs: list[HookSpec[R]] | None = None) -> None:
        self._hook_specs: list[HookSpec[R]] = list(hook_specs) if hook_specs else []

    @property
    def hook_specs(self) -> list[HookSpec[R]]:
        """返回当前注册的 hook 规格列表。"""
        return list(self._hook_specs)

    def add(self, spec: HookSpec[R]) -> None:
        """追加一个 hook 规格。"""
        self._hook_specs.append(spec)

    def insert(self, index: int, spec: HookSpec[R]) -> None:
        """在指定位置插入 hook 规格。"""
        self._hook_specs.insert(index, spec)

    def remove(self, spec: HookSpec[R]) -> None:
        """移除一个 hook 规格。"""
        self._hook_specs.remove(spec)

    def extend(self, specs: list[HookSpec[R]]) -> None:
        """批量追加 hook 规格。"""
        self._hook_specs.extend(specs)

    async def dispatch(
        self,
        hook_point: HookPoint,
        ctx: AgentContext[R],
        payload: HookPayload | None = None,
        *,
        hook_timeout: float | None = None,
    ) -> HookResult:
        """按顺序调度所有注册的 Hook 的指定 hook_point 方法。

        Args:
            hook_point: 调度点
            ctx: Agent 执行上下文
            payload: 可选统一承载数据，data 字段会作为 **kwargs 传递
            hook_timeout: 每个 hook 的超时秒数，None 使用默认值

        Returns:
            聚合后的 HookResult。
            - 如果任何 hook 返回 veto=True，结果中 veto=True
            - content_override 取最后一非空值

        Raises:
            AgentControlError: 受控终止异常透传
            CancelledError: asyncio 取消透传
            Exception: HookErrorPolicy.ABORT 时会抛出
        """
        timeout = hook_timeout if hook_timeout is not None else _DEFAULT_HOOK_TIMEOUT
        hook_kwargs = dict(payload.data) if payload else {}

        aggregated = HookResult.pass_through()

        entry = _HOOK_DISPATCH.get(hook_point)
        if entry is None:
            return aggregated

        dispatch_cls, caller = entry

        for spec in self._hook_specs:
            hook = spec.hook
            if not isinstance(hook, dispatch_cls):
                continue

            try:
                result = await asyncio.wait_for(
                    caller(hook, ctx, **hook_kwargs),
                    timeout=timeout,
                )
                if isinstance(result, HookResult):
                    if result.veto:
                        aggregated = HookResult(veto=True)
                    if result.content_override is not None:
                        aggregated = HookResult(
                            veto=aggregated.veto,
                            content_override=result.content_override,
                        )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                logger.warning(
                    "Hook %s.%s timed out after %.1fs",
                    type(hook).__name__,
                    hook_point.value,
                    timeout,
                )
                self._handle_error(spec, hook_point, timeout, is_timeout=True)
            except Exception:
                logger.exception(
                    "Hook %s failed in %s",
                    type(hook).__name__,
                    hook_point.value,
                )
                self._handle_error(spec, hook_point, timeout, is_timeout=False)

        return aggregated

    def _handle_error(
        self,
        spec: HookSpec,
        hook_point: HookPoint,
        timeout: float,
        *,
        is_timeout: bool,
    ) -> None:
        """根据 HookErrorPolicy 处理 hook 异常。"""
        hook_name = type(spec.hook).__name__

        if spec.on_error == HookErrorPolicy.IGNORE:
            return
        elif spec.on_error == HookErrorPolicy.LOG:
            # 已在上层记录日志
            pass
        elif spec.on_error == HookErrorPolicy.ABORT:
            error_type = "timeout" if is_timeout else "error"
            from framework.control.exceptions import PolicyViolation

            raise PolicyViolation(
                f"Hook {hook_name}.{hook_point.value} {error_type} (policy=abort)"
            )

    def dispatch_finalize(
        self,
        ctx: AgentContext[R],
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
                    type(hook).__name__,
                )
                if spec.on_error == HookErrorPolicy.ABORT:
                    raise
        return result
