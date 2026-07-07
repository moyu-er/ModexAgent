"""LoopDetectionHook helpers — pure function tests."""
import json

import pytest

from modex_agent.core.message import ChatMessage
from modex_agent.core.types import LLMResponse, ToolCall
from modex_agent.hook.builtin.loop_detection import (
    LoopDetectionHook,
    _collect_recent_assistants,
    _normalize_text,
    _similarity,
    _tool_calls_fingerprint,
    _AssistantView,
)
from modex_agent.control.exceptions import LoopDetectedError


class TestNormalize:
    def test_collapses_whitespace_and_lowercases(self):
        assert _normalize_text("  Hello   World  ") == "hello world"

    def test_empty(self):
        assert _normalize_text("   ") == ""


class TestSimilarity:
    def test_identical(self):
        assert _similarity("abc", "abc") == 1.0

    def test_both_empty(self):
        assert _similarity("", "") == 1.0

    def test_one_empty(self):
        assert _similarity("a", "") == 0.0

    def test_normalization_applied(self):
        # whitespace/case differences should not drop similarity to 0
        assert _similarity("Hello World", "hello   world") == 1.0

    def test_below_one_for_different(self):
        assert 0.0 <= _similarity("completely different text", "totally unrelated words") < 0.85


class TestToolFingerprint:
    def test_openai_dict_format(self):
        calls = [
            {"id": "c1", "type": "function",
             "function": {"name": "read", "arguments": json.dumps({"path": "/a"})}},
            {"id": "c2", "type": "function",
             "function": {"name": "ls", "arguments": json.dumps({"path": "/b"})}},
        ]
        fp = _tool_calls_fingerprint(calls)
        assert ("read", '{"path": "/a"}') in fp
        assert ("ls", '{"path": "/b"}') in fp

    def test_toolcall_dataclass_format(self):
        calls = [ToolCall(tool_name="read", arguments={"path": "/a"}, call_id="c1")]
        fp = _tool_calls_fingerprint(calls)
        assert ("read", '{"path": "/a"}') in fp

    def test_ignores_call_id_and_order(self):
        a = [ToolCall(tool_name="read", arguments={"path": "/a"}, call_id="c1"),
             ToolCall(tool_name="ls", arguments={"path": "/b"}, call_id="c2")]
        b = [ToolCall(tool_name="ls", arguments={"path": "/b"}, call_id="zzz"),
             ToolCall(tool_name="read", arguments={"path": "/a"}, call_id="yyy")]
        assert _tool_calls_fingerprint(a) == _tool_calls_fingerprint(b)

    def test_arguments_key_order_invariant(self):
        a = [ToolCall(tool_name="x", arguments={"a": 1, "b": 2})]
        b = [ToolCall(tool_name="x", arguments={"b": 2, "a": 1})]
        assert _tool_calls_fingerprint(a) == _tool_calls_fingerprint(b)

    def test_empty(self):
        assert _tool_calls_fingerprint([]) == frozenset()


class TestCollectRecentAssistants:
    def _asst(self, content, tool_calls=None):
        return ChatMessage(role="assistant", content=content, tool_calls=tool_calls)

    def test_stops_at_user(self):
        msgs = [
            self._asst("old1"),
            ChatMessage(role="user", content="hi"),
            self._asst("a"),
            self._asst("b"),
        ]
        current = LLMResponse(content="c", finish_reason="stop")
        views = _collect_recent_assistants(msgs, current)
        assert [v.content for v in views] == ["a", "b", "c"]

    def test_ignores_tool_messages(self):
        msgs = [
            self._asst("a"),
            ChatMessage(role="tool", content="result", tool_call_id="1", name="read"),
            self._asst("b"),
        ]
        current = LLMResponse(content="c", finish_reason="stop")
        views = _collect_recent_assistants(msgs, current)
        assert [v.content for v in views] == ["a", "b", "c"]

    def test_current_response_tool_calls_carried(self):
        msgs = [self._asst(None, [
            {"id": "c1", "type": "function", "function": {"name": "read", "arguments": '{"path": "/a"}'}}
        ])]
        current = LLMResponse(
            content=None, finish_reason="tool_calls",
            tool_calls=[ToolCall(tool_name="read", arguments={"path": "/a"}, call_id="c2")],
        )
        views = _collect_recent_assistants(msgs, current)
        assert len(views) == 2
        # both assistant views carry a read fingerprint
        for v in views:
            assert ("read", '{"path": "/a"}') in v.tool_fp

    def test_user_boundary_ignores_prior_turn_loop(self):
        """A long run of identical assistant messages in a previous turn must
        not leak across the user boundary and cause a false loop detection."""
        msgs = [
            ChatMessage(role="assistant", content="repeated old"),
            ChatMessage(role="assistant", content="repeated old"),
            ChatMessage(role="assistant", content="repeated old"),
            ChatMessage(role="assistant", content="repeated old"),
            ChatMessage(role="user", content="try again"),
            ChatMessage(role="assistant", content="new"),
        ]
        current = LLMResponse(content="new", finish_reason="stop")
        views = _collect_recent_assistants(msgs, current)
        # Only the current turn's assistants (after the user message) are kept.
        assert [v.content for v in views] == ["new", "new"]

    def test_mixed_tool_and_user_boundary(self):
        """Tool messages are skipped, and the user boundary still blocks older
        assistant messages even when tool messages are interleaved."""
        msgs = [
            ChatMessage(role="assistant", content="old"),
            ChatMessage(role="user", content="retry"),
            ChatMessage(role="assistant", content="a"),
            ChatMessage(role="tool", content="result", tool_call_id="1", name="read"),
            ChatMessage(role="assistant", content="b"),
        ]
        current = LLMResponse(content="c", finish_reason="stop")
        views = _collect_recent_assistants(msgs, current)
        assert [v.content for v in views] == ["a", "b", "c"]


