"""可中断 Agent Runner。

包装 Agent.run() 以支持 graceful cancellation。
属于通用 Agent 执行基础设施，不依赖 multi_agent 包。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, TypeVar

from .emitter import AgentResult
from .events import AgentEvent

if TYPE_CHECKING:
    from .agent import Agent, AgentContext
    from .emitter import ContentEmitter

E = TypeVar("E", bound=AgentEvent)


class InterruptibleRunner:
    """可中断的 Runner，包装 Agent.run()。"""

    async def run(
        self,
        agent: Agent[E],
        context: AgentContext,
        emitter: ContentEmitter[E],
    ) -> AgentResult:
        try:
            return await agent.run(context, emitter)
        except asyncio.CancelledError:
            partial = ""
            if hasattr(emitter, "get_content"):
                partial = emitter.get_content() or ""
            history = await context.history.to_list()
            return AgentResult(
                content=partial or "Task was cancelled before completion.",
                stop_reason="cancelled",
                messages=history,
                partial_content=partial,
            )
