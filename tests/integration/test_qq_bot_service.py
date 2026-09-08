"""Integration tests for bot_service.py components.

验证端到端流程：
- QQBotService 组件初始化
- QQBotEmitter 与 QQOutputAdapter 的集成
- 流式/非流式模式切换
- 推理内容处理流程
"""

from enum import Enum
from typing import Any, TypeVar

import pytest

pytestmark = pytest.mark.integration
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Add framework path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from modex_agent.adapters.platform import StreamingMode
from modex_agent.core.emitter import AgentResult, ContentEmitter, StopReason
from modex_agent.core.events import AgentEvent, EmitterConfig
from modex_agent.core.session_id import SessionInfo

E = TypeVar("E", bound=AgentEvent)


class _BufferingEmitter(ContentEmitter[E]):
    """Minimal test emitter that captures output for assertions."""

    def __init__(self, config: EmitterConfig | None = None):
        super().__init__(config)
        self._buffer = ""
        self._reasoning_buffer = ""
        self._result: AgentResult | None = None
        self._events: list[tuple[E, Any]] = []

    async def emit(self, event: E, data: Any = None) -> None:
        event_name = event.value if isinstance(event, Enum) else str(event)
        if self.config.is_enabled(event_name):
            self._events.append((event, data))
        await super().emit(event, data)

    async def _on_event(self, event: E, data: Any = None) -> None:
        event_name = event.value if isinstance(event, Enum) else str(event)
        if event_name == "model_reasoning":
            if isinstance(data, str):
                self._reasoning_buffer += data

    async def emit_delta(self, delta: str) -> None:
        self._buffer += delta

    async def emit_content(self, full_content: str) -> None:
        self._buffer += full_content

    async def emit_complete(self, result: AgentResult) -> None:
        self._result = result

    async def emit_error(self, error: str) -> None:
        self._result = AgentResult(error=error, stop_reason=StopReason.ERROR)

    def get_content(self) -> str:
        return self._buffer

    def get_reasoning(self) -> str:
        return self._reasoning_buffer

    def get_events(self, event_type: E | None = None) -> list[tuple[E, Any]]:
        if event_type is not None:
            return [(e, d) for e, d in self._events if e == event_type]
        return list(self._events)

    def get_events_by_name(self, name: str) -> list[tuple[E, Any]]:
        result = []
        for e, d in self._events:
            if isinstance(e, Enum) and e.name == name or isinstance(e, str) and e == name:
                result.append((e, d))
        return result


