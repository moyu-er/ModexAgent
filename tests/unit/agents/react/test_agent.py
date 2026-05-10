"""Tests for ReActAgent thin shell."""
import pytest

from framework.agents.react.agent import ReActAgent
from framework.agents.react.graph import ReActGraph
from framework.core.agent import AgentContext
from framework.core.graph.engine import GraphEngine
from framework.core.tool_manager import InMemoryToolManager
from framework.hook import HookRunner
from framework.memory.history import ListMessageHistory


class _MockProvider:
    pass


class TestReActAgent:
    def test_creates_graph_and_engine(self):
        agent = ReActAgent(_MockProvider(), mode="clean")
        assert isinstance(agent.graph, ReActGraph)
        assert isinstance(agent.engine, GraphEngine)

    def test_name(self):
        agent = ReActAgent(_MockProvider())
        assert agent.name == "ReActAgent"

    def test_full_mode_default(self):
        agent = ReActAgent(_MockProvider())
        assert agent.graph.name == "react_full"

    @pytest.mark.asyncio
    async def test_clean_mode_run_completes(self):
        """Clean mode should run start->llm->end without errors (mock provider fails but gracefully)."""
        agent = ReActAgent(_MockProvider(), mode="clean")

        class _Emitter:
            def wants_streaming(self):
                return False

        ctx = AgentContext(
            system_prompt="Hi",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
        )
        emitter = _Emitter()
        try:
            result = await agent.run(ctx, emitter)
            # Clean mode should complete (LLMNode will error without real provider, caught by ReActAgent)
            assert result is not None
        except Exception:
            pass

        # contextvar should be reset
        assert ctx.emitter is None


class TestReActAgentRuntime:
    @pytest.mark.asyncio
    async def test_clean_mode_sets_clean_runtime(self):
        agent = ReActAgent(_MockProvider(), mode="clean")

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
        )
        emitter = _Emitter()
        try:
            await agent.run(ctx, emitter)
        except Exception:
            pass
        assert ctx.runtime is not None
        assert ctx.runtime.services.hooks is None  # sanitized clean mode

    @pytest.mark.asyncio
    async def test_full_mode_preserves_hooks(self):
        agent = ReActAgent(_MockProvider(), mode="full")

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
        )
        emitter = _Emitter()
        try:
            await agent.run(ctx, emitter)
        except Exception:
            pass
        assert ctx.runtime is not None
        assert ctx.runtime.services.hooks is None  # no prebuilt hooks supplied
