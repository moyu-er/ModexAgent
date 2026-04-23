"""Tests for CompositeRunHook exception logging."""

import logging

import pytest

from framework.core.hooks import AgentRunHook, CompositeRunHook


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


class TestCompositeRunHookLogging:
    """P1-3: CompositeRunHook logs exceptions instead of silently swallowing."""

    @pytest.mark.asyncio
    async def test_exception_logged_not_swallowed(self, caplog):
        hook = CompositeRunHook([BrokenHook()])
        async_ctx = None  # BrokenHook doesn't use ctx

        with caplog.at_level(logging.DEBUG, logger="framework.core.hooks"):
            await hook.before_turn(async_ctx)
            await hook.before_iteration(async_ctx)
            await hook.after_iteration(async_ctx)
            await hook.after_turn(async_ctx, None)

        assert len(caplog.records) == 4
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
