from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from framework.core.types import InputMessage


class CommandInterceptor(ABC):
    """命令拦截器：处理用户输入中的系统命令。"""

    @abstractmethod
    def handle(self, message: InputMessage) -> str | None:
        """若匹配到命令则返回响应文本，否则返回 None 让消息继续处理。"""
        ...

    async def handle_async(self, message: InputMessage) -> str | None:
        """异步版本命令拦截。默认委托给同步 handle()。"""
        return self.handle(message)


class SystemCommandInterceptor(CommandInterceptor):
    """默认系统命令拦截器。"""

    def __init__(
        self,
        pool: Any | None = None,
    ):
        self._pool = pool

    def handle(self, message: InputMessage) -> str | None:
        content = message.content.strip()

        if content == "/stop":
            return "Stopping current session tasks..."

        if content == "/clear":
            # 清空历史由上层 Pipeline/Session 处理
            return "History cleared."

        if content == "/status":
            # 返回运行中 Agent 状态
            if self._pool is not None:
                agents = getattr(self._pool, "list_agents", lambda: [])()
                return f"Active agents: {len(agents)}"
            return "No agent pool configured."

        return None

    async def handle_async(self, message: InputMessage) -> str | None:
        """异步命令拦截，实际执行 /stop 的取消操作。"""
        content = message.content.strip()

        if content == "/stop":
            # 同步 subagent 在 turn 内完成，无需显式 cancel_by_session
            return "Stopping current session tasks..."

        return self.handle(message)
