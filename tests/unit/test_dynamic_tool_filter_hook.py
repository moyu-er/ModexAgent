"""Tests for DynamicToolFilterHook — token threshold and error readonly."""

from unittest.mock import MagicMock

from framework.core.agent import AgentContext
from framework.hook.builtin.dynamic_tool_filter import DynamicToolFilterHook
from framework.memory.history import ListMessageHistory


def _ctx(meta: dict | None = None) -> AgentContext:
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory([]),
        tool_manager=MagicMock(),
        session_id="s1",
        metadata=meta or {},
    )


class TestDynamicToolFilterHook:
    async def test_token_threshold_denies_tools(self):
        base = MagicMock()
        hook = DynamicToolFilterHook(
            base=base,
            token_thresholds={100: {"shell"}, 200: {"shell", "write_file"}},
        )
        ctx = _ctx({"usage": {"total_tokens": 150}})

        await hook.before_iteration(ctx)
        # At 150 tokens, tool_manager is replaced with FilteredToolManager
        assert ctx.tool_manager is not base
        assert ctx.metadata.get("_dynamic_tool_active") is True
        denied = ctx.metadata.get("_dynamic_tool_denied", set())
        assert "shell" in denied

    async def test_error_threshold_readonly(self):
        base = MagicMock()
        hook = DynamicToolFilterHook(base=base, error_readonly_threshold=3)
        ctx = _ctx({"consecutive_errors": 5})

        await hook.before_iteration(ctx)
        assert ctx.tool_manager is not base
        denied = ctx.metadata.get("_dynamic_tool_denied", set())
        assert "write_file" in denied or "shell" in denied
        assert ctx.metadata.get("_dynamic_tool_active") is True

    async def test_restores_base_after_iteration(self):
        base = MagicMock()
        hook = DynamicToolFilterHook(base=base, token_thresholds={10: {"shell"}})
        ctx = _ctx({"usage": {"total_tokens": 100}})

        await hook.before_iteration(ctx)
        assert ctx.tool_manager is not base

        await hook.after_iteration(ctx)
        assert ctx.tool_manager is base
        assert ctx.metadata.get("_dynamic_tool_active") is None

    async def test_no_changes_when_no_thresholds_met(self):
        base = MagicMock()
        hook = DynamicToolFilterHook(base=base)
        ctx = _ctx()

        await hook.before_iteration(ctx)
        assert ctx.tool_manager is base
        assert ctx.metadata.get("_dynamic_tool_active") is None
