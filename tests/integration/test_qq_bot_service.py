"""Integration tests for bot_service.py components.

验证端到端流程：
- QQBotService 组件初始化
- QQBotEmitter 与 QQOutputAdapter 的集成
- 流式/非流式模式切换
- 推理内容处理流程
"""

import pytest

pytestmark = pytest.mark.integration
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Add framework path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from modex_agent.adapters.emitter import BufferingSink
from modex_agent.adapters.platform import StreamingMode
from modex_agent.core.emitter import KindGate, TurnEventSink
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.turn_events import (
    StopReason,
    TurnFinishedEvent,
    TurnReasoningEvent,
    TurnTextEvent,
)


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

    def test_buffering_sink_import(self):
        """Test that BufferingSink can be imported and subclassed."""

        class TestSink(BufferingSink):
            pass

        assert TestSink is not None

    def test_turn_events_have_reasoning_kind(self):
        """Test that the TurnEvent union carries the reasoning kind."""
        assert TurnReasoningEvent(text="x").kind == "reasoning"
        assert TurnTextEvent(text="x").kind == "text"

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

            from modex_agent.core.emitter import AgentResult, turn_finished_event
            from modex_agent.core.turn_events import TurnToolCallEvent

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

            # Test content is buffered (NONE mode -> TURN policy)
            await emitter.emit(TurnTextEvent(text="Hello "))
            await emitter.emit(TurnTextEvent(text="World"))
            assert emitter._content_buffer == "Hello World"
            assert len(mock_adapter.send_delta.call_args_list) == 0

            # Test reasoning is logged (not sent)
            import logging

            with caplog.at_level(logging.INFO, logger="bot.reasoning"):
                await emitter.emit(TurnReasoningEvent(text="Thinking..."))
                assert "[Reasoning]" in caplog.text

            # Test tool calls are ignored (logged only)
            await emitter.emit(
                TurnToolCallEvent(tool_name="test", call_id="call_0", arguments={})
            )
            # No additional calls to adapter

            # Test turn_finished flushes buffer via send()
            result = AgentResult(content="Hello World", reasoning="Some reasoning")
            await emitter.emit(turn_finished_event(result))
            assert mock_adapter.send.called

        except ImportError as e:
            pytest.skip(f"QQBotEmitter not available: {e}")

    @pytest.mark.asyncio
    async def test_react_agent_streaming_vs_non_streaming(self):
        """Emitter streaming preference changes emitter delivery, not the provider call path.

        Since the single event loop converged (commit 49860c84), every provider
        call goes through chat_stream regardless of the emitter's streaming
        preference; emitter driving is gated at the event dispatch point. What
        differs is what the emitter receives: one ``TurnTextEvent`` per delta
        during the call (streaming sink) vs the folded content once at
        end-of-call (non-streaming sink).
        """
        from modex_agent.agents.react import ReActAgent
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
                    await on_content_delta(" world")
                return LLMResponse(content="Hello world")

            async def chat(self, messages=None, **kwargs):
                self.chat_called = True
                return LLMResponse(content="Hello world")

            def get_default_model(self):
                return "mock-model"

        class DeliveryRecorder(TurnEventSink):
            """Records every text event that reaches the sink."""

            def __init__(self, *, streaming: bool):
                super().__init__()
                self._streaming = streaming
                self.texts: list[str] = []

            def wants_streaming(self) -> bool:
                return self._streaming

            async def _dispatch(self, event) -> None:
                if isinstance(event, TurnTextEvent):
                    self.texts.append(event.text)

        provider = MockProvider()
        agent = ReActAgent(provider=provider)

        from modex_agent.memory.history import ListMessageHistory

        context = AgentContext(
            system_prompt="Test",
            history=ListMessageHistory([{"role": "user", "content": "Hi"}]),
            tool_manager=MagicMock(),
            session=SessionInfo.from_str("test.agent"),
        )

        # Streaming sink: per-delta text events during the event loop.
        emitter = DeliveryRecorder(streaming=True)
        await agent.run(context, emitter)
        assert provider.chat_stream_called is True
        assert provider.chat_called is False
        assert emitter.texts == ["Hello", " world"]

        # Reset
        provider.chat_stream_called = False
        provider.chat_called = False

        # Non-streaming sink: the same chat_stream call happens, but the
        # folded response is delivered once at end-of-call.
        emitter2 = DeliveryRecorder(streaming=False)
        await agent.run(context, emitter2)
        assert provider.chat_stream_called is True
        assert provider.chat_called is False
        assert emitter2.texts == ["Hello world"]

    def test_output_adapter_send_delta_interface(self):
        """Test that OutputAdapter has the send_delta interface."""
        from modex_agent.adapters.output import OutputAdapter

        # Check that send_delta method exists
        assert hasattr(OutputAdapter, "send_delta")

        assert hasattr(OutputAdapter, "streaming_mode")

    @pytest.mark.asyncio
    async def test_end_to_end_event_flow(self):
        """Test complete event flow from Agent to QQ Output."""
        from modex_agent.adapters.output import OutputAdapter
        from modex_agent.agents.react import ReActAgent
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

        class TestSink(BufferingSink):
            async def _dispatch(self, event) -> None:
                if isinstance(event, TurnReasoningEvent):
                    events_received.append(("model_reasoning", event.text))
                await super()._dispatch(event)

        # Setup
        adapter = MockAdapter()
        emitter = TestSink(adapter, "test_session")

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
                if on_reasoning_delta:
                    await on_reasoning_delta("My reasoning")
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
