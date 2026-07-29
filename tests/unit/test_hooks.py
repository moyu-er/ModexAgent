"""Tests for HookRunner dispatching and error handling."""

import json
import logging

import pytest

from modex_agent.core.agent import AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import ToolResult
from modex_agent.core.types import LLMResponse, ToolCall
from modex_agent.hook import HookPoint, HookPayload, HookRunner, HookSpec, HookErrorPolicy
from modex_agent.hook.abc import (
    AfterIterationHook, AfterLLMResponseHook, AfterTurnHook,
    BeforeIterationHook, BeforeTurnHook, FinalizeContentHook,
)
from modex_agent.hook.builtin import RunLoggingHook

class BrokenHook(BeforeTurnHook, BeforeIterationHook, AfterIterationHook, AfterTurnHook, AfterLLMResponseHook):
    """Hook that raises in every async method."""

    @property
    def name(self) -> str:
        return "broken_hook"

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


class TestHookRunnerLogging:
    """HookRunner logs exceptions instead of silently swallowing."""

    @pytest.mark.asyncio
    async def test_exception_logged_not_swallowed(self, caplog):
        hooks = [HookSpec(hook=BrokenHook(), on_error=HookErrorPolicy.LOG)]
        runner = HookRunner(hooks)
        async_ctx = None

        with caplog.at_level(logging.DEBUG, logger="modex_agent.hook.runner"):
            await runner.dispatch(HookPoint.BEFORE_TURN, async_ctx)
            await runner.dispatch(HookPoint.BEFORE_ITERATION, async_ctx)
            await runner.dispatch(
                HookPoint.AFTER_LLM_RESPONSE,
                async_ctx,
                HookPayload(data={"response": LLMResponse(content="hello")}),
            )
            await runner.dispatch(HookPoint.AFTER_ITERATION, async_ctx)
            await runner.dispatch(HookPoint.AFTER_TURN, async_ctx, HookPayload(data={"result": None}))

        assert len(caplog.records) == 5
        for record in caplog.records:
            assert "BrokenHook" in record.message

    @pytest.mark.asyncio
    async def test_other_hooks_still_run_after_exception(self):
        calls: list[str] = []

        class TrackingHook(BeforeTurnHook):
            @property
            def name(self) -> str: return "tracking_hook"
            async def before_turn(self, ctx):
                calls.append("track")

        hooks = [
            HookSpec(hook=BrokenHook(), on_error=HookErrorPolicy.LOG),
            HookSpec(hook=TrackingHook(), on_error=HookErrorPolicy.LOG),
        ]
        runner = HookRunner(hooks)
        await runner.dispatch(HookPoint.BEFORE_TURN, None)
        assert calls == ["track"]

    def test_finalize_content_chains_through_all_hooks(self):
        class UpperHook(FinalizeContentHook):
            @property
            def name(self) -> str: return "upper_hook"
            def finalize_content(self, ctx, content):
                return content.upper() if content else content

        class PrefixHook(FinalizeContentHook):
            @property
            def name(self) -> str: return "prefix_hook"
            def finalize_content(self, ctx, content):
                return f"[{content}]" if content else content

        hooks = [
            HookSpec(hook=UpperHook(), on_error=HookErrorPolicy.LOG),
            HookSpec(hook=PrefixHook(), on_error=HookErrorPolicy.LOG),
        ]
        runner = HookRunner(hooks)
        result = runner.dispatch_finalize(None, "hello")
        assert result == "[HELLO]"

    def test_finalize_content_logs_errors(self, caplog):
        class BrokenFinalizeHook(FinalizeContentHook):
            @property
            def name(self) -> str: return "broken_finalize_hook"
            def finalize_content(self, ctx, content):
                raise RuntimeError("finalize boom")

        class GoodFinalizeHook(FinalizeContentHook):
            @property
            def name(self) -> str: return "good_finalize_hook"
            def finalize_content(self, ctx, content):
                return content + "!"

        hooks = [
            HookSpec(hook=BrokenFinalizeHook(), on_error=HookErrorPolicy.LOG),
            HookSpec(hook=GoodFinalizeHook(), on_error=HookErrorPolicy.LOG),
        ]
        runner = HookRunner(hooks)

        with caplog.at_level(logging.DEBUG, logger="modex_agent.hook.runner"):
            result = runner.dispatch_finalize(None, "hello")

        assert result == "hello!"
        assert any("finalize_content failed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_after_llm_response_delegates_to_all_hooks(self):
        calls: list[str] = []

        class TrackingHook(AfterLLMResponseHook):
            @property
            def name(self) -> str: return "tracking_hook"
            async def after_llm_response(self, ctx, response):
                calls.append(f"{ctx.session}:{response.content}")

        hooks = [
            HookSpec(hook=TrackingHook(), on_error=HookErrorPolicy.LOG),
        ]
        runner = HookRunner(hooks)
        ctx = AgentContext(
            system_prompt="",
            history=None,  # type: ignore[arg-type]
            tool_manager=None,  # type: ignore[arg-type]
            session=SessionInfo.from_str("session-1"),
        )

        await runner.dispatch(
            HookPoint.AFTER_LLM_RESPONSE,
            ctx,
            HookPayload(data={"response": LLMResponse(content="model text")}),
        )

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
            session=SessionInfo.from_str("chat-a"),
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
        assert any("[LLM]" in msg for msg in messages)
        assert any("session_id=chat-a" in msg for msg in messages)
        assert any("agent=" in msg for msg in messages)
        assert any("iter=" in msg for msg in messages)
        assert any("final answer" in msg for msg in messages)
        assert any("prompt_tokens" in msg for msg in messages)

    @pytest.mark.asyncio
    async def test_logs_tool_call_and_result_with_session_id_and_arguments(self, caplog):
        hook = RunLoggingHook(logger_name="tests.run_logging")
        ctx = AgentContext(
            system_prompt="",
            history=None,  # type: ignore[arg-type]
            tool_manager=None,  # type: ignore[arg-type]
            session=SessionInfo.from_str("chat-b"),
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
                [ToolResult.from_text("search", json.dumps({"temp": 21}), call_id="call-1")],
            )

        messages = [record.message for record in caplog.records]
        assert any("[TOOL_CALL]" in msg for msg in messages)
        assert any("[TOOL_RESULT]" in msg for msg in messages)
        assert any("session_id=chat-b" in msg for msg in messages)
        assert any("tool=search" in msg for msg in messages)
        assert any('"query": "weather"' in msg for msg in messages)
        assert any('\\"temp\\": 21' in msg for msg in messages)

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
            session=SessionInfo.from_str("chat-c"),
        )
        tool_call = ToolCall(
            tool_name="write",
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
                [ToolResult.from_text("write", "result1\nresult2\n" + "z" * 50, call_id="call-2")],
            )

        for record in caplog.records:
            lines = record.message.split("\n")
            assert len(lines) == 2, (
                f"Expected exactly 2 lines (tag line + content line), got {len(lines)}: {record.message!r}"
            )
            assert "\\n" not in lines[1], (
                f"Content line should have no literal \\n: {lines[1]!r}"
            )
        assert any("truncated" in record.message for record in caplog.records)
