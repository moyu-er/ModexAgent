"""Tests for CompositeRunHook exception logging."""

import logging

import pytest

from framework.core.agent import AgentContext
from framework.core.hooks import AgentRunHook, CompositeRunHook, RunLoggingHook
from framework.core.tool_manager import ToolResult
from framework.core.types import LLMResponse, ToolCall


class BrokenHook(AgentRunHook):
    """Hook that raises in every method."""

    async def before_turn(self, ctx):
        raise RuntimeError("before_turn boom")

    async def before_iteration(self, ctx):
        raise RuntimeError("before_iteration boom")

    async def after_iteration(self, ctx):
        raise RuntimeError("after_iteration boom")

    async def after_turn(self, ctx, result):
        raise RuntimeError("after_turn boom")

    async def after_llm_response(self, ctx, response):
        raise RuntimeError("after_llm_response boom")


class TestCompositeRunHookLogging:
    """P1-3: CompositeRunHook logs exceptions instead of silently swallowing."""

    @pytest.mark.asyncio
    async def test_exception_logged_not_swallowed(self, caplog):
        hook = CompositeRunHook([BrokenHook()])
        async_ctx = None  # BrokenHook doesn't use ctx

        with caplog.at_level(logging.DEBUG, logger="framework.core.hooks"):
            await hook.before_turn(async_ctx)
            await hook.before_iteration(async_ctx)
            await hook.after_llm_response(async_ctx, LLMResponse(content="hello"))
            await hook.after_iteration(async_ctx)
            await hook.after_turn(async_ctx, None)

        assert len(caplog.records) == 5
        for record in caplog.records:
            assert "failed" in record.message
            assert "BrokenHook" in record.message

    @pytest.mark.asyncio
    async def test_other_hooks_still_run_after_exception(self):
        calls: list[str] = []

        class TrackingHook(AgentRunHook):
            async def before_turn(self, ctx):
                calls.append("track")

        hook = CompositeRunHook([BrokenHook(), TrackingHook()])
        await hook.before_turn(None)
        assert calls == ["track"]

    def test_finalize_content_delegates_to_all_hooks(self):
        """P1: CompositeRunHook.finalize_content chains through all hooks."""

        class UpperHook(AgentRunHook):
            def finalize_content(self, ctx, content):
                return content.upper() if content else content

        class PrefixHook(AgentRunHook):
            def finalize_content(self, ctx, content):
                return f"[{content}]" if content else content

        hook = CompositeRunHook([UpperHook(), PrefixHook()])
        result = hook.finalize_content(None, "hello")
        # Upper first → "HELLO", then Prefix → "[HELLO]"
        assert result == "[HELLO]"

    def test_finalize_content_logs_errors(self, caplog):
        """P1: finalize_content logs exceptions and continues."""

        class BrokenFinalizeHook(AgentRunHook):
            def finalize_content(self, ctx, content):
                raise RuntimeError("finalize boom")

        class GoodFinalizeHook(AgentRunHook):
            def finalize_content(self, ctx, content):
                return content + "!"

        hook = CompositeRunHook([BrokenFinalizeHook(), GoodFinalizeHook()])

        with caplog.at_level(logging.DEBUG, logger="framework.core.hooks"):
            result = hook.finalize_content(None, "hello")

        assert result == "hello!"
        assert any("finalize_content failed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_after_llm_response_delegates_to_all_hooks(self):
        calls: list[str] = []

        class TrackingHook(AgentRunHook):
            async def after_llm_response(self, ctx, response):
                calls.append(f"{ctx.session_id}:{response.content}")

        hook = CompositeRunHook([TrackingHook()])
        ctx = AgentContext(
            system_prompt="",
            history=None,  # type: ignore[arg-type]
            tool_manager=None,  # type: ignore[arg-type]
            session_id="session-1",
        )

        await hook.after_llm_response(ctx, LLMResponse(content="model text"))

        assert calls == ["session-1:model text"]


class TestRunLoggingHook:
    """RunLoggingHook emits detailed per-session LLM/tool logs."""

    @pytest.mark.asyncio
    async def test_logs_llm_output_with_session_id(self, caplog):
        hook = RunLoggingHook(logger_name="tests.run_logging")
        ctx = AgentContext(
            system_prompt="",
            history=None,  # type: ignore[arg-type]
            tool_manager=None,  # type: ignore[arg-type]
            session_id="chat-a",
        )

        with caplog.at_level(logging.INFO, logger="tests.run_logging"):
            await hook.after_llm_response(
                ctx,
                LLMResponse(
                    content="final answer",
                    reasoning_content="short reasoning",
                    usage={"prompt_tokens": 3, "completion_tokens": 2},
                ),
            )

        messages = [record.message for record in caplog.records]
        assert any("session_id=chat-a" in msg for msg in messages)
        assert any("LLM response" in msg and "final answer" in msg for msg in messages)
        assert any("short reasoning" in msg for msg in messages)
        assert any("prompt_tokens" in msg for msg in messages)

    @pytest.mark.asyncio
    async def test_logs_tool_call_and_result_with_session_id_and_arguments(self, caplog):
        hook = RunLoggingHook(logger_name="tests.run_logging")
        ctx = AgentContext(
            system_prompt="",
            history=None,  # type: ignore[arg-type]
            tool_manager=None,  # type: ignore[arg-type]
            session_id="chat-b",
        )
        tool_call = ToolCall(
            tool_name="search",
            arguments={"query": "weather"},
            call_id="call-1",
        )

        with caplog.at_level(logging.INFO, logger="tests.run_logging"):
            await hook.before_tool_execution(ctx, [tool_call])
            await hook.after_tool_execution(
                ctx,
                [ToolResult(tool_name="search", result={"temp": 21}, call_id="call-1")],
            )

        messages = [record.message for record in caplog.records]
        assert any("Tool call start" in msg for msg in messages)
        assert any("session_id=chat-b" in msg for msg in messages)
        assert any("tool=search" in msg for msg in messages)
        assert any('"query": "weather"' in msg for msg in messages)
        assert any("Tool call end" in msg and '"temp": 21' in msg for msg in messages)

    @pytest.mark.asyncio
    async def test_collapses_newlines_and_truncates_long_content(self, caplog):
        hook = RunLoggingHook(
            logger_name="tests.run_logging",
            max_content_chars=24,
            max_result_chars=24,
        )
        ctx = AgentContext(
            system_prompt="",
            history=None,  # type: ignore[arg-type]
            tool_manager=None,  # type: ignore[arg-type]
            session_id="chat-c",
        )
        tool_call = ToolCall(
            tool_name="write_file",
            arguments={"content": "line1\nline2\nline3\n" + "x" * 50},
            call_id="call-2",
        )

        with caplog.at_level(logging.INFO, logger="tests.run_logging"):
            await hook.after_llm_response(
                ctx,
                LLMResponse(content="answer line1\nanswer line2\n" + "y" * 50),
            )
            await hook.before_tool_execution(ctx, [tool_call])
            await hook.after_tool_execution(
                ctx,
                [ToolResult(tool_name="write_file", result="result1\nresult2\n" + "z" * 50, call_id="call-2")],
            )

        for record in caplog.records:
            assert "\n" not in record.message
            assert "\\n" not in record.message
        assert any("truncated" in record.message for record in caplog.records)
