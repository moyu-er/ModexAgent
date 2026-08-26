"""LoopDetectionHook helpers — pure function tests."""
import json

import pytest

from modex_agent.control.exceptions import LoopDetectedError
from modex_agent.core.message import ChatMessage
from modex_agent.core.types import LLMResponse, ToolCall
from modex_agent.hook.builtin.loop_detection import (
    LoopDetectionHook,
    _collect_recent_assistants,
    _similarity,
    _tool_calls_count,
    _tool_calls_fingerprint,
)


class TestSimilarity:
    def test_identical(self):
        assert _similarity("abc", "abc") == 1.0

    def test_both_empty(self):
        assert _similarity("", "") == 1.0

    def test_one_empty(self):
        assert _similarity("a", "") == 0.0

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

    def test_count_keeps_duplicates(self):
        # The fingerprint is a set (dedupes), but the count must reflect the
        # real number of calls so duplicate batches aren't seen as identical.
        dup = [ToolCall(tool_name="read", arguments={"path": "/a"}, call_id="c1"),
               ToolCall(tool_name="read", arguments={"path": "/a"}, call_id="c2")]
        single = [ToolCall(tool_name="read", arguments={"path": "/a"}, call_id="c1")]
        assert _tool_calls_fingerprint(dup) == _tool_calls_fingerprint(single)
        assert _tool_calls_count(dup) == 2
        assert _tool_calls_count(single) == 1
        assert _tool_calls_count(None) == 0


