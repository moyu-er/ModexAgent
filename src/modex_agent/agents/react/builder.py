"""ReActAgent Builder。

封装 ReActAgent 的构建逻辑，使 factory.py 无需了解 ReAct 实现细节。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...core.emitter import TurnBinding, TurnEventSink
    from ...core.provider import LLMProvider
    from ...multi_agent.descriptor import AgentDescriptor


class ReActAgentBuilder:
    """ReActAgent 构建器。

    负责根据 AgentDescriptor 构建 ReActAgent 实例及其 turn-event sink factory。
    """

    @staticmethod
    def build_agent(descriptor: AgentDescriptor, provider: LLMProvider):
        """构建 Agent 实例。"""
        from .agent import ReActAgent

        return ReActAgent(provider=provider)

    @staticmethod
    def build_emitter_factory(emitter_output_adapter):
        """构建 turn-event sink factory（用于 AgentPipeline）。

        Args:
            emitter_output_adapter: 已解析好的 output adapter（BrokerOutputAdapter 或原生 OutputAdapter）
        """
        from ...adapters.emitter import BufferingSink

        def _factory(binding: TurnBinding) -> TurnEventSink:
            return BufferingSink(
                output_adapter=emitter_output_adapter,
                session_id=binding.session_id,
            )

        return _factory
