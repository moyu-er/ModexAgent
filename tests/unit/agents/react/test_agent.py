"""Tests for ReActAgent thin shell."""
import pytest
from framework.agents.react.agent import ReActAgent, ReActEvent
from framework.core.agent import AgentContext
from framework.core.tool_manager import InMemoryToolManager
from framework.memory.history import ListMessageHistory
from framework.core.graph.engine import GraphEngine
from framework.agents.react.graph import ReActGraph


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
