"""Agent 抽象基类和 AgentContext

提供 Agent[E] 泛型抽象基类和 AgentContext 执行上下文。
"""

import contextvars
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from framework.memory.core.message import ChatMessage
from framework.memory.history import MessageHistory

from .emitter import AgentResult, ContentEmitter
from .events import AgentEvent
from .hooks import AgentRunHook
from .message_utils import AGENT_COMMUNICATION_SYSTEM_NOTE, normalize_agent_messages_for_llm
from .runtime_context import RuntimeContext, RuntimeContextManager
from .tool_manager import ToolManager


@dataclass
class AgentContext:
    """Agent 执行上下文

    包含执行所需的所有信息。
    """

    system_prompt: str
    history: MessageHistory
    tool_manager: ToolManager
    session_id: str = ""
    max_iterations: int = 10
    max_tools_per_turn: int = 10
    temperature: float | None = None
    max_tokens: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    on_checkpoint: Callable[[list[ChatMessage | dict[str, Any]]], Awaitable[None]] | None = None
    hooks: list[AgentRunHook] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)  # Agent->User 方向的附件路径列表
    runtime_context_manager: RuntimeContextManager | None = None
    runtime_context: RuntimeContext | None = None

    def add_attachment(self, path: str) -> None:
        """将文件路径添加到 attachments 列表（供 Tool 调用期间使用）。"""
        self.attachments.append(path)

    async def to_messages(self) -> list[dict[str, Any]]:
        """转换为 LLM 消息列表

        内部存储的 role: "agent" 消息会在此处转换为 role: "user" 并添加来源前缀，
        同时在系统提示词中注入 Agent 通信说明（仅当存在 agent 消息时）。
        """
        # 过滤掉 history 中已有的 system 消息，避免重复系统提示词
        history_list = await self.history.to_list()

        # 将内部 agent 角色转换为 LLM 兼容格式
        history_list, has_agent_msgs = normalize_agent_messages_for_llm(history_list)
        non_system = [msg for msg in history_list if msg.get("role") != "system"]

        messages: list[dict[str, Any]] = []
        # 只在 system_prompt 非空时添加，避免 API 报错
        if self.system_prompt:
            system_content = self.system_prompt
            # 存在 agent 消息时，追加通信说明到系统提示词
            if has_agent_msgs and AGENT_COMMUNICATION_SYSTEM_NOTE not in system_content:
                system_content += AGENT_COMMUNICATION_SYSTEM_NOTE
            messages.append({"role": "system", "content": system_content})

        # Strip None values to keep output clean and compatible with tests
        def _strip_none(d: dict[str, Any]) -> dict[str, Any]:
            return {k: v for k, v in d.items() if v is not None}

        messages.extend(_strip_none(msg) for msg in non_system)
        return messages

    def get_tool_descriptions(self) -> list[dict[str, Any]]:
        """获取工具描述（供 LLM 使用）"""
        return self.tool_manager.get_tool_descriptions()


current_agent_context: contextvars.ContextVar["AgentContext"] = contextvars.ContextVar(
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
