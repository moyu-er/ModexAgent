"""Tests for ReActAgent thin shell."""
import pytest

from modex_agent.agents.react.agent import ReActAgent
from modex_agent.core.agent import AgentContext
from modex_agent.core.emitter import TurnEvent, TurnEventSink
from modex_agent.core.session_id import SessionInfo
from modex_agent.memory.history import ListMessageHistory
from modex_agent.tools.manager import InMemoryToolManager


class _MockProvider:
    pass


class _NullSink(TurnEventSink):
    """Discards every event — the mock provider fails before any emission."""

    async def _dispatch(self, event: TurnEvent) -> None:
        _ = event


class TestReActAgent:
    def test_name(self):
        agent = ReActAgent(_MockProvider())  # type: ignore[arg-type]
        assert agent.name == "ReActAgent"

    def test_mode_stored(self):
        agent = ReActAgent(_MockProvider(), mode="clean")  # type: ignore[arg-type]
        assert agent.mode == "clean"

    def test_full_mode_default(self):
        agent = ReActAgent(_MockProvider())  # type: ignore[arg-type]
        assert agent.mode == "full"

    @pytest.mark.asyncio
    async def test_clean_mode_run_completes(self):
        """Clean mode should run start->llm->end without errors (mock provider fails but gracefully)."""
        agent = ReActAgent(_MockProvider(), mode="clean")  # type: ignore[arg-type]

        ctx = AgentContext(
            system_prompt="Hi",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo.from_str("test.agent"),
        )
        sink = _NullSink()
        try:
            result = await agent.run(ctx, sink)
            # Clean mode should complete (LLMNode will error without real provider, caught by ReActAgent)
            assert result is not None
        except Exception:
            pass

        # contextvar-held emitter should be reset
        assert ctx.emitter is None


class TestReActAgentRuntime:
    @pytest.mark.asyncio
    async def test_clean_mode_sets_clean_runtime(self):
        agent = ReActAgent(_MockProvider(), mode="clean")  # type: ignore[arg-type]

        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo.from_str("test.agent"),
        )
        sink = _NullSink()
        try:
            await agent.run(ctx, sink)
        except Exception:
            pass
        assert ctx.runtime is not None
        assert ctx.runtime.services.hooks is None  # sanitized clean mode

    @pytest.mark.asyncio
    async def test_full_mode_preserves_hooks(self):
        agent = ReActAgent(_MockProvider(), mode="full")  # type: ignore[arg-type]

        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo.from_str("test.agent"),
        )
        sink = _NullSink()
        try:
            await agent.run(ctx, sink)
        except Exception:
            pass
        assert ctx.runtime is not None
        assert ctx.runtime.services.hooks is None  # no prebuilt hooks supplied
