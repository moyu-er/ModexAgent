"""Tests for shared memory utilities."""

from modex_agent.memory.utils import (
    EMPTY_MEMORY_SUMMARY_MARKERS,
    estimate_token_count,
    normalize_memory_summary,
)
from modex_agent.utils.helpers import strip_think


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


class TestStripThink:
    def test_removes_think_tags(self):
        assert strip_think("<think>思考内容</think>实际内容") == "实际内容"

    def test_removes_thinking_tags(self):
        assert strip_think("<thinking>reasoning</thinking>real content") == "real content"

    def test_case_insensitive(self):
        assert strip_think("<THINK>abc</THINK>def") == "def"
        assert strip_think("<Thinking>abc</Thinking>def") == "def"

    def test_multiline_think(self):
        raw = "<think>\nline1\nline2\n</think>\nactual"
        assert strip_think(raw) == "actual"

    def test_unclosed_think_removed(self):
        assert strip_think("<think>unfinished reasoning") is None

    def test_tibetan_unicode_removed(self):
        assert strip_think("༺思考内容༽实际内容") == "实际内容"

    def test_no_think_returns_original(self):
        assert strip_think("hello world") == "hello world"

    def test_empty_returns_none(self):
        assert strip_think("") is None
        assert strip_think(None) is None

    def test_only_think_returns_none(self):
        assert strip_think("<think>only thinking</think>") is None

    def test_mid_content_think_tags_preserved(self):
        """内容中间出现的 think 标签不应被误删（前缀-only 语义）。"""
        assert strip_think("Hello <think/> world") == "Hello <think/> world"
        assert strip_think("Text with <thinking>discussed</thinking> mid") == "Text with <thinking>discussed</thinking> mid"
        assert strip_think("Here is <think>example</think> of tag") == "Here is <think>example</think> of tag"

    def test_leading_indentation_preserved(self):
        """移除思维链后应保留代码块等合法缩进，不要 lstrip 所有空白。"""
        raw = "<think>reasoning</think>\n    def foo():\n        pass"
        assert strip_think(raw) == "    def foo():\n        pass"

    def test_think_prefix_with_extra_closed_tag_in_content(self):
        """前缀思维链+内容中有闭合 think 标签：只删前缀，保留内容中的标签。"""
        raw = "<think>my reasoning</think>See <think>example</think> above"
        assert strip_think(raw) == "See <think>example</think> above"

    def test_unclosed_thinking_returns_none(self):
        assert strip_think("<thinking>unfinished") is None

    def test_uppercase_thinking_prefix(self):
        assert strip_think("<THINKING>abc</THINKING>def") == "def"


class TestNormalizeMemorySummary:
    def test_none_returns_none(self):
        assert normalize_memory_summary(None) is None

    def test_empty_string_returns_none(self):
        assert normalize_memory_summary("") is None
        assert normalize_memory_summary("   ") is None

    def test_known_markers_filtered(self):
        for marker in EMPTY_MEMORY_SUMMARY_MARKERS:
            assert normalize_memory_summary(marker) is None

    def test_meaningful_summary_preserved(self):
        assert normalize_memory_summary("User asked about Python") == "User asked about Python"

    def test_too_short_filtered(self):
        assert normalize_memory_summary("ok") is None
        assert normalize_memory_summary("hi") is None

    def test_whitespace_only_filtered(self):
        assert normalize_memory_summary("!!! ???") is None
