"""Tests for ReAct nodes."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from framework.agents.react.agent import ReActAgent
from framework.agents.react.constants import ReActMetaKey, ReActNode, ReActReason
from framework.agents.react.nodes.end import EndNode
from framework.agents.react.nodes.llm import LLMNode
from framework.agents.react.nodes.start import StartNode
from framework.agents.react.nodes.tool import ToolNode
from framework.agents.react.state import TurnResumeState
from framework.approval.constants import ApprovalDecision
from framework.core.agent import AgentContext
from framework.core.constants import FinishReason
from framework.core.context_extensions import ExtensionKey
from framework.core.emitter import ToolCall, ToolResult
from framework.core.graph.constants import GraphMetaKey, GraphNode
from framework.core.tool_manager import InMemoryToolManager
from framework.memory.history import ListMessageHistory


class _MockEmitter:
    def __init__(self):
        self.events: list = []

    async def emit(self, event, data=None):
        self.events.append((event, data))

    async def emit_complete(self, result):
        self.events.append(("complete", result))

    async def emit_delta(self, delta):
        pass

    async def emit_content(self, content):
        pass

    async def emit_stream_end(self, resuming=False):
        pass

    def wants_streaming(self):
        return False


class _MockHistory:
    def __init__(self):
        self.msgs: list = []

    async def append(self, msg):
        self.msgs.append(msg)

    async def to_list(self):
        return list(self.msgs)


class TestStartNode:
    @pytest.mark.asyncio
    async def test_normal_start_routes_to_llm(self):
        node = StartNode()
        ctx = AgentContext(
            system_prompt="test", history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
        )
        ctx.emitter = _MockEmitter()

        t = await node.execute(ctx)
        assert t.target == ReActNode.LLM
        assert t.reason == ReActReason.NORMAL_START
        assert ctx.metadata[ReActMetaKey.ITERATION] == 0

    @pytest.mark.asyncio
    async def test_resume_routes_to_tool(self):
        node = StartNode()
        resume = TurnResumeState(
            iteration=3, tool_calls=[], tool_decisions=["allowed"],
            all_new_messages=[],
        )
        ctx = AgentContext(
            system_prompt="test", history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            metadata={ReActMetaKey.RESUME_STATE: resume},
        )
        ctx.emitter = _MockEmitter()

        t = await node.execute(ctx)
        assert t.target == ReActNode.TOOL
        assert t.reason == ReActReason.RESUME_TOOLS
        assert ctx.metadata[ReActMetaKey.ITERATION] == 3

    @pytest.mark.asyncio
    async def test_resume_target_is_not_approval_specific(self):
        node = StartNode()
        resume = TurnResumeState(
            iteration=3, tool_calls=[], tool_decisions=[],
            all_new_messages=[], resume_node="llm", resume_reason="resume_llm",
        )
        ctx = AgentContext(
            system_prompt="test", history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            metadata={ReActMetaKey.RESUME_STATE: resume},
        )
        ctx.emitter = _MockEmitter()

        t = await node.execute(ctx)
        assert t.target == "llm"
        assert t.reason == "resume_llm"
        assert ReActMetaKey.LLM_RESPONSE not in ctx.metadata


class TestEndNode:
    @pytest.mark.asyncio
    async def test_writes_result_to_metadata(self):
        async def _mock_clear_checkpoint(self, ctx):
            pass

        agent = type("_MockAgent", (), {
            "_clear_checkpoint": _mock_clear_checkpoint,
        })()
        node = EndNode(agent)
        llm_resp = type("_MockResponse", (), {
            "content": "Done!", "reasoning_content": None,
            "tool_calls": [], "finish_reason": "stop",
        })()
        ctx = AgentContext(
            system_prompt="test", history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            metadata={
                ReActMetaKey.LLM_RESPONSE: llm_resp,
                ReActMetaKey.ITERATION_MSGS: [],
            },
        )
        ctx.emitter = _MockEmitter()

        t = await node.execute(ctx)
        assert t.target == GraphNode.END
        result = ctx.metadata[GraphMetaKey.GRAPH_RESULT]
        assert result.content == "Done!"

    @pytest.mark.asyncio
    async def test_max_iterations_writes_fallback_result(self):
        async def _mock_clear_checkpoint(self, ctx):
            pass

        agent = type("_MockAgent", (), {
            "_clear_checkpoint": _mock_clear_checkpoint,
        })()
        node = EndNode(agent)
        ctx = AgentContext(
            system_prompt="test", history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            metadata={ReActMetaKey.ITERATION_MSGS: []},
        )
        ctx.emitter = _MockEmitter()

        t = await node.execute(ctx)
        assert t.target == GraphNode.END
        result = ctx.metadata[GraphMetaKey.GRAPH_RESULT]
        assert result.content == "max iterations reached"
        assert result.stop_reason == "max_iterations"

    @pytest.mark.asyncio
    async def test_turn_cancelled_writes_cancelled_result(self):
        async def _mock_clear_checkpoint(self, ctx):
            pass

        agent = type("_MockAgent", (), {
            "_clear_checkpoint": _mock_clear_checkpoint,
        })()
        node = EndNode(agent)
        ctx = AgentContext(
            system_prompt="test", history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            metadata={
                ReActMetaKey.END_REASON: ReActReason.TURN_CANCELLED,
                ReActMetaKey.ITERATION_MSGS: [],
            },
        )
        ctx.emitter = _MockEmitter()

        t = await node.execute(ctx)
        assert t.target == GraphNode.END
        result = ctx.metadata[GraphMetaKey.GRAPH_RESULT]
        assert result.content == "turn cancelled"
        assert result.stop_reason == "turn_cancelled"
        assert result.error is None


class TestLLMNode:
    @pytest.mark.asyncio
    async def test_routes_to_tool_on_has_tool_calls(self):
        async def _mock_llm(messages, ctx):
            return type("_MockResponse", (), {
                "content": None, "reasoning_content": None,
                "tool_calls": [ToolCall(tool_name="search", arguments={})],
                "finish_reason": "stop",
            })()

        agent = type("_MockAgent", (), {
            "provider": type("_MockProvider", (), {})(),
            "_build_assistant_message": lambda self, content, tool_calls: {
                "role": "assistant",
            },
            "_call_hooks": lambda self, *a, **kw: None,
            "_drain_injections": lambda self, ctx, max_per_phase=3: [],
            "_save_checkpoint": AsyncMock(return_value=None),
        })()
        node = LLMNode(agent, enable_hooks=False)
        node._call_llm = _mock_llm

        ctx = AgentContext(
            system_prompt="test", history=_MockHistory(),
            tool_manager=InMemoryToolManager(),
            metadata={ReActMetaKey.ITERATION: 0},
        )
        ctx.emitter = _MockEmitter()

        t = await node.execute(ctx)
        assert t.target == ReActNode.TOOL
        assert t.reason == ReActReason.HAS_TOOLS

    @pytest.mark.asyncio
    async def test_routes_to_end_on_no_tool_calls(self):
        async def _mock_llm(messages, ctx):
            return type("_MockResponse", (), {
                "content": "Hello!", "reasoning_content": None,
                "tool_calls": [], "finish_reason": "stop",
            })()

        agent = type("_MockAgent", (), {
            "provider": type("_MockProvider", (), {})(),
            "_build_assistant_message": lambda self, content, tool_calls: {
                "role": "assistant", "content": "Hello!",
            },
            "_call_hooks": lambda self, *a, **kw: None,
            "_drain_injections": lambda self, ctx, max_per_phase=3: [],
            "_save_checkpoint": AsyncMock(return_value=None),
        })()
        node = LLMNode(agent, enable_hooks=False)
        node._call_llm = _mock_llm

        ctx = AgentContext(
            system_prompt="test", history=_MockHistory(),
            tool_manager=InMemoryToolManager(),
            metadata={ReActMetaKey.ITERATION: 0},
        )
        ctx.emitter = _MockEmitter()

        t = await node.execute(ctx)
        assert t.target == ReActNode.END
        assert t.reason == ReActReason.NO_TOOLS

    @pytest.mark.asyncio
    async def test_routes_to_end_on_max_iterations(self):
        agent = type("_MockAgent", (), {
            "_call_hooks": lambda self, *a, **kw: None,
            "_drain_injections": lambda self, ctx, max_per_phase=3: [],
        })()
        node = LLMNode(agent, enable_hooks=False)

        ctx = AgentContext(
            system_prompt="test", history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            max_iterations=5, metadata={ReActMetaKey.ITERATION: 5},
        )
        ctx.emitter = _MockEmitter()

        t = await node.execute(ctx)
        assert t.target == ReActNode.END
        assert t.reason == ReActReason.MAX_ITERATIONS

    @pytest.mark.asyncio
    async def test_routes_to_end_on_llm_error(self):
        async def _mock_llm(messages, ctx):
            return type("_MockResponse", (), {
                "content": "API Error", "reasoning_content": None,
                "tool_calls": [], "finish_reason": FinishReason.ERROR.value,
            })()

        agent = type("_MockAgent", (), {
            "provider": type("_MockProvider", (), {})(),
            "_build_assistant_message": lambda self, content, tool_calls: {
                "role": "assistant",
            },
            "_call_hooks": lambda self, *a, **kw: None,
            "_drain_injections": lambda self, ctx, max_per_phase=3: [],
        })()
        node = LLMNode(agent, enable_hooks=False)
        node._call_llm = _mock_llm

        ctx = AgentContext(
            system_prompt="test", history=_MockHistory(),
            tool_manager=InMemoryToolManager(),
            metadata={ReActMetaKey.ITERATION: 0},
        )
        ctx.emitter = _MockEmitter()

        t = await node.execute(ctx)
        assert t.target == ReActNode.END
        assert t.reason == ReActReason.LLM_ERROR


class TestToolNode:
    @pytest.mark.asyncio
    async def test_execute_batch_all_allowed(self):
        executed: list[str] = []

        class _MockAgent:
            async def _execute_tool(self, tc, ctx):
                executed.append(tc.tool_name)
                return ToolResult(tool_name=tc.tool_name, result=f"ok_{tc.tool_name}")

            def _build_tool_message(self, result, call_id):
                return {
                    "role": "tool",
                    "tool_call_id": call_id or result.tool_name,
                    "name": result.tool_name,
                    "content": str(result.result) if result.result else str(result.error),
                }

            async def _call_hooks(self, *a, **kw):
                pass

            async def _drain_injections(self, ctx, max_per_phase=3):
                return []

            async def _save_checkpoint(self, msgs, ctx):
                pass

        agent = _MockAgent()
        node = ToolNode(agent, enable_approval=False, enable_hooks=False)

        history = _MockHistory()
        tc1 = ToolCall(tool_name="search", arguments={}, call_id="c1")
        tc2 = ToolCall(tool_name="read", arguments={}, call_id="c2")
        response = type("_MockResponse", (), {"tool_calls": [tc1, tc2]})()

        ctx = AgentContext(
            system_prompt="test", history=history,
            tool_manager=InMemoryToolManager(),
            metadata={
                ReActMetaKey.LLM_RESPONSE: response,
                ReActMetaKey.ITERATION: 1,
            },
        )
        ctx.emitter = _MockEmitter()

        t = await node.execute(ctx)
        assert t.target == ReActNode.LLM
        assert t.reason == ReActReason.TOOLS_DONE
        assert len(executed) == 2
        assert len(history.msgs) == 2

    @pytest.mark.asyncio
    async def test_denied_tool_cascades_and_cancels(self):
        executed: list[str] = []

        class _MockAgent:
            async def _execute_tool(self, tc, ctx):
                executed.append(tc.tool_name)
                return ToolResult(tool_name=tc.tool_name, result="ok")

            def _build_tool_message(self, result, call_id):
                return {
                    "role": "tool",
                    "tool_call_id": call_id or result.tool_name,
                    "name": result.tool_name,
                    "content": str(result.result) if result.result else str(result.error),
                }

            async def _call_hooks(self, *a, **kw):
                pass

            async def _drain_injections(self, ctx, max_per_phase=3):
                return []

            async def _save_checkpoint(self, msgs, ctx):
                pass

            async def _save_denial_checkpoint(self, all_messages, ctx):
                pass

        agent = _MockAgent()
        node = ToolNode(agent, enable_approval=False, enable_hooks=False)

        history = _MockHistory()
        tc1 = ToolCall(tool_name="t1", arguments={}, call_id="c1")
        tc2 = ToolCall(tool_name="t2", arguments={}, call_id="c2")

        response = type("_MockResponse", (), {"tool_calls": [tc1, tc2]})()

        ctx = AgentContext(
            system_prompt="test", history=history,
            tool_manager=InMemoryToolManager(),
            metadata={
                ReActMetaKey.LLM_RESPONSE: response,
                ReActMetaKey.ITERATION: 1,
                ReActMetaKey.DENY_AS_CANCEL: True,
            },
        )
        ctx.emitter = _MockEmitter()

        t = await node._execute_batch(
            [tc1, tc2],
            [ApprovalDecision.ALLOWED, ApprovalDecision.DENIED],
            ctx,
        )

        assert t.target == ReActNode.END
        assert t.reason == ReActReason.TURN_CANCELLED
        # Only t1 should execute; t2 is DENIED
        assert len(executed) == 1

    @pytest.mark.asyncio
    async def test_denied_tool_cancel_path_uses_real_agent_checkpoint_signature(self):
        agent = ReActAgent(provider=object(), mode="clean")
        node = ToolNode(agent, enable_approval=False, enable_hooks=False)

        tc = ToolCall(tool_name="write_file", arguments={"path": "/tmp/x"}, call_id="c1")
        ctx = AgentContext(
            system_prompt="test",
            history=_MockHistory(),
            tool_manager=InMemoryToolManager(),
            metadata={
                ReActMetaKey.ITERATION: 1,
                ReActMetaKey.ITERATION_MSGS: [],
                ReActMetaKey.DENY_AS_CANCEL: True,
            },
        )
        ctx.emitter = _MockEmitter()

        transition = await node._execute_batch([tc], [ApprovalDecision.DENIED], ctx)

        assert transition.target == ReActNode.END
        assert transition.reason == ReActReason.TURN_CANCELLED

    @pytest.mark.asyncio
    async def test_exceeds_max_tools_routes_to_end(self):
        class _MockAgent:
            async def _call_hooks(self, *a, **kw):
                pass

            async def _drain_injections(self, ctx, max_per_phase=3):
                return []

        agent = _MockAgent()
        node = ToolNode(agent, enable_approval=False, enable_hooks=False)

        tc_list = [
            ToolCall(tool_name=f"t{i}", arguments={}, call_id=f"c{i}")
            for i in range(5)
        ]
        response = type("_MockResponse", (), {"tool_calls": tc_list})()

        ctx = AgentContext(
            system_prompt="test", history=_MockHistory(),
            tool_manager=InMemoryToolManager(),
            extensions={ExtensionKey.MAX_TOOLS_PER_TURN: 3},
            metadata={
                ReActMetaKey.LLM_RESPONSE: response,
                ReActMetaKey.ITERATION: 1,
            },
        )
        ctx.emitter = _MockEmitter()

        t = await node.execute(ctx)
        assert t.target == ReActNode.END
        assert t.reason == ReActReason.TURN_CANCELLED

    @pytest.mark.asyncio
    async def test_classify_all_returns_allowed_for_normal_tools(self):
        class _MockAgent:
            pass

        agent = _MockAgent()
        node = ToolNode(agent, enable_approval=False, enable_hooks=False)

        tool_calls = [
            ToolCall(tool_name="search", arguments={}),
            ToolCall(tool_name="read", arguments={}),
        ]
        ctx = AgentContext(
            system_prompt="test", history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
        )

        decisions = node._classify_all(tool_calls, ctx)
        assert decisions == [ApprovalDecision.ALLOWED, ApprovalDecision.ALLOWED]
