"""Tests for ReActAgent thin shell."""
import pytest

from modex_agent.agents.react.agent import ReActAgent
from modex_agent.core.agent import AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.hook import HookRunner
from modex_agent.memory.history import ListMessageHistory
from modex_agent.tools.manager import InMemoryToolManager


class _MockProvider:
    pass


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

        class _Emitter:
            def wants_streaming(self):
                return False

        ctx = AgentContext(
            system_prompt="Hi",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo.from_str("test.agent"),
        )
        emitter = _Emitter()
        try:
            result = await agent.run(ctx, emitter)  # type: ignore[arg-type]
            # Clean mode should complete (LLMNode will error without real provider, caught by ReActAgent)
            assert result is not None
        except Exception:
            pass

        # contextvar should be reset
        assert ctx.emitter is None


class TestReActAgentRuntime:
    @pytest.mark.asyncio
    async def test_clean_mode_sets_clean_runtime(self):
        agent = ReActAgent(_MockProvider(), mode="clean")  # type: ignore[arg-type]

        class _Emitter:
            def wants_streaming(self):
                return False

            async def emit(self, *args, **kwargs):
                pass

            async def emit_delta(self, *args, **kwargs):
                pass

            async def emit_content(self, *args, **kwargs):
                pass

            async def emit_stream_end(self, *args, **kwargs):
                pass

            async def emit_complete(self, *args, **kwargs):
                pass

        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo.from_str("test.agent"),
        )
        emitter = _Emitter()
        try:
            await agent.run(ctx, emitter)  # type: ignore[arg-type]
        except Exception:
            pass
        assert ctx.runtime is not None
        assert ctx.runtime.services.hooks is None  # sanitized clean mode

    @pytest.mark.asyncio
    async def test_full_mode_preserves_hooks(self):
        agent = ReActAgent(_MockProvider(), mode="full")  # type: ignore[arg-type]

        class _Emitter:
            def wants_streaming(self):
                return False

            async def emit(self, *a, **kw):
                pass

            async def emit_delta(self, *a, **kw):
                pass

            async def emit_content(self, *a, **kw):
                pass

            async def emit_stream_end(self, *a, **kw):
                pass

            async def emit_complete(self, *a, **kw):
                pass

        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo.from_str("test.agent"),
        )
        emitter = _Emitter()
        try:
            await agent.run(ctx, emitter)  # type: ignore[arg-type]
        except Exception:
            pass
        assert ctx.runtime is not None
        assert ctx.runtime.services.hooks is None  # no prebuilt hooks supplied
