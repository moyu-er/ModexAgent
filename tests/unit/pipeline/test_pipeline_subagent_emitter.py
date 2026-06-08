"""Tests for AgentPipeline emitter selection in multi-agent scenarios.

Verifies that:
- Main agent always uses factory emitter (regardless of source_agent)
- Subagent agents (no emitter_factory) always use StreamingAwareEmitter
"""

from unittest.mock import MagicMock

import pytest

from framework.core.context import ContextState
from framework.core.emitter import AgentResult, StreamingAwareEmitter
from framework.core.types import InputMessage
from framework.pipeline.pipeline import AgentPipeline


class _CapturingAgent:
    """Agent that records the emitter passed to run()."""

    name = "test-agent"

    def __init__(self):
        self.received_emitter = None

    async def run(self, context, emitter):
        self.received_emitter = emitter
        return AgentResult(content="done")


class _StubContextManager:
    async def load_with_metadata(self, session_id, metadata=None):
        return ContextState(system_prompt="test", history=[])

    async def load(self, session_id, **kwargs):
        return ContextState(system_prompt="test", history=[])

    async def save(self, **kwargs):
        pass

    async def flush(self, session_id):
        pass

    async def clear_checkpoint(self, session_id):
        pass

    async def save_checkpoint(self, session_id, messages):
        pass

    async def load_checkpoint(self, session_id):
        return None

    async def build_system_prompt(self, **kwargs):
        return "test system prompt"

    def wrap_governance(self, governance, session_id):
        return governance


class _StubToolManager:
    async def startup(self):
        pass

    async def shutdown(self):
        pass

    def list_tools(self):
        return []


class _StubOutputAdapter:
    async def send(self, message, session_id):
        pass

    async def send_delta(self, delta, session_id):
        pass

    async def flush_deltas(self, session_id):
        pass

    @property
    def supports_streaming(self):
        return False


class _StubInputAdapter:
    async def start(self):
        pass

    async def stop(self):
        pass

    async def receive(self):
        yield  # empty async generator


class TestPipelineEmitterSelection:
    """Verify emitter type selected based on emitter_factory presence."""

    def _make_pipeline(self, emitter_factory=None):
        agent = _CapturingAgent()
        pipeline = AgentPipeline(
            agent=agent,
            context_manager=_StubContextManager(),
            tool_manager=_StubToolManager(),
            input_adapter=_StubInputAdapter(),
            output_adapter=_StubOutputAdapter(),
            emitter_factory=emitter_factory,
            sanitizer=None,
        )
        return pipeline, agent

    async def test_main_with_source_agent_uses_factory_emitter(self):
        """Main agent + subagent message → factory emitter (NOT StreamingAwareEmitter).

        Main agent should always use factory emitter so the LLM's assistant
        response (including tool outputs like SendFileToUser) reaches the user.
        """
        factory_emitter = MagicMock()
        pipeline, agent = self._make_pipeline(emitter_factory=lambda sid: factory_emitter)
        msg = InputMessage(
            content="subagent result",
            session_id="conv_001:main",
            metadata={"source_agent": "office-expert"},
        )

        await pipeline._process_message_locked(msg, "conv_001:main")

        assert agent.received_emitter is factory_emitter

    async def test_main_without_source_agent_uses_factory_emitter(self):
        """Main agent + user message → factory emitter (normal output)."""
        factory_emitter = MagicMock()
        pipeline, agent = self._make_pipeline(emitter_factory=lambda sid: factory_emitter)
        msg = InputMessage(content="hello", session_id="conv_001:main")

        await pipeline._process_message_locked(msg, "conv_001:main")

        assert agent.received_emitter is factory_emitter

    async def test_subagent_with_source_agent_uses_streaming_emitter(self):
        """Subagent agent (no emitter_factory) + main message → StreamingAwareEmitter."""
        pipeline, agent = self._make_pipeline(emitter_factory=None)
        msg = InputMessage(
            content="please help",
            session_id="conv_001:main:office-expert",
            metadata={"source_agent": "main"},
        )

        await pipeline._process_message_locked(msg, "conv_001:main:office-expert")

        assert isinstance(agent.received_emitter, StreamingAwareEmitter)

    async def test_subagent_without_source_agent_uses_streaming_emitter(self):
        """Subagent agent (no emitter_factory) + user message → StreamingAwareEmitter."""
        pipeline, agent = self._make_pipeline(emitter_factory=None)
        msg = InputMessage(content="hello", session_id="conv_001:main:office-expert")

        await pipeline._process_message_locked(msg, "conv_001:main:office-expert")

        assert isinstance(agent.received_emitter, StreamingAwareEmitter)
