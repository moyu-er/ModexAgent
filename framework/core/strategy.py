"""Agent 执行策略。

这些策略决定 Agent 如何执行单次 turn（ReAct 循环或单轮 LLM 调用）。
属于通用 Agent 执行基础设施，不依赖 multi_agent 包。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, TypeVar

from .constants import StopReason
from .events import AgentEvent

if TYPE_CHECKING:
    from .agent import Agent, AgentContext
    from .emitter import AgentResult, ContentEmitter

E = TypeVar("E", bound=AgentEvent)


class ExecutionStrategy(ABC):
    """执行策略抽象基类。"""

    @abstractmethod
    async def execute(
        self,
        agent: Agent[E],
        context: AgentContext,
        emitter: ContentEmitter[E],
    ) -> AgentResult:
        """执行策略。"""
        ...


class ReActStrategy(ExecutionStrategy):
    """ReAct 执行策略。"""

    async def execute(
        self,
        agent: Agent[E],
        context: AgentContext,
        emitter: ContentEmitter[E],
    ) -> AgentResult:
        return await agent.run(context, emitter)


class SingleTurnStrategy(ExecutionStrategy):
    """单轮执行策略（直接 LLM 调用）。"""

    async def execute(
        self,
        agent: Agent[E],
        context: AgentContext,
        emitter: ContentEmitter[E],
    ) -> AgentResult:
        from .provider import LLMProvider

        provider = getattr(agent, "provider", None)
        if not isinstance(provider, LLMProvider):
            raise RuntimeError("SingleTurnStrategy requires an agent with an LLM provider")
        response = await provider.chat(
            messages=await context.to_messages(),
            temperature=context.temperature or 0.7,
            max_tokens=context.max_tokens,
        )
        result = AgentResult(content=response.content or "", stop_reason=StopReason.COMPLETED)
        await context.history.append({"role": "assistant", "content": result.content})
        await emitter.emit_complete(result)
        return result
