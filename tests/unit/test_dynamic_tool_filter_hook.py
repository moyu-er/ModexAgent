"""Tests for DynamicToolFilterHook — token threshold and error readonly."""

from unittest.mock import MagicMock

from framework.core.agent import AgentContext
from framework.hook.builtin.dynamic_tool_filter import DynamicToolFilterHook
from framework.memory.history import ListMessageHistory
from framework.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from framework.runtime.models import TurnIdentity, TurnStateBase
from framework.runtime.services import AgentRuntime, AgentRuntimeServices


def _ctx(token_usage: dict | None = None, consecutive_errors: int = 0) -> AgentContext:
    state = TurnStateBase(
        identity=TurnIdentity(agent_id="test", session_id="s1", turn_id="t1"),
        agent_kind=AgentKind.REACT, phase=TurnPhase.RUNNING,
    )
    if token_usage:
        state.custom[TurnCustomKey.TOOL_USAGE] = token_usage
    if consecutive_errors:
        state.custom[TurnCustomKey.CONSECUTIVE_ERRORS] = consecutive_errors
    runtime = AgentRuntime(services=AgentRuntimeServices(), state=state)
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory([]),
        tool_manager=MagicMock(),
        session_id="s1",
        runtime=runtime,
    )


class TestDynamicToolFilterHook:
    async def test_token_threshold_denies_tools(self):
        base = MagicMock()
        hook = DynamicToolFilterHook(
            base=base,
            token_thresholds={100: {"shell"}, 200: {"shell", "write_file"}},
        )
        ctx = _ctx(token_usage={"total_tokens": 150})

        await hook.before_iteration(ctx)
        # At 150 tokens, tool_manager is replaced with FilteredToolManager
        assert ctx.tool_manager is not base
        assert ctx.runtime.state.custom.get(TurnCustomKey.DYNAMIC_TOOL_ACTIVE) is True
        denied = ctx.runtime.state.custom.get(TurnCustomKey.DYNAMIC_TOOL_DENIED, set())
        assert "shell" in denied

    async def test_error_threshold_readonly(self):
        base = MagicMock()
        hook = DynamicToolFilterHook(base=base, error_readonly_threshold=3)
        ctx = _ctx(consecutive_errors=5)

        await hook.before_iteration(ctx)
        assert ctx.tool_manager is not base
        denied = ctx.runtime.state.custom.get(TurnCustomKey.DYNAMIC_TOOL_DENIED, set())
        assert "write_file" in denied or "shell" in denied
        assert ctx.runtime.state.custom.get(TurnCustomKey.DYNAMIC_TOOL_ACTIVE) is True

    async def test_restores_base_after_iteration(self):
        base = MagicMock()
        hook = DynamicToolFilterHook(base=base, token_thresholds={10: {"shell"}})
        ctx = _ctx(token_usage={"total_tokens": 100})

        await hook.before_iteration(ctx)
        assert ctx.tool_manager is not base

        await hook.after_iteration(ctx)
        assert ctx.tool_manager is base
        assert ctx.runtime.state.custom.get(TurnCustomKey.DYNAMIC_TOOL_ACTIVE) is None

    async def test_no_changes_when_no_thresholds_met(self):
        base = MagicMock()
        hook = DynamicToolFilterHook(base=base)
        ctx = _ctx()

        await hook.before_iteration(ctx)
        assert ctx.tool_manager is base
        assert ctx.runtime.state.custom.get(TurnCustomKey.DYNAMIC_TOOL_ACTIVE) is None
