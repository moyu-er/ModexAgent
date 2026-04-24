"""Memory injection policies for LLM context assembly.

Provides pluggable strategies for converting MemorySystem state into
ContextState (system_prompt + history).
"""

import logging
from abc import ABC, abstractmethod

from framework.core.context import ContextState
from framework.core.types import MessageRole
from framework.memory.compression.tool_chain import _fit_token_window
from framework.memory.core.message import ChatMessage
from framework.memory.core.scope import MemoryAgentRole, MemoryContext
from framework.memory.injection.filter import (
    InjectionFilterStrategy,
    NoopFilterStrategy,
    ToolMessageFilterStrategy,
)
from framework.memory.system import MemorySystem
from framework.memory.utils import estimate_token_count

logger = logging.getLogger(__name__)


class MemoryInjectionPolicy(ABC):
    """负责将 MemorySystem 各层内容映射为 LLM 可用的 ContextState。"""

    @abstractmethod
    async def assemble(
        self,
        memory_system: MemorySystem,
        context: MemoryContext,
        base_system_prompt: str = "",
    ) -> ContextState:
        """将 MemorySystem 的各层数据组装成 ContextState。

        Args:
            memory_system: 记忆系统实例
            context: 记忆上下文
            base_system_prompt: 基础系统提示词

        Returns:
            ContextState: 包含 system_prompt 和 history 的完整上下文状态
        """
        pass


