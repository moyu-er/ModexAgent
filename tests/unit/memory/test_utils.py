"""Tests for shared memory utilities."""

from framework.memory.utils import estimate_token_count


class TestEstimateTokenCount:
    def test_english_estimate(self):
        # 100 ASCII chars should be ~25 tokens under old logic, still roughly valid
        msgs = [{"role": "user", "content": "a" * 100}]
        assert estimate_token_count(msgs) > 0

    def test_chinese_not_underestimated(self):
        """中文不应再使用 chars//4 的严重低估逻辑。"""
        chinese_text = "这是一个中文句子。" * 10  # 110 chars (10 sentences * 11 chars)
        msgs = [{"role": "user", "content": chinese_text}]
        estimated = estimate_token_count(msgs)
        # Old logic would give 110//4 = 27, which is ~4x too low for Chinese.
        # New logic should be at least 55 (0.5 per char) + overhead.
        assert estimated >= 50, f"Chinese token estimate too low: {estimated}"

    def test_mixed_content_estimate(self):
        mixed = "Hello world 你好世界" * 20  # 240 chars, half ASCII half CJK
        msgs = [{"role": "user", "content": mixed}]
        estimated = estimate_token_count(msgs)
        assert estimated > 0

    def test_tool_calls_add_tokens(self):
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1", "function": {"name": "weather"}}]}
        ]
        assert estimate_token_count(msgs) > len(msgs) * 2

    def test_empty_messages_zero(self):
        assert estimate_token_count([]) == 0