def _ctx_with_history(messages):
    """Build an AgentContext whose history returns the given messages."""
    from modex_agent.core.agent import AgentContext
    from modex_agent.memory.history import ListMessageHistory
    from modex_agent.core.tool_manager import InMemoryToolManager
    from modex_agent.core.session_id import SessionInfo

    hist = ListMessageHistory(messages)
    return AgentContext(
        system_prompt="", history=hist,
        tool_manager=InMemoryToolManager(), session=SessionInfo.from_str("s.x"),
        max_iterations=5,
    )


class TestLoopDetectionHookContent:
    @pytest.mark.asyncio
    async def test_content_loop_raises(self):
        msgs = [
            ChatMessage(role="assistant", content="I will check the file."),
            ChatMessage(role="assistant", content="I will check the file."),
            ChatMessage(role="assistant", content="I will check the file."),
            ChatMessage(role="assistant", content="I will check the file."),
        ]
        ctx = _ctx_with_history(msgs)
        hook = LoopDetectionHook(window_size=5, content_similarity_threshold=0.85)
        resp = LLMResponse(content="I will check the file.", finish_reason="stop")
        with pytest.raises(LoopDetectedError) as ei:
            await hook.after_llm_response(ctx, resp)
        assert ei.value.loop_type == "content"
        assert "<loop_detected" in ei.value.user_content

    @pytest.mark.asyncio
    async def test_skips_empty_content_messages(self):
        # 4 prior assistants with empty content (tool-only) + 1 empty response
        # must NOT trigger a content loop (empties excluded from content window)
        msgs = [ChatMessage(role="assistant", content=None) for _ in range(4)]
        ctx = _ctx_with_history(msgs)
        hook = LoopDetectionHook(window_size=3)
        resp = LLMResponse(content=None, finish_reason="stop")
        await hook.after_llm_response(ctx, resp)  # must not raise

    @pytest.mark.asyncio
    async def test_under_window_does_not_raise(self):
        msgs = [ChatMessage(role="assistant", content="same"), ChatMessage(role="assistant", content="same")]
        ctx = _ctx_with_history(msgs)
        hook = LoopDetectionHook(window_size=5)
        resp = LLMResponse(content="same", finish_reason="stop")
        await hook.after_llm_response(ctx, resp)  # only 3 < 5, no raise

    @pytest.mark.asyncio
    async def test_user_breaks_window(self):
        msgs = [
            ChatMessage(role="assistant", content="same"),
            ChatMessage(role="user", content="again"),
            ChatMessage(role="assistant", content="same"),
            ChatMessage(role="assistant", content="same"),
        ]
        ctx = _ctx_with_history(msgs)
        hook = LoopDetectionHook(window_size=5)
        resp = LLMResponse(content="same", finish_reason="stop")
        await hook.after_llm_response(ctx, resp)  # only 3 after user, no raise

    @pytest.mark.asyncio
    async def test_llm_error_response_skipped(self):
        ctx = _ctx_with_history([])
        hook = LoopDetectionHook(window_size=2)
        resp = LLMResponse(content="x", finish_reason="error", error="boom")
        await hook.after_llm_response(ctx, resp)  # no raise


class TestLoopDetectionHookTool:
    @pytest.mark.asyncio
    async def test_tool_loop_raises(self):
        tc = [{"id": "c", "type": "function", "function": {"name": "read", "arguments": '{"path": "/a"}'}}]
        msgs = [ChatMessage(role="assistant", content=None, tool_calls=tc) for _ in range(4)]
        ctx = _ctx_with_history(msgs)
        hook = LoopDetectionHook(window_size=5)
        resp = LLMResponse(
            content=None, finish_reason="tool_calls",
            tool_calls=[ToolCall(tool_name="read", arguments={"path": "/a"})],
        )
        with pytest.raises(LoopDetectedError) as ei:
            await hook.after_llm_response(ctx, resp)
        assert ei.value.loop_type == "tool"
        assert "read" in ei.value.user_content

    @pytest.mark.asyncio
    async def test_different_args_not_a_loop(self):
        msgs = [
            ChatMessage(role="assistant", content=None, tool_calls=[
                {"id": "1", "type": "function", "function": {"name": "read", "arguments": '{"path": "/a"}'}}]),
            ChatMessage(role="assistant", content=None, tool_calls=[
                {"id": "2", "type": "function", "function": {"name": "read", "arguments": '{"path": "/b"}'}}]),
        ]
        ctx = _ctx_with_history(msgs)
        hook = LoopDetectionHook(window_size=3)
        resp = LLMResponse(
            content=None, finish_reason="tool_calls",
            tool_calls=[ToolCall(tool_name="read", arguments={"path": "/c"})],
        )
        await hook.after_llm_response(ctx, resp)  # different args, no raise