class DefaultMemoryInjectionPolicy(MemoryInjectionPolicy):
    """默认策略：
    - system_prompt = base_prompt + long_term(SOUL/USER/MEMORY) + history 摘要 + provider prefetch
    - history = short_term(已持久化历史，经 InjectionFilterStrategy 过滤)
    - peer/subagent 仅获得 short-term，不注入中长期记忆或 provider 内容
    """

    def __init__(
        self,
        max_short_term_messages: int = 50,
        max_history_entries: int = 5,
        max_system_prompt_tokens: int | None = None,
        max_total_tokens: int | None = None,
        filter_strategy: InjectionFilterStrategy | None = None,
    ):
        self.max_short_term_messages = max_short_term_messages
        self.max_history_entries = max_history_entries
        self.max_system_prompt_tokens = max_system_prompt_tokens
        self.max_total_tokens = max_total_tokens
        self.filter_strategy = filter_strategy or ToolMessageFilterStrategy()

    @staticmethod
    def _is_peer_or_subagent(context: MemoryContext) -> bool:
        candidates = [context.agent_id, context.sender_agent, context.receiver_agent]
        for value in candidates:
            if not value:
                continue
            v = str(value).lower()
            if v == MemoryAgentRole.PEER.value or v.startswith(MemoryAgentRole.PEER.value):
                return True
            if v == MemoryAgentRole.SUBAGENT.value or v.startswith(MemoryAgentRole.SUBAGENT.value):
                return True
        return False

    async def assemble(
        self,
        memory_system: MemorySystem,
        context: MemoryContext,
        base_system_prompt: str = "",
    ) -> ContextState:
        # 1. 短期记忆（所有角色共享）
        short_term_msgs = await memory_system.get_history(
            context, max_messages=self.max_short_term_messages
        )
        history = self.filter_strategy.filter(list(short_term_msgs))

        # peer/subagent: 只返回过滤后的短期记忆，不注入中长期/provider
        if self._is_peer_or_subagent(context):
            history_obj = memory_system.create_message_history(
                context=context, initial_messages=history
            )
            return ContextState(
                system_prompt=base_system_prompt,
                history=history_obj,
            )

        # 1.1 从历史中提取最近一条 user message 作为 query
        query = ""
        for msg in reversed(short_term_msgs):
            role = msg.role if isinstance(msg, ChatMessage) else msg.get("role", "")
            if role == MessageRole.USER:
                content = msg.content if isinstance(msg, ChatMessage) else msg.get("content", "")
                query = content if isinstance(content, str) else str(content)
                break

        # 2. 中长期记忆系统提示词
        memory_prompt = await memory_system.build_system_prompt(
            context,
            max_history_entries=self.max_history_entries,
            query=query,
        )

        # 2.0 provider 静态 system prompt blocks
        provider_blocks: list[str] = []
        for provider in memory_system.get_providers():
            try:
                block = provider.system_prompt_block()
                if block:
                    provider_blocks.append(block)
            except Exception as e:
                logger.warning("Provider '%s' system_prompt_block failed: %s", provider.name, e)
        if provider_blocks:
            blocks_text = "\n\n".join(provider_blocks)
            memory_prompt = (
                memory_prompt + "\n\n---\n\n" + blocks_text
                if memory_prompt
                else blocks_text
            )

        # 2.1 provider 动态 prefetch（统一注入入口）
        if query:
            prefetch_result = await memory_system.prefetch_memories(query, context)
            if prefetch_result:
                section = f"<memory-context>\n{prefetch_result}\n</memory-context>"
                memory_prompt = (
                    memory_prompt + "\n\n---\n\n" + section
                    if memory_prompt
                    else section
                )

        # 2.2 短期记忆压缩摘要
        compression_summary = await memory_system.get_compression_summary(context)
        if compression_summary:
            section = f"[Earlier conversation compressed] {compression_summary}"
            memory_prompt = (
                section + "\n\n---\n\n" + memory_prompt
                if memory_prompt
                else section
            )

        # 2.3 AutoCompact 空闲压缩摘要
        auto_compact_summary = await memory_system.get_auto_compact_summary(context)
        if auto_compact_summary:
            section = f"[Auto-compact summary] {auto_compact_summary}"
            memory_prompt = (
                section + "\n\n---\n\n" + memory_prompt
                if memory_prompt
                else section
            )

        # 3. Token 预算控制
        if self.max_total_tokens is not None:
            base_tokens = estimate_token_count(
                [ChatMessage(role=MessageRole.SYSTEM, content=base_system_prompt)]
            )
            memory_tokens = estimate_token_count(
                [ChatMessage(role=MessageRole.SYSTEM, content=memory_prompt)]
            )
            available_for_history = self.max_total_tokens - base_tokens - memory_tokens
            available_for_history = max(available_for_history, 0)

            history_dicts = [m.to_dict() for m in history]
            history_tokens = estimate_token_count(history_dicts)
            if history_tokens > available_for_history and len(history) > 2:
                history, _ = _fit_token_window(history, available_for_history)

        if self.max_system_prompt_tokens is not None:
            combined_system = "\n\n---\n\n".join(
                [p for p in [base_system_prompt, memory_prompt] if p]
            )
            while True:
                if not memory_prompt:
                    break
                tokens = estimate_token_count(
                    [ChatMessage(role=MessageRole.SYSTEM, content=combined_system)]
                )
                if tokens <= self.max_system_prompt_tokens:
                    break
                trimmed = self._trim_memory_prompt_section(memory_prompt)
                if trimmed == memory_prompt:
                    break
                memory_prompt = trimmed
                combined_system = "\n\n---\n\n".join(
                    [p for p in [base_system_prompt, memory_prompt] if p]
                )

        parts = []
        if base_system_prompt:
            parts.append(base_system_prompt)
        if memory_prompt:
            parts.append(memory_prompt)
        system_prompt = "\n\n---\n\n".join(parts) if parts else ""

        history_obj = memory_system.create_message_history(
            context=context,
            initial_messages=history,
        )
        return ContextState(
            system_prompt=system_prompt,
            history=history_obj,
        )

    @staticmethod
    def _trim_memory_prompt_section(memory_prompt: str) -> str:
        """移除 memory_prompt 中最后一个 Markdown section（优先移除历史摘要）。"""
        if not memory_prompt:
            return memory_prompt
        sections = memory_prompt.split("\n\n---\n\n")
        if len(sections) <= 1:
            return memory_prompt
        # 优先移除 "近期对话摘要" section
        for i in range(len(sections) - 1, -1, -1):
            if "近期对话摘要" in sections[i]:
                sections.pop(i)
                return "\n\n---\n\n".join(sections)
        # 否则移除最后一个 section
        sections.pop()
        return "\n\n---\n\n".join(sections)


__all__ = [
    "DefaultMemoryInjectionPolicy",
    "MemoryInjectionPolicy",
    "InjectionFilterStrategy",
    "NoopFilterStrategy",
    "ToolMessageFilterStrategy",
]