class TestCollectRecentAssistants:
    def _asst(self, content, tool_calls=None):
        return ChatMessage(role="assistant", content=content, tool_calls=tool_calls)

    def _tc(self, name="read", path="/a"):
        return [ToolCall(tool_name=name, arguments={"path": path}, call_id="c")]

    def test_stops_at_user(self):
        msgs = [
            self._asst("old1", self._tc()),            # previous turn
            ChatMessage(role="user", content="hi"),
            self._asst("a", self._tc()),
            self._asst("b", self._tc()),
        ]
        current = LLMResponse(content="c", finish_reason="stop")
        views = _collect_recent_assistants(msgs, current)
        assert [v.content for v in views] == ["a", "b", "c"]

    def test_ignores_tool_messages(self):
        msgs = [
            self._asst("a", self._tc()),
            ChatMessage(role="tool", content="result", tool_call_id="1", name="read"),
            self._asst("b", self._tc()),
        ]
        current = LLMResponse(content="c", finish_reason="stop")
        views = _collect_recent_assistants(msgs, current)
        assert [v.content for v in views] == ["a", "b", "c"]

    def test_stops_at_toolless_assistant(self):
        # A tool-less assistant step ends the tool-repeating run, just like a
        # user message — older tool-bearing assistants are excluded.
        msgs = [
            self._asst("older", self._tc()),   # tool-bearing, but before a tool-less step
            self._asst("no tools here"),        # tool-less -> breaks the backward scan
            self._asst("b", self._tc()),
        ]
        current = LLMResponse(content="c", finish_reason="stop")
        views = _collect_recent_assistants(msgs, current)
        assert [v.content for v in views] == ["b", "c"]

    def test_current_response_tool_calls_carried(self):
        msgs = [self._asst(None, [
            ToolCall(tool_name="read", arguments={"path": "/a"}, call_id="c1")
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
            ChatMessage(role="assistant", content="repeated old", tool_calls=self._tc()),
            ChatMessage(role="assistant", content="repeated old", tool_calls=self._tc()),
            ChatMessage(role="assistant", content="repeated old", tool_calls=self._tc()),
            ChatMessage(role="assistant", content="repeated old", tool_calls=self._tc()),
            ChatMessage(role="user", content="try again"),
            ChatMessage(role="assistant", content="new", tool_calls=self._tc()),
        ]
        current = LLMResponse(content="new", finish_reason="tool_calls",
                              tool_calls=[ToolCall(tool_name="read", arguments={"path": "/a"})])
        views = _collect_recent_assistants(msgs, current)
        # Only the current turn's assistants (after the user message) are kept.
        assert [v.content for v in views] == ["new", "new"]

    def test_mixed_tool_and_user_boundary(self):
        """Tool messages are skipped, and the user boundary still blocks older
        assistant messages even when tool messages are interleaved."""
        msgs = [
            ChatMessage(role="assistant", content="old", tool_calls=self._tc()),
            ChatMessage(role="user", content="retry"),
            ChatMessage(role="assistant", content="a", tool_calls=self._tc()),
            ChatMessage(role="tool", content="result", tool_call_id="1", name="read"),
            ChatMessage(role="assistant", content="b", tool_calls=self._tc()),
        ]
        current = LLMResponse(content="c", finish_reason="stop")
        views = _collect_recent_assistants(msgs, current)
        assert [v.content for v in views] == ["a", "b", "c"]


def _ctx_with_history(messages):
    """Build an AgentContext whose history returns the given messages."""
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.core.tool_manager import InMemoryToolManager
    from modex_agent.memory.history import ListMessageHistory

    hist = ListMessageHistory(messages)
    return AgentContext(
        system_prompt="", history=hist,
        tool_manager=InMemoryToolManager(), session=SessionInfo.from_str("s.x"),
        max_iterations=5,
    )


class TestLoopDetectionHookContent:
    @pytest.mark.asyncio
    async def test_content_and_tool_loop_raises(self):
        # A real loop: identical content AND the same tool call, N times.
        tc = [ToolCall(tool_name="read", arguments={"path": "/a"}, call_id="c")]
        msgs = [
            ChatMessage(role="assistant", content="I will check the file.", tool_calls=tc),
            ChatMessage(role="assistant", content="I will check the file.", tool_calls=tc),
            ChatMessage(role="assistant", content="I will check the file.", tool_calls=tc),
            ChatMessage(role="assistant", content="I will check the file.", tool_calls=tc),
        ]
        ctx = _ctx_with_history(msgs)
        hook = LoopDetectionHook(window_size=5, content_similarity_threshold=0.85)
        resp = LLMResponse(
            content="I will check the file.", finish_reason="tool_calls",
            tool_calls=[ToolCall(tool_name="read", arguments={"path": "/a"})],
        )
        with pytest.raises(LoopDetectedError) as ei:
            await hook.after_llm_response(ctx, resp)
        assert ei.value.loop_type == "tool"
        assert "<loop_detected" in ei.value.user_content
        # the combined notice surfaces both signals
        assert "read" in ei.value.user_content
        assert "I will check the file." in ei.value.user_content

    @pytest.mark.asyncio
    async def test_similar_content_but_no_tools_no_raise(self):
        # Content repeats but no tool calls — the AND conjunction is unsatisfied.
        msgs = [
            ChatMessage(role="assistant", content="I will check the file."),
            ChatMessage(role="assistant", content="I will check the file."),
            ChatMessage(role="assistant", content="I will check the file."),
            ChatMessage(role="assistant", content="I will check the file."),
        ]
        ctx = _ctx_with_history(msgs)
        hook = LoopDetectionHook(window_size=5, content_similarity_threshold=0.85)
        resp = LLMResponse(content="I will check the file.", finish_reason="stop")
        await hook.after_llm_response(ctx, resp)  # must not raise

    @pytest.mark.asyncio
    async def test_skips_empty_content_messages(self):
        # 4 prior assistants with empty content (tool-only) + 1 empty response
        # must NOT trigger: the AND conjunction needs non-empty content too.
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
    async def test_same_tool_but_different_content_no_raise(self):
        # Same tool/args repeated, but each step says something different —
        # the AND conjunction (content similarity) is unsatisfied.
        def tc(_i: int) -> list[ToolCall]:
            return [ToolCall(tool_name="read", arguments={"path": "/a"}, call_id="c")]
        msgs = [
            ChatMessage(role="assistant", content="looking at part one", tool_calls=tc(1)),
            ChatMessage(role="assistant", content="now checking section two", tool_calls=tc(2)),
            ChatMessage(role="assistant", content="inspecting the third area", tool_calls=tc(3)),
            ChatMessage(role="assistant", content="verifying the fourth spot", tool_calls=tc(4)),
        ]
        ctx = _ctx_with_history(msgs)
        hook = LoopDetectionHook(window_size=5, content_similarity_threshold=0.85)
        resp = LLMResponse(
            content="examining the fifth region", finish_reason="tool_calls",
            tool_calls=[ToolCall(tool_name="read", arguments={"path": "/a"})],
        )
        await hook.after_llm_response(ctx, resp)  # content differs, no raise

    @pytest.mark.asyncio
    async def test_different_args_not_a_loop(self):
        msgs = [
            ChatMessage(role="assistant", content=None, tool_calls=[
                ToolCall(tool_name="read", arguments={"path": "/a"}, call_id="1")]),
            ChatMessage(role="assistant", content=None, tool_calls=[
                ToolCall(tool_name="read", arguments={"path": "/b"}, call_id="2")]),
        ]
        ctx = _ctx_with_history(msgs)
        hook = LoopDetectionHook(window_size=3)
        resp = LLMResponse(
            content=None, finish_reason="tool_calls",
            tool_calls=[ToolCall(tool_name="read", arguments={"path": "/c"})],
        )
        await hook.after_llm_response(ctx, resp)  # different args, no raise
