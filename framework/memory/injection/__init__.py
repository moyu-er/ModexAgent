"""Memory injection policies for LLM context assembly.

Provides pluggable strategies for converting MemorySystem state into
ContextState (system_prompt + history).
"""

from abc import ABC, abstractmethod

from framework.core.context import ContextState
from framework.memory.compression.tool_chain import (
    _fit_token_window,
    _is_tool_call,
    _is_tool_result,
)
from framework.memory.core.scope import MemoryContext
from framework.memory.history import ShortTermMessageHistory
from framework.memory.managers.short_term import ShortTermMemoryManager
from framework.memory.managers.working import WorkingMemoryManager
from framework.memory.system import MemorySystem
from framework.memory.utils import estimate_token_count


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
    - system_prompt = base_prompt + long_term(SOUL/USER/MEMORY) + history 摘要
    - history = short_term(已持久化历史) + working_memory(当前 turn 缓存)
    """

    def __init__(
        self,
        max_short_term_messages: int = 50,
        max_history_entries: int = 5,
        max_system_prompt_tokens: int | None = None,
        max_total_tokens: int | None = None,
        filter_tool_messages: bool = True,
    ):
        self.max_short_term_messages = max_short_term_messages
        self.max_history_entries = max_history_entries
        self.max_system_prompt_tokens = max_system_prompt_tokens
        self.max_total_tokens = max_total_tokens
        self.filter_tool_messages = filter_tool_messages

    async def assemble(
        self,
        memory_system: MemorySystem,
        context: MemoryContext,
        base_system_prompt: str = "",
    ) -> ContextState:
        # 职责边界说明：
        # - 本方法（assemble）负责将 MemorySystem 各层数据组合成 ContextState，
        #   其中 system_prompt 仅包含来自记忆层的内容（long_term + history 摘要）。
        # - MemorySystemContextManager.build_system_prompt() 在此基础上追加
        #   工具描述、skills 和运行时信息，两者是组合关系而非重复。

        # 1. 短期记忆 + 工作记忆（按时间顺序：旧的 short_term 在前，新的 working 在后）
        short_term_msgs = await memory_system.get_history(
            context, max_messages=self.max_short_term_messages
        )
        working_msgs = memory_system.get_working_messages(context)
        history = short_term_msgs + working_msgs

        # 1.1 过滤 tool 消息（tool_calls + tool results），减少 token 浪费
        if self.filter_tool_messages:
            history = [
                msg for msg in history
                if not (_is_tool_call(msg) or _is_tool_result(msg))
            ]

        # 2. 中长期记忆系统提示词
        memory_prompt = await memory_system.build_system_prompt(
            context, max_history_entries=self.max_history_entries
        )

        # 2.1 短期记忆压缩摘要（不再以 system 消息存入 history，而是动态注入 system_prompt）
        compression_summary = await memory_system.get_compression_summary(context)
        if compression_summary:
            section = f"[Earlier conversation compressed] {compression_summary}"
            memory_prompt = (
                section + "\n\n---\n\n" + memory_prompt
                if memory_prompt
                else section
            )

        # 3. Token 预算控制
        if self.max_total_tokens is not None:
            base_tokens = estimate_token_count(
                [{"role": "system", "content": base_system_prompt}]
            )
            memory_tokens = estimate_token_count(
                [{"role": "system", "content": memory_prompt}]
            )
            available_for_history = self.max_total_tokens - base_tokens - memory_tokens
            available_for_history = max(available_for_history, 0)

            history_dicts = [m.to_dict() for m in history]
            history_tokens = estimate_token_count(history_dicts)
            if history_tokens > available_for_history and len(history) > 2:
                history, _ = _fit_token_window(history, available_for_history)

        if self.max_system_prompt_tokens is not None:
            # system_prompt = base + memory_prompt
            combined_system = "\n\n---\n\n".join(
                [p for p in [base_system_prompt, memory_prompt] if p]
            )
            while True:
                if not memory_prompt:
                    break
                tokens = estimate_token_count(
                    [{"role": "system", "content": combined_system}]
                )
                if tokens <= self.max_system_prompt_tokens:
                    break
                # 从 memory_prompt 中逐 section 丢弃（优先丢弃历史摘要）
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

        # 使用 ShortTermMessageHistory 让 context.history.append()
        # 直接写入短期记忆存储（实时持久化 ReAct 中间数据）。
        # ShortTermMessageHistory.to_list() 会自动读取 messages.jsonl
        # 并合并 working memory，不需要手动同步历史消息。
        stm = memory_system._managers.short_term
        working = memory_system._managers.working
        if not isinstance(stm, ShortTermMemoryManager):
            raise TypeError(
                f"short_term manager must be ShortTermMemoryManager, got {type(stm).__name__}"
            )
        if not isinstance(working, WorkingMemoryManager):
            raise TypeError(
                f"working manager must be WorkingMemoryManager, got {type(working).__name__}"
            )
        return ContextState(
            system_prompt=system_prompt,
            history=ShortTermMessageHistory(
                manager=stm,
                context=context,
                working_manager=working,
            ),
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
