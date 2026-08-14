"""Tests for HookErrorPolicy — ignore, log, abort behaviour."""

from __future__ import annotations

import pytest

from modex_agent.control.exceptions import PolicyViolation
from modex_agent.hook import HookErrorPolicy, HookPoint, HookPayload, HookRunner, HookSpec
from modex_agent.hook.abc import BeforeIterationHook, BeforeGraphHook


class BrokenHook(BeforeGraphHook, BeforeIterationHook):
    @property
    def name(self) -> str:
        return "broken_hook"

    async def before_graph(self, ctx):
        raise RuntimeError("boom")

    async def before_iteration(self, ctx):
        raise RuntimeError("boom")


class GoodHook(BeforeGraphHook):
    @property
    def name(self) -> str:
        return "good_hook"

    async def before_graph(self, ctx):
        pass


class TestHookErrorPolicyIgnore:
    """IGNORE: exception is silently swallowed, subsequent hooks run."""

    @pytest.mark.asyncio
    async def test_ignore_swallows_exception(self):
        runner = HookRunner([
            HookSpec(hook=BrokenHook(), on_error=HookErrorPolicy.IGNORE),
        ])
        # Should not raise
        result = await runner.dispatch(HookPoint.BEFORE_GRAPH, None)
        assert result is None

    @pytest.mark.asyncio
    async def test_ignore_allows_subsequent_hooks(self):
        calls: list[str] = []

        class TrackingHook(BeforeGraphHook):
            @property
            def name(self) -> str: return "tracking_hook"
            async def before_graph(self, ctx):
                calls.append("track")

        runner = HookRunner([
            HookSpec(hook=BrokenHook(), on_error=HookErrorPolicy.IGNORE),
            HookSpec(hook=TrackingHook(), on_error=HookErrorPolicy.IGNORE),
        ])
        await runner.dispatch(HookPoint.BEFORE_GRAPH, None)
        assert calls == ["track"]


class TestHookErrorPolicyLog:
    """LOG: exception is logged but not raised, subsequent hooks run."""

    @pytest.mark.asyncio
    async def test_log_does_not_raise(self, caplog):
        import logging

        runner = HookRunner([
            HookSpec(hook=BrokenHook(), on_error=HookErrorPolicy.LOG),
        ])
        with caplog.at_level(logging.WARNING, logger="modex_agent.hook.runner"):
            result = await runner.dispatch(HookPoint.BEFORE_GRAPH, None)
        assert result is None
        assert any("broken_hook" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_log_allows_subsequent_hooks(self):
        calls: list[str] = []

        class TrackingHook(BeforeGraphHook):
            @property
            def name(self) -> str: return "tracking_hook"
            async def before_graph(self, ctx):
                calls.append("track")

        runner = HookRunner([
            HookSpec(hook=BrokenHook(), on_error=HookErrorPolicy.LOG),
            HookSpec(hook=TrackingHook(), on_error=HookErrorPolicy.LOG),
        ])
        await runner.dispatch(HookPoint.BEFORE_GRAPH, None)
        assert calls == ["track"]


class TestHookErrorPolicyAbort:
    """ABORT: exception is converted to PolicyViolation and raised."""

    @pytest.mark.asyncio
    async def test_abort_raises_policy_violation(self):
        runner = HookRunner([
            HookSpec(hook=BrokenHook(), on_error=HookErrorPolicy.ABORT),
        ])
        with pytest.raises(PolicyViolation):
            await runner.dispatch(HookPoint.BEFORE_GRAPH, None)

    @pytest.mark.asyncio
    async def test_abort_stops_subsequent_hooks(self):
        calls: list[str] = []

        class TrackingHook(BeforeGraphHook):
            @property
            def name(self) -> str: return "tracking_hook"
            async def before_graph(self, ctx):
                calls.append("track")

        runner = HookRunner([
            HookSpec(hook=BrokenHook(), on_error=HookErrorPolicy.ABORT),
            HookSpec(hook=TrackingHook(), on_error=HookErrorPolicy.ABORT),
        ])
        with pytest.raises(PolicyViolation):
            await runner.dispatch(HookPoint.BEFORE_GRAPH, None)
        assert calls == []

    @pytest.mark.asyncio
    async def test_abort_distinguishes_timeout_vs_error(self):
        import asyncio

        class SlowHook(BeforeGraphHook):
            @property
            def name(self) -> str: return "slow_hook"
            async def before_graph(self, ctx):
                await asyncio.sleep(100)

        runner = HookRunner([
            HookSpec(hook=SlowHook(), on_error=HookErrorPolicy.ABORT),
        ])
        with pytest.raises(PolicyViolation) as exc_info:
            await runner.dispatch(
                HookPoint.BEFORE_GRAPH, None, hook_timeout=0.01
            )
        assert "timeout" in str(exc_info.value)


class TestHookErrorPolicyMixed:
    """Mixed policies in the same runner."""

    @pytest.mark.asyncio
    async def test_mixed_policies(self, caplog):
        import logging

        calls: list[str] = []

        class TrackingHook(BeforeGraphHook):
            @property
            def name(self) -> str: return "tracking_hook"
            async def before_graph(self, ctx):
                calls.append("track")

        runner = HookRunner([
            HookSpec(hook=BrokenHook(), on_error=HookErrorPolicy.IGNORE),
            HookSpec(hook=BrokenHook(), on_error=HookErrorPolicy.LOG),
            HookSpec(hook=TrackingHook(), on_error=HookErrorPolicy.ABORT),
        ])
        with caplog.at_level(logging.WARNING, logger="modex_agent.hook.runner"):
            await runner.dispatch(HookPoint.BEFORE_GRAPH, None)
        assert calls == ["track"]
        # LOG policy should have produced a record
        assert any("broken_hook" in r.message for r in caplog.records)
