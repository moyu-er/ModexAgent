"""HookRunner —— Hook 调度执行器。

负责按 HookPoint 调度所有注册的 Hook，处理错误策略和受控异常传播。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from framework.hook.abc import (
    HookErrorPolicy,
    HookPayload,
    HookPoint,
    HookResult,
    HookSpec,
)

if TYPE_CHECKING:
    from framework.core.agent import AgentContext

R = TypeVar("R", default=Any)

logger = logging.getLogger(__name__)

# 默认超时：每个 hook 方法调用的最大时间（秒）
_DEFAULT_HOOK_TIMEOUT = 10.0


class HookRunner(Generic[R]):
    """Hook 调度执行器。

    按配置顺序遍历 Hook 列表，对每个 Hook 调用匹配 hook_point 的方法。
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
        ctx: "AgentContext[R]",
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

        for spec in self._hook_specs:
            hook = spec.hook
            method = getattr(hook, hook_point.value, None)
            if method is None:
                continue

            try:
                result = await asyncio.wait_for(
                    method(ctx, **hook_kwargs),
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
        ctx: "AgentContext[R]",
        content: str | None,
    ) -> str | None:
        """同步串行调用所有 hook 的 finalize_content 方法。

        finalize_content 是同步方法，在此依次链式调用。
        """
        result = content
        for spec in self._hook_specs:
            hook = spec.hook
            method = getattr(hook, "finalize_content", None)
            if method is None:
                continue
            try:
                result = method(ctx, result)
            except Exception:
                logger.exception(
                    "Hook %s.finalize_content failed",
                    type(hook).__name__,
                )
                if spec.on_error == HookErrorPolicy.ABORT:
                    raise
        return result
