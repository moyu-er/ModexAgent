"""DispatchDeadline — 可续期的 dispatch 超时机制。

Pool 在 dispatch 开始时创建 DispatchDeadline 并通过 ContextVar 向下传递。
LLM 节点每完成一轮推理后续期，pool 的 watchdog 协程监控 deadline 是否过期。
"""
from __future__ import annotations

import time
from contextvars import ContextVar

__all__ = ["DispatchDeadline", "current_dispatch_deadline"]

# Pool 设置，LLM node 读取并 renew。
current_dispatch_deadline: ContextVar[DispatchDeadline | None] = ContextVar(
    "current_dispatch_deadline", default=None,
)


class DispatchDeadline:
    """可续期的单调时钟 deadline。

    * pool._run_dispatch 创建实例并通过 ContextVar 注入
    * nodes/llm.py 在每轮 LLM 完成后调用 renew()
    * pool watchdog 循环检查 is_expired
    """

    __slots__ = ("_expires_at", "_extension")

    def __init__(self, initial_timeout: float, extension: float) -> None:
        self._expires_at: float = time.monotonic() + initial_timeout
        self._extension: float = extension

    def renew(self) -> None:
        """从当前时刻续期一个 extension 时长，不会缩短已有 deadline。"""
        self._expires_at = max(
            self._expires_at,
            time.monotonic() + self._extension,
        )

    @property
    def is_expired(self) -> bool:
        return time.monotonic() >= self._expires_at

    @property
    def remaining(self) -> float:
        return max(0.0, self._expires_at - time.monotonic())
