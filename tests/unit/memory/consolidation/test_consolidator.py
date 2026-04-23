"""Tests for Consolidator."""


from framework.core.types import LLMResponse
from framework.memory.consolidation import Consolidator
from framework.memory.core.compression import CompressionContext


class TestStripRuntimePrefixes:
    def test_removes_runtime_context_block(self):
        messages = [
            {
                "role": "user",
                "content": "[Runtime Context]\nchannel=qq, chat_id=123\n\nHello world",
            },
            {"role": "assistant", "content": "Hi there"},
        ]
        result = Consolidator.strip_runtime_prefixes(messages)
        assert result[0]["content"] == "Hello world"
        assert result[1]["content"] == "Hi there"

    def test_leaves_plain_messages_unchanged(self):
        messages = [{"role": "user", "content": "Just a message"}]
        result = Consolidator.strip_runtime_prefixes(messages)
        assert result[0]["content"] == "Just a message"


class TestConsolidatorCompress:
    async def test_compress_with_llm(self):
        class FakeLLM:
            async def chat(self, messages, **kwargs):
                return "User asked about weather."

        strategy = Consolidator(llm_provider=FakeLLM())
        messages = [
            {"role": "user", "content": "What's the weather?"},
            {"role": "assistant", "content": "Sunny."},
        ]
        result = await strategy.compress(messages, CompressionContext())
        # Consolidator 只压缩较旧的前半部分，保留最近的消息
        assert result.pruned_messages == [messages[0]]
        assert result.remaining_messages == [messages[1]]
        assert "weather" in result.summary.lower()

    async def test_compress_with_llm_response_object(self):
        class FakeLLM:
            async def chat(self, messages, **kwargs):
                return LLMResponse(content="User asked about weather.")

        strategy = Consolidator(llm_provider=FakeLLM())
        messages = [
            {"role": "user", "content": "What's the weather?"},
            {"role": "assistant", "content": "Sunny."},
        ]
        result = await strategy.compress(messages, CompressionContext())
        assert result.pruned_messages == [messages[0]]
        assert result.remaining_messages == [messages[1]]
        assert "weather" in result.summary.lower()
        assert "LLMResponse" not in result.summary

    async def test_compress_fallback_on_llm_failure(self):
        class BadLLM:
            async def chat(self, messages, **kwargs):
                raise RuntimeError("API error")

        strategy = Consolidator(llm_provider=BadLLM())
        messages = [
            {"role": "user", "content": "Question one"},
            {"role": "assistant", "content": "Answer one"},
        ]
        result = await strategy.compress(messages, CompressionContext())
        assert result.pruned_messages == [messages[0]]
        assert result.remaining_messages == [messages[1]]
        assert "[Consolidator]" in result.summary

    async def test_compress_empty_messages(self):
        strategy = Consolidator()
        result = await strategy.compress([], CompressionContext())
        assert not result.summary
        assert not result.pruned_messages

    async def test_compress_respects_tool_chain_boundary(self):
        """split_point 不应切断 tool_call + tool_result 链。"""
        strategy = Consolidator()
        messages = [
            {"role": "user", "content": "查天气"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "tc1", "function": {"name": "get_weather"}}],
            },
            {"role": "tool", "content": "晴天", "tool_call_id": "tc1"},
            {"role": "assistant", "content": "北京今天晴天"},
        ]
        result = await strategy.compress(messages, CompressionContext())
        # 4 条消息，split_point=2；但第 2 条是 tool_call，第 3 条是对应的 tool_result
        # 安全分割应将边界扩展到 chain_end=3，因此 to_compress 至少包含前 3 条
        assert len(result.pruned_messages) >= 3
        assert result.remaining_messages == [messages[-1]]

    async def test_strips_runtime_prefixes_before_llm(self):
        class CapturingLLM:
            def __init__(self):
                self.captured = None

            async def chat(self, messages, **kwargs):
                self.captured = messages
                return "Summary"

        llm = CapturingLLM()
        strategy = Consolidator(llm_provider=llm)
        messages = [
            {"role": "user", "content": "First message"},
            {
                "role": "user",
                "content": "[Runtime Context]\nchannel=qq\n\nActual question",
            },
            {"role": "assistant", "content": "Partial answer"},
            {"role": "user", "content": "Follow up"},
        ]
        await strategy.compress(messages, CompressionContext())
        assert llm.captured is not None
        user_msg = llm.captured[-1]["content"]
        assert "[Runtime Context]" not in user_msg
        assert "Actual question" in user_msg
