"""ContextManager 抽象基类和实现

提供上下文管理功能，支持多种存储后端。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from framework.core.skills import ResolutionContext, SkillManager
from framework.memory.core.message import ChatMessage
from framework.memory.history import ListMessageHistory, MessageHistory

if TYPE_CHECKING:
    from framework.memory.context_governance import ContextGovernance
    from framework.memory.pipeline.pipeline import SystemPromptPipeline

from .emitter import AgentResult
from .message_utils import AGENT_COMMUNICATION_SYSTEM_NOTE, normalize_agent_messages_for_llm

logger = logging.getLogger(__name__)


@dataclass
class ContextState:
    """上下文状态"""

    system_prompt: str = ""
    history: MessageHistory = field(default_factory=ListMessageHistory)
    metadata: dict[str, Any] = field(default_factory=dict)
    system_prompt_pipeline: SystemPromptPipeline | None = None

    def __post_init__(self) -> None:
        """构造时自动将 list 转换为 ListMessageHistory，确保类型一致性。"""
        if isinstance(self.history, list):
            self.history = ListMessageHistory(list(self.history))

    async def to_messages(self) -> list[dict[str, Any]]:
        """转换为 LLM 消息列表

        内部存储的 role: "agent" 消息会在此处转换为 role: "user" 并添加来源前缀。
        当存在 agent 消息时，自动追加 Agent 通信说明到 system prompt。
        """
        history_list = await self.history.to_list()

        # 将内部 agent 角色转换为 LLM 兼容格式
        history_list, has_agent_msgs = normalize_agent_messages_for_llm(history_list)

        messages = []
        # Prefer pipeline over static system_prompt
        system_content = ""
        if self.system_prompt_pipeline is not None:
            system_content = await self.system_prompt_pipeline.get_or_refresh()
        elif self.system_prompt:
            system_content = self.system_prompt

        if system_content:
            if has_agent_msgs and AGENT_COMMUNICATION_SYSTEM_NOTE not in system_content:
                system_content += AGENT_COMMUNICATION_SYSTEM_NOTE
            messages.append({"role": "system", "content": system_content})
        messages.extend(history_list)
        return messages


class ContextManager(ABC):
    """上下文管理器抽象基类

    职责：
    1. 管理对话历史和系统提示词
    2. 构建 AgentContext 供 Agent 使用
    3. 保存对话结果到历史

    与 nanobot 的区别：
    - 不绑定特定存储（内存、文件、数据库都可以）
    - 支持多种历史管理策略（滑动窗口、token 限制、智能压缩）
    - 可插拔的上下文构建策略
    """

    @abstractmethod
    async def load(
        self,
        session_id: str,
        runtime_info: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        tool_manager: Any = None,
        skill_manager: Any = None,
    ) -> ContextState:
        """加载指定会话的上下文"""
        pass

    @abstractmethod
    async def save(
        self,
        session_id: str,
        user_message: ChatMessage | dict[str, Any] | None,
        assistant_result: AgentResult,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """保存对话回合到历史

        Args:
            session_id: 会话 ID
            user_message: 用户消息（None 表示已在上下文中添加）
            assistant_result: Agent 执行结果
            metadata: 额外元数据
        """
        pass

    @abstractmethod
    async def build_system_prompt(
        self,
        tool_manager: Any,
        skill_manager: SkillManager | None = None,
        runtime_info: dict[str, Any] | None = None,
    ) -> str:
        """构建系统提示词

        子类可以覆盖此方法实现自定义的系统提示词构建逻辑。
        """
        pass

    @abstractmethod
    async def clear(self, session_id: str) -> None:
        """清空指定会话的历史"""
        pass

    # --- Optional extension points (no-op defaults) ---

    async def load_with_metadata(
        self, session_id: str, metadata: dict[str, Any] | None = None
    ) -> ContextState:
        """Load with optional metadata. Default delegates to load()."""
        return await self.load(session_id)

    async def flush(self, session_id: str) -> None:
        """Flush working memory to short-term. No-op by default."""
        pass

    def wrap_governance(
        self,
        governance: ContextGovernance | None,
        session_id: str,
    ) -> ContextGovernance | None:
        return governance


class InMemoryContextManager(ContextManager):
    """内存中的上下文管理器"""

    def __init__(self, base_system_prompt: str = ""):
        self.base_system_prompt = base_system_prompt
        self._sessions: dict[str, ContextState] = {}

    async def load(self, session_id: str, runtime_info=None, metadata=None, tool_manager=None, skill_manager=None) -> ContextState:
        if session_id not in self._sessions:
            self._sessions[session_id] = ContextState(
                system_prompt=self.base_system_prompt,
                history=ListMessageHistory(),
                metadata={},
            )
        return self._sessions[session_id]

    async def save(
        self,
        session_id: str,
        user_message: ChatMessage | dict[str, Any] | None,
        assistant_result: AgentResult,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        state = await self.load(session_id)
        if user_message:
            await state.history.append(user_message)
        # Agent implementations are responsible for appending their own
        # messages to context.history via await ctx.history.append().
        # ContextManager.save() no longer re-appends assistant_result.messages.
        if metadata:
            if "meta_source" not in metadata:
                metadata = {**metadata, "meta_source": "framework"}
            state.metadata.update(metadata)

    async def build_system_prompt(
        self,
        tool_manager: Any,
        skill_manager: SkillManager | None = None,
        runtime_info: dict[str, Any] | None = None,
    ) -> str:
        """Base-class fallback.  MemorySystemContextManager overrides this
        and assembles skills + experiences + memory layers in load() instead.
        This version exists for ContextManager subclasses that do NOT use a
        full MemorySystem.
        """
        parts = [self.base_system_prompt]

        # Skills — persistent reference (included in base class when no
        # MemorySystem pipeline is available to inject them via load()).
        if skill_manager is not None:
            skill_prompt = await skill_manager.build_prompt(
                ResolutionContext.from_runtime(tool_manager=tool_manager)
            )
            if skill_prompt:
                parts.append(skill_prompt)

        parts.append(AGENT_COMMUNICATION_SYSTEM_NOTE)

        # 添加运行时信息
        if runtime_info:
            runtime_text = self._format_runtime_info(runtime_info)
            if runtime_text:
                parts.append(runtime_text)

        return "\n\n---\n\n".join(parts)

    async def clear(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def _format_runtime_info(self, info: dict[str, Any]) -> str:
        lines = ["## Runtime"]
        if "current_time" in info:
            lines.append(f"Current Time: {info['current_time']}")
        if "platform" in info:
            lines.append(f"Platform: {info['platform']}")
        return "\n".join(lines)


