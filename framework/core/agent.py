"""Agent 抽象基类和 AgentContext

提供 Agent[E] 泛型抽象基类和 AgentContext 执行上下文。
"""

from __future__ import annotations

import contextvars
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from framework.memory.history import MessageHistory

from .emitter import AgentResult, ContentEmitter
from .events import AgentEvent
from .message_utils import normalize_agent_messages_for_llm
from .tool_manager import ToolManager

R = TypeVar("R", default=Any)


@dataclass
class AgentContext(Generic[R]):
    """Agent execution context — core fields only. Extensions for agent-type-specific services."""

    system_prompt: str
    history: MessageHistory
    tool_manager: ToolManager
    session_id: str = ""
    max_iterations: int = 10
    temperature: float | None = None
    max_tokens: int | None = None
    attachments: list[str] = field(default_factory=list)
    extensions: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    emitter: ContentEmitter | None = None
    runtime: R | None = None

    def add_attachment(self, path: str) -> None:
        self.attachments.append(path)

    async def to_messages(self) -> list[dict[str, Any]]:
        history_list = await self.history.to_list()
        history_list, _has_agent_msgs = normalize_agent_messages_for_llm(history_list)
        non_system = [msg for msg in history_list if msg.get("role") != "system"]

        def _strip_none(d: dict[str, Any]) -> dict[str, Any]:
            return {k: v for k, v in d.items() if v is not None}

        return [_strip_none(msg) for msg in non_system]

    def get_tool_descriptions(self) -> list[dict[str, Any]]:
        return self.tool_manager.get_tool_descriptions()


def ctx_ext(ctx: AgentContext[Any], key: str, default: Any = None) -> Any:
    """Safe accessor for AgentContext.extensions."""
    return ctx.extensions.get(key, default)


current_agent_context: contextvars.ContextVar[AgentContext[Any]] = contextvars.ContextVar(
    "current_agent_context"
)

E = TypeVar('E', bound=AgentEvent)


class Agent(ABC, Generic[E]):
    """Agent 推理模式抽象基类

    职责：执行特定的推理模式（ReAct、Plan 等）。
    不处理：消息路由、历史管理、输出发送。

    通过 ContentEmitter 输出内容，不关心外部如何处理。

    每个子类应定义 event_enum，说明该 Agent 会触发哪些事件类型。

    泛型参数 E 是 Agent 特定的事件枚举类型。
    """

    # 子类必须定义使用的事件类型枚举
    # 例如：event_enum = ReActEvent
    event_enum: type[E] = None  # type: ignore

    @abstractmethod
    async def run(
        self,
        context: AgentContext,
        emitter: ContentEmitter[E],
    ) -> AgentResult:
        """执行 Agent

        Args:
            context: 执行上下文
            emitter: 内容发送器（类型参数与该 Agent 的事件枚举匹配）

        Returns:
            AgentResult: 执行结果
        """
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """Agent 名称"""
        pass

    @classmethod
    def get_event_enum(cls) -> type[E]:
        """获取该 Agent 使用的事件类型枚举

        外部可以根据这个信息配置 Emitter。

        Returns:
            该 Agent 的事件枚举类（如 ReActEvent）
        """
        return cls.event_enum  # type: ignore
