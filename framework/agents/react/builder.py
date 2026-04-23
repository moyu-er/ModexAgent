"""ReActAgent Builder。

封装 ReActAgent 的构建逻辑，使 factory.py 无需了解 ReAct 实现细节。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...core.emitter import ContentEmitter
    from ...core.provider import LLMProvider
    from ...multi_agent.descriptor import AgentDescriptor


class ReActAgentBuilder:
    """ReActAgent 构建器。

    负责根据 AgentDescriptor 构建 ReActAgent 实例及其 emitter factory。
    """

    @staticmethod
    def build_agent(descriptor: AgentDescriptor, provider: LLMProvider):
        """构建 Agent 实例。"""
        from .agent import ReActAgent

        return ReActAgent(provider=provider)

    @staticmethod
    def build_emitter_factory(emitter_output_adapter):
        """构建 emitter factory（用于 AgentPipeline）。

        Args:
            emitter_output_adapter: 已解析好的 output adapter（BrokerOutputAdapter 或原生 OutputAdapter）
        """
        from .agent import ReActEvent
        from ...core.emitter import StreamingAwareEmitter

        def _factory(session_id: str) -> ContentEmitter:
            return StreamingAwareEmitter[ReActEvent](
                output_adapter=emitter_output_adapter,
                session_id=session_id,
            )

        return _factory
