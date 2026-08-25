"""Tests for LiteLLMProvider finish_reason=length pending tool-call drop.

W0 audit P4: a stream cut at the max_tokens ceiling leaves tool calls
truncated mid-arguments; the provider must NOT flush them into the
LLMResponse (repaired-truncated or empty arguments would execute in the
ReAct loop). Only completed calls ride the response on a length ending;
any other finish reason keeps the historical partial-flush behavior.
"""

from unittest.mock import AsyncMock, patch

import pytest

from modex_agent.core.constants import FinishReason
from modex_agent.core.message import ChatMessage
from modex_agent.core.types import MessageRole
from modex_agent.providers.litellm_provider import LiteLLMProvider


class MockToolFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class MockToolCall:
    def __init__(self, index, call_id, function):
        self.index = index
        self.id = call_id
        self.function = function


class MockDelta:
    def __init__(self, content=None, reasoning_content=None, tool_calls=None):
        self.content = content
        self.reasoning_content = reasoning_content
        self.tool_calls = tool_calls


class MockChoice:
    def __init__(self, delta, finish_reason=None):
        self.delta = delta
        self.finish_reason = finish_reason


class MockChunk:
    def __init__(self, choices, usage=None):
        self.choices = choices
        self.usage = usage


class TestLiteLLMProviderLengthDrop:
    """finish_reason=length must drop pending (incomplete) tool calls."""

    @pytest.fixture
    def provider(self):
        with patch.dict("os.environ", {"LITELLM_LOG": "ERROR"}):
            provider = LiteLLMProvider(model="test-model", api_key="test-key")
            provider._acompletion = AsyncMock()
            return provider

    @staticmethod
    def _tool_call(index, call_id, name, args):
        return MockToolCall(
            index=index,
            call_id=call_id,
            function=MockToolFunction(name=name, arguments=args),
        )

    def _tool_chunk(self, tool_calls, finish_reason=None):
        return MockChunk(
            choices=[
                MockChoice(MockDelta(tool_calls=tool_calls), finish_reason=finish_reason)
            ]
        )

    def _mock_stream(self, chunks):
        async def stream():
            for c in chunks:
                yield c

        return stream()

    @pytest.mark.asyncio
    async def test_length_drop_keeps_only_completed_calls(self, provider):
        """Stream ends mid-arguments at the token ceiling → only the completed
        call rides the response; the truncated call is dropped."""
        chunks = [
            self._tool_chunk(
                [self._tool_call(0, "call_1", "search", '{"query": "test"}')]
            ),
            # Truncated mid-arguments: unparseable, but repairable into a
            # valid-looking (wrong) payload — exactly the unsafe case.
            self._tool_chunk(
                [self._tool_call(1, "call_2", "write_file", '{"path": "a.py", "content": "trunc')]
            ),
            self._tool_chunk([], finish_reason="length"),
        ]
        provider._acompletion.return_value = self._mock_stream(chunks)

        result = await provider.chat_stream(
            messages=[ChatMessage(role=MessageRole.USER, content="search")],
        )

        assert result.finish_reason == FinishReason.LENGTH.value
        assert [tc.tool_name for tc in result.tool_calls] == ["search"]
        assert result.tool_calls[0].arguments == {"query": "test"}

    @pytest.mark.asyncio
    async def test_length_drop_all_incomplete_leaves_tool_calls_empty(self, provider):
        """Every call truncated at the ceiling → tool_calls empty (the empty
        ending then flows into LengthGuard downstream)."""
        chunks = [
            self._tool_chunk(
                [self._tool_call(0, "call_9", "write_file", '{"path": "a.py", "content": "trunc')]
            ),
            self._tool_chunk([], finish_reason="length"),
        ]
        provider._acompletion.return_value = self._mock_stream(chunks)

        result = await provider.chat_stream(
            messages=[ChatMessage(role=MessageRole.USER, content="write")],
        )

        assert result.finish_reason == FinishReason.LENGTH.value
        assert result.tool_calls == []
        assert result.has_tool_calls is False

    @pytest.mark.asyncio
    async def test_stop_ending_still_flushes_pending_calls(self, provider):
        """Clean stop ending → partial-flush behavior unchanged: the pending
        (incomplete) call still rides the response with recovered arguments."""
        chunks = [
            self._tool_chunk(
                [self._tool_call(0, "call_1", "search", '{"query": "test"}')]
            ),
            # Unrepairably truncated → flushed as the call with empty args
            self._tool_chunk(
                [self._tool_call(1, "call_2", "send_to_agent", '{"target_agent":"reviewer","content":"hel')]
            ),
            self._tool_chunk([], finish_reason="stop"),
        ]
        provider._acompletion.return_value = self._mock_stream(chunks)

        result = await provider.chat_stream(
            messages=[ChatMessage(role=MessageRole.USER, content="search")],
        )

        assert result.finish_reason == FinishReason.STOP.value
        assert [tc.tool_name for tc in result.tool_calls] == ["search", "send_to_agent"]
        assert isinstance(result.tool_calls[1].arguments, dict)
