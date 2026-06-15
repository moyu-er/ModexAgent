"""Agent 抽象基类和 AgentContext

提供 Agent[E] 泛型抽象基类和 AgentContext 执行上下文。
"""

from __future__ import annotations

import contextvars
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic

from typing_extensions import TypeVar

from framework.core.session_id import SessionInfo
from framework.memory.history import MessageHistory

from .emitter import AgentResult, ContentEmitter
from .events import AgentEvent
from .message_utils import normalize_agent_messages_for_llm
from .tool_manager import ToolManager

if TYPE_CHECKING:
    from framework.memory.pipeline.pipeline import SystemPromptPipeline
    from framework.multi_agent.comm_kind import AgentCommKind
    from framework.runtime.models import TurnIdentity
    from framework.runtime.services import AgentRuntime


@dataclass
class AgentContext:
    """Agent execution context — typed runtime state via ``runtime`` field."""

    system_prompt: str
    history: MessageHistory
    """Message history for the current session.

    **IMPORTANT**: Do NOT use ``len(history)``, ``list(history)``, or index
    access ``history[0]`` — these are **not** guaranteed to work.  The pool-mode
    implementation (async-backed history) intentionally raises on
    synchronous ``__len__`` / ``__iter__`` / ``__getitem__`` because messages
    live in an async storage backend.

    Always use ``await history.to_list()`` to get a list of messages, then use
    ``len()`` / iteration on *that* list::

        messages = await ctx.history.to_list()
        count = len(messages)
        for msg in messages:
            ...
    """
    tool_manager: ToolManager
    session: SessionInfo
    comm_kind: AgentCommKind | None = None
    max_iterations: int = 10
    temperature: float | None = None
    max_tokens: int | None = None
    attachments: list[str] = field(default_factory=list)
    emitter: ContentEmitter | None = None
    runtime: AgentRuntime | None = None
    identity: TurnIdentity | None = None
    system_prompt_pipeline: SystemPromptPipeline | None = None

    @property
    def current_turn_uuid(self) -> str | None:
        """Current turn UUID from runtime state, for control command validation."""
        if self.runtime is None:
            return None
        return self.runtime.turn_uuid

    async def to_messages(self) -> list[dict[str, Any]]:
        history_list = await self.history.to_list()
        history_list, _has_agent_msgs = normalize_agent_messages_for_llm(history_list)
        non_system = [msg for msg in history_list if msg.get("role") != "system"]

        def _strip_none(d: dict[str, Any]) -> dict[str, Any]:
            return {k: v for k, v in d.items() if v is not None}

        return [_strip_none(msg) for msg in non_system]

    def get_tool_descriptions(self) -> list[dict[str, Any]]:
        return self.tool_manager.get_tool_descriptions()

    def add_attachment(self, path: str) -> None:
        self.attachments.append(path)


current_agent_context: contextvars.ContextVar[AgentContext] = contextvars.ContextVar(
    "current_agent_context"
)

E = TypeVar("E", bound=AgentEvent)


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
        return cls.event_enum