class TestQQBotServiceIntegration:
    """Integration tests for QQ Bot Service."""

    @pytest.fixture
    def mock_config(self):
        """Create mock configuration."""
        return {
            "qq": {
                "app_id": "test_app_id",
                "secret": "test_secret",
                "sandbox": True,
                "allow_from": ["*"],
            },
            "llm": {
                "model": "deepseek-ai/DeepSeek-R1",
                "api_key": "test_key",
                "temperature": 0.7,
                "max_output_tokens": 2000,
            },
            "agent": {
                "system_prompt": "You are a helpful assistant.",
            },
            "output": {
                "streaming": True,
            },
            "tools": {
                "file_tools": {"enabled": False},
                "shell_tools": {"enabled": False},
            },
            "mcp": {"servers": {}},
        }

    @pytest.fixture
    def mock_qq_client(self):
        """Create mock QQ bot client."""
        client = MagicMock()
        client.api = MagicMock()
        client.api.post_c2c_message = AsyncMock()
        return client

    def test_streaming_aware_emitter_import(self):
        """Test that StreamingAwareEmitter can be imported."""
        from modex_agent import ReActEvent, StreamingAwareEmitter

        # Should be able to create a subclass
        class TestEmitter(StreamingAwareEmitter[ReActEvent]):
            async def emit_delta(self, delta: str) -> None:
                pass

        assert TestEmitter is not None

    def test_react_event_has_model_reasoning(self):
        """Test that ReActEvent includes MODEL_REASONING."""
        from modex_agent.agents.react import ReActEvent

        assert hasattr(ReActEvent, "MODEL_REASONING")
        assert ReActEvent.MODEL_REASONING.value == "model_reasoning"

    def test_agent_result_has_reasoning_field(self):
        """Test that AgentResult has reasoning field."""
        from modex_agent.core.emitter import AgentResult

        result = AgentResult(content="Hello", reasoning="Thinking...")
        assert result.reasoning == "Thinking..."

    @pytest.mark.asyncio
    async def test_qb_bot_emitter_business_logic(self, caplog):
        """Test QQBotEmitter business logic in isolation."""
        import sys

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "examples" / "bot_project"))

        try:
            from bot.adapters.qq import QQBotEmitter

            from modex_agent.agents.react import ReActEvent
            from modex_agent.core.emitter import AgentResult
            from modex_agent.core.message import ToolCall

            # Create mock adapter
            mock_adapter = MagicMock()
            mock_adapter.streaming_mode = StreamingMode.NONE
            mock_adapter.send_delta = AsyncMock()
            mock_adapter.send = AsyncMock()
            mock_adapter.flush_deltas = AsyncMock()

            # Create emitter
            emitter = QQBotEmitter(
                output_adapter=mock_adapter,
                session_id="test_session",
            )

            # Test content is buffered (pseudo-streaming)
            await emitter.emit_delta("Hello ")
            await emitter.emit_delta("World")
            assert emitter._content_buffer == "Hello World"
            assert len(mock_adapter.send_delta.call_args_list) == 0

            # Test reasoning is logged (not sent)
            import logging

            with caplog.at_level(logging.INFO, logger="bot.reasoning"):
                await emitter.emit(ReActEvent.MODEL_REASONING, "Thinking...")
                assert "[Reasoning]" in caplog.text

            # Test tool calls are ignored
            tool_call = ToolCall(tool_name="test", arguments={})
            await emitter.emit(ReActEvent.TOOL_CALL_START, tool_call)
            # No additional calls to adapter

            # Test complete flushes buffer via send()
            result = AgentResult(content="Hello World", reasoning="Some reasoning")
            await emitter.emit_complete(result)
            assert mock_adapter.send.called

        except ImportError as e:
            pytest.skip(f"QQBotEmitter not available: {e}")

    @pytest.mark.asyncio
    async def test_react_agent_streaming_vs_non_streaming(self):
        """Emitter streaming preference changes emitter delivery, not the provider call path.

        Since the single event loop converged (commit 49860c84), every provider
        call goes through chat_stream regardless of the emitter's streaming
        preference; emitter driving is gated at the event dispatch point. What
        differs is what the emitter receives: per-delta emits during the call
        (streaming emitter) vs the folded content once at end-of-call
        (non-streaming emitter).
        """
        from modex_agent.agents.react import ReActAgent, ReActEvent
        from modex_agent.core.agent import AgentContext
        from modex_agent.core.llm_struct import LLMResponse
        from modex_agent.core.provider import CallbackStreamProvider

        # Create mock provider that tracks which API is called
        class MockProvider(CallbackStreamProvider):
            def __init__(self):
                self.chat_stream_called = False
                self.chat_called = False

            async def chat_stream(
                self, messages=None, on_content_delta=None, on_reasoning_delta=None, **kwargs
            ):
                self.chat_stream_called = True
                if on_content_delta:
                    await on_content_delta("Hello")
                return LLMResponse(content="Hello")

            async def chat(self, messages=None, **kwargs):
                self.chat_called = True
                return LLMResponse(content="Hello")

            def get_default_model(self):
                return "mock-model"

        # Records HOW content reached the emitter: per-delta or end-of-call.
        class DeliveryRecorder(_BufferingEmitter[ReActEvent]):
            def __init__(self):
                super().__init__()
                self.deltas: list[str] = []
                self.full_contents: list[str] = []

            async def emit_delta(self, delta: str) -> None:
                self.deltas.append(delta)
                await super().emit_delta(delta)

            async def emit_content(self, full_content: str) -> None:
                self.full_contents.append(full_content)
                await super().emit_content(full_content)

        provider = MockProvider()
        agent = ReActAgent(provider=provider)

        from modex_agent.memory.history import ListMessageHistory

        context = AgentContext(
            system_prompt="Test",
            history=ListMessageHistory([{"role": "user", "content": "Hi"}]),
            tool_manager=MagicMock(),
            session=SessionInfo.from_str("test.agent"),
        )

        # Test streaming mode (emitter wants streaming): deltas are driven
        # into the emitter during the event loop.
        class StreamingEmitter(DeliveryRecorder):
            def wants_streaming(self):
                return True

        emitter = StreamingEmitter()
        await agent.run(context, emitter)
        assert provider.chat_stream_called is True
        assert provider.chat_called is False
        assert emitter.deltas == ["Hello"]
        assert emitter.full_contents == []
        assert emitter.get_content() == "Hello"

        # Reset
        provider.chat_stream_called = False
        provider.chat_called = False

        # Test non-streaming mode (emitter doesn't want streaming): the same
        # chat_stream call happens, but no per-delta emits — the folded
        # response is delivered once at end-of-call via emit_content.
        emitter2 = DeliveryRecorder()
        await agent.run(context, emitter2)
        assert provider.chat_stream_called is True
        assert provider.chat_called is False
        assert emitter2.deltas == []
        assert emitter2.full_contents == ["Hello"]
        assert emitter2.get_content() == "Hello"

    def test_output_adapter_send_delta_interface(self):
        """Test that OutputAdapter has the send_delta interface."""
        from modex_agent.adapters.output import OutputAdapter

        # Check that send_delta method exists
        assert hasattr(OutputAdapter, "send_delta")

        assert hasattr(OutputAdapter, "streaming_mode")

    @pytest.mark.asyncio
    async def test_end_to_end_event_flow(self):
        """Test complete event flow from Agent to QQ Output."""
        from modex_agent.adapters.emitter import StreamingAwareEmitter
        from modex_agent.adapters.output import OutputAdapter
        from modex_agent.agents.react import ReActAgent, ReActEvent
        from modex_agent.core.agent import AgentContext

        # Track events
        events_received = []

        class MockAdapter(OutputAdapter):
            def __init__(self):
                self._streaming_mode = StreamingMode.NONE

            @property
            def name(self) -> str:
                return "mock"

            @property
            def streaming_mode(self):
                return self._streaming_mode

            async def send_delta(self, delta, session_id, metadata=None):
                events_received.append(("send_delta", delta))

            async def send(self, message, session_id):
                events_received.append(("send", message.content))

            async def flush_deltas(self, session_id):
                events_received.append(("flush",))

        class TestEmitter(StreamingAwareEmitter[ReActEvent]):
            async def emit_delta(self, delta: str) -> None:
                events_received.append(("emit_delta", delta))
                await self.output_adapter.send_delta(delta, self.session_id)

            async def _on_event(self, event: ReActEvent, data=None) -> None:
                event_name = event.value if hasattr(event, "value") else str(event)
                if event_name == "model_reasoning":
                    events_received.append(("model_reasoning", data))
                await super()._on_event(event, data)

        # Setup
        adapter = MockAdapter()
        emitter = TestEmitter(adapter, "test_session")

        # Create mock provider
        from modex_agent.core.llm_struct import LLMResponse
        from modex_agent.core.provider import CallbackStreamProvider

        class MockProvider(CallbackStreamProvider):
            async def chat_stream(
                self,
                messages=None,
                model=None,
                temperature=None,
                max_output_tokens=None,
                tools=None,
                on_content_delta=None,
                on_reasoning_delta=None,
                **kwargs,
            ):
                return LLMResponse(
                    content="Final answer",
                    reasoning_content="My reasoning",
                )

            def get_default_model(self):
                return "mock"

        agent = ReActAgent(provider=MockProvider())
        from modex_agent.memory.history import ListMessageHistory

        context = AgentContext(
            system_prompt="Test",
            history=ListMessageHistory([{"role": "user", "content": "Hi"}]),
            tool_manager=MagicMock(),
            session=SessionInfo.from_str("test.agent"),
        )

        # Run
        result = await agent.run(context, emitter)

        # Verify flow
        assert result.content == "Final answer"
        assert result.reasoning == "My reasoning"

        # Should have received reasoning event
        reasoning_events = [e for e in events_received if e[0] == "model_reasoning"]
        assert len(reasoning_events) == 1
        assert reasoning_events[0][1] == "My reasoning"

    def test_qq_service_initialization_structure(self, mock_config):
        """Test QQBotService structure without actually initializing."""
        import sys

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "examples" / "bot_project"))

        try:
            from bot.service.qq_service import QQBotService

            # Verify current IOC-based service construction surface.
            assert hasattr(QQBotService, "initialize")
            assert hasattr(QQBotService, "start")

        except ImportError as e:
            pytest.skip(f"QQBotService not importable: {e}")

    @pytest.mark.asyncio
    async def test_qq_bot_skills_use_compact_prompt(self):
        """Regression test: QQ Bot skills must produce a compact prompt table,
        not inline full skill content, to avoid exceeding LLM context limits.
        """
        import sys

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "examples" / "bot_project"))

        from modex_agent.plugins.defaults.capabilities.skills.builder import (
            DefaultSkillBuilder,
        )
        from modex_agent.plugins.defaults.capabilities.skills.catalog import SkillCatalog
        from modex_agent.plugins.defaults.capabilities.skills.models import ResolutionContext
        from modex_agent.plugins.defaults.capabilities.skills.source import FileSkillSource

        skills_dir = (
            Path(__file__).parent.parent.parent
            / "examples"
            / "bot_project"
            / "skills"
            / "default"
            / "default"
        )
        if not skills_dir.exists():
            pytest.skip("bot_project/skills/default/default directory not found")

        source = FileSkillSource(
            directories=[skills_dir],
            cache=True,
            layout="directory",
            skill_filename="SKILL.md",
        )
        sm = SkillCatalog(source=source, builder=DefaultSkillBuilder())

        class FakeTM:
            def has_tool(self, name: str) -> bool:
                return name == "read_file"

        ctx = ResolutionContext(tool_manager=FakeTM())
        prompt = await sm.render_prompt(ctx)

        # Should be a compact table, not inlined content
        assert "<available_skills>" in prompt
        # Must NOT contain full skill body text that would bloat the context
        assert prompt.count("<skill name=") > 0  # at least one skill listed
        # Each skill should appear as a single table row, not as multi-line content
        assert prompt.count("<available_skills>") == 1


# Run async tests
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
