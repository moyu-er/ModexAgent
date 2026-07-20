"""Tests for ReAct nodes."""

from __future__ import annotations

import pytest

from modex_agent import ToolCall
from modex_agent.agents.react.constants import ReActNode, ReActReason
from modex_agent.agents.react.injection_drainer import InjectionDrainer
from modex_agent.agents.react.llm_client import ReactLlmClient
from modex_agent.agents.react.nodes.end import EndNode
from modex_agent.agents.react.nodes.llm import LLMNode
from modex_agent.agents.react.nodes.start import StartNode
from modex_agent.agents.react.nodes.tool import ToolNode
from modex_agent.agents.react.runtime import ReactGraphRuntime
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.agents.react.tool_executor import ToolExecutor
from modex_agent.approval.constants import ApprovalDecision
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import FinishReason
from modex_agent.core.graph.constants import GraphNode
from modex_agent.core.tool_manager import InMemoryToolManager, ToolResult
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.core.session_id import SessionInfo


def _make_runtime() -> AgentRuntime:
    state = ReActTurnState(
        identity=TurnIdentity(agent_id="test", session=SessionInfo.from_str("s1"), turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    runtime = AgentRuntime(services=AgentRuntimeServices(), state=state)
    # Ticket 04: nodes route AOP through ``runtime.graph_runtime``. Tests that
    # bypass ``ReActAgent.run()`` must set it themselves; a no-op
    # ``ReactGraphRuntime()`` matches the previous behavior (no services wired).
    runtime.graph_runtime = ReactGraphRuntime()
    return runtime


def _make_llm_client() -> ReactLlmClient:
    """A ReactLlmClient whose provider is unused (call() is stubbed per-test)."""
    return ReactLlmClient(provider=object())


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
        runtime = _make_runtime()
        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            identity=runtime.state.identity,
            runtime=runtime,
            session=SessionInfo.from_str("test.agent"),
        )
        ctx.emitter = _MockEmitter()

        t = await node.execute(ctx)
        assert t.target == ReActNode.LLM
        assert t.reason == ReActReason.NORMAL_START
        assert runtime.state.iteration == 0

    @pytest.mark.asyncio
    async def test_resume_routes_to_tool(self):
        node = StartNode()
        runtime = _make_runtime()
        runtime.state.phase = TurnPhase.SUSPENDED
        runtime.state.current_node = ReActNode.TOOL
        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            identity=runtime.state.identity,
            runtime=runtime,
            session=SessionInfo.from_str("test.agent"),
        )
        ctx.emitter = _MockEmitter()

        t = await node.execute(ctx)
        assert t.target == ReActNode.TOOL
        assert t.reason == ReActReason.RESUME_TOOLS

    @pytest.mark.asyncio
    async def test_resume_target_is_not_approval_specific(self):
        node = StartNode()
        runtime = _make_runtime()
        runtime.state.phase = TurnPhase.SUSPENDED
        runtime.state.current_node = ReActNode.LLM
        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            identity=runtime.state.identity,
            runtime=runtime,
            session=SessionInfo.from_str("test.agent"),
        )
        ctx.emitter = _MockEmitter()

        t = await node.execute(ctx)
        assert t.target == ReActNode.LLM
        assert t.reason == ReActReason.RESUME_TOOLS


class TestEndNode:
    @pytest.mark.asyncio
    async def test_writes_result_to_metadata(self):
        node = EndNode()
        runtime = _make_runtime()
        runtime.state.llm_response = type(
            "_MockResponse",
            (),
            {
                "content": "Done!",
                "reasoning_content": None,
                "tool_calls": [],
                "finish_reason": "stop",
            },
        )()
        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            identity=runtime.state.identity,
            runtime=runtime,
            session=SessionInfo.from_str("test.agent"),
        )
        ctx.emitter = _MockEmitter()

        t = await node.execute(ctx)
        assert t.target == GraphNode.END
        result = ctx.runtime.state.custom[TurnCustomKey.GRAPH_RESULT]
        assert result.content == "Done!"

    @pytest.mark.asyncio
    async def test_max_iterations_writes_fallback_result(self):
        node = EndNode()
        runtime = _make_runtime()
        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            identity=runtime.state.identity,
            runtime=runtime,
            session=SessionInfo.from_str("test.agent"),
        )
        ctx.emitter = _MockEmitter()

        t = await node.execute(ctx)
        assert t.target == GraphNode.END
        result = ctx.runtime.state.custom[TurnCustomKey.GRAPH_RESULT]
        assert result.content == "max iterations reached"
        assert result.stop_reason == "max_iterations"

    @pytest.mark.asyncio
    async def test_turn_cancelled_writes_cancelled_result(self):
        node = EndNode()
        runtime = _make_runtime()
        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            identity=runtime.state.identity,
            runtime=runtime,
            session=SessionInfo.from_str("test.agent"),
        )
        ctx.emitter = _MockEmitter()

        t = await node.execute(ctx)
        assert t.target == GraphNode.END
        result = ctx.runtime.state.custom[TurnCustomKey.GRAPH_RESULT]
        assert result.content == "max iterations reached"


class TestLLMNode:
    @pytest.mark.asyncio
    async def test_routes_to_tool_on_has_tool_calls(self):
        async def _mock_call(messages, ctx):
            return type(
                "_MockResponse",
                (),
                {
                    "content": None,
                    "reasoning_content": None,
                    "tool_calls": [ToolCall(tool_name="search", arguments={})],
                    "finish_reason": "stop",
                },
            )()

        llm_client = _make_llm_client()
        llm_client.call = _mock_call
        node = LLMNode(llm_client, InjectionDrainer())

        runtime = _make_runtime()
        ctx = AgentContext(
            system_prompt="test",
            history=_MockHistory(),
            tool_manager=InMemoryToolManager(),
            identity=runtime.state.identity,
            runtime=runtime,
            session=SessionInfo.from_str("test.agent"),
        )
        ctx.emitter = _MockEmitter()

        t = await node.execute(ctx)
        assert t.target == ReActNode.TOOL
        assert t.reason == ReActReason.HAS_TOOLS

    @pytest.mark.asyncio
    async def test_routes_to_end_on_no_tool_calls(self):
        async def _mock_call(messages, ctx):
            return type(
                "_MockResponse",
                (),
                {
                    "content": "Hello!",
                    "reasoning_content": None,
                    "tool_calls": [],
                    "finish_reason": "stop",
                },
            )()

        llm_client = _make_llm_client()
        llm_client.call = _mock_call
        node = LLMNode(llm_client, InjectionDrainer())

        runtime = _make_runtime()
        ctx = AgentContext(
            system_prompt="test",
            history=_MockHistory(),
            tool_manager=InMemoryToolManager(),
            identity=runtime.state.identity,
            runtime=runtime,
            session=SessionInfo.from_str("test.agent"),
        )
        ctx.emitter = _MockEmitter()

        t = await node.execute(ctx)
        assert t.target == ReActNode.END
        assert t.reason == ReActReason.NO_TOOLS

    @pytest.mark.asyncio
    async def test_routes_to_end_on_max_iterations(self):
        llm_client = _make_llm_client()
        node = LLMNode(llm_client, InjectionDrainer())

        runtime = _make_runtime()
        runtime.state.iteration = 5
        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo.from_str("test.agent"),
            max_iterations=5,
            identity=runtime.state.identity,
            runtime=runtime,
        )
        ctx.emitter = _MockEmitter()

        t = await node.execute(ctx)
        assert t.target == ReActNode.END
        assert t.reason == ReActReason.MAX_ITERATIONS

    @pytest.mark.asyncio
    async def test_routes_to_end_on_llm_error(self):
        async def _mock_call(messages, ctx):
            return type(
                "_MockResponse",
                (),
                {
                    "content": "API Error",
                    "reasoning_content": None,
                    "tool_calls": [],
                    "finish_reason": FinishReason.ERROR.value,
                },
            )()

        llm_client = _make_llm_client()
        llm_client.call = _mock_call
        node = LLMNode(llm_client, InjectionDrainer())

        runtime = _make_runtime()
        ctx = AgentContext(
            system_prompt="test",
            history=_MockHistory(),
            tool_manager=InMemoryToolManager(),
            identity=runtime.state.identity,
            runtime=runtime,
            session=SessionInfo.from_str("test.agent"),
        )
        ctx.emitter = _MockEmitter()

        t = await node.execute(ctx)
        assert t.target == ReActNode.END
        assert t.reason == ReActReason.LLM_ERROR


class TestToolNode:
    @pytest.mark.asyncio
    async def test_execute_batch_all_allowed(self):
        executed: list[str] = []

        tool_executor = ToolExecutor(default_tool_timeout=30.0)

        async def _mock_execute(tc, ctx):
            executed.append(tc.tool_name)
            return ToolResult(tool_name=tc.tool_name, result=f"ok_{tc.tool_name}")

        tool_executor.execute = _mock_execute  # type: ignore[method-assign]
        node = ToolNode(tool_executor)

        history = _MockHistory()
        tc1 = ToolCall(tool_name="search", arguments={}, call_id="c1")
        tc2 = ToolCall(tool_name="read", arguments={}, call_id="c2")
        response = type("_MockResponse", (), {"tool_calls": [tc1, tc2]})()

        runtime = _make_runtime()
        runtime.state.llm_response = response
        runtime.state.iteration = 1
        ctx = AgentContext(
            system_prompt="test",
            history=history,
            tool_manager=InMemoryToolManager(),
            identity=runtime.state.identity,
            runtime=runtime,
            session=SessionInfo.from_str("test.agent"),
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

        tool_executor = ToolExecutor(default_tool_timeout=30.0)

        async def _mock_execute(tc, ctx):
            executed.append(tc.tool_name)
            return ToolResult(tool_name=tc.tool_name, result="ok")

        tool_executor.execute = _mock_execute  # type: ignore[method-assign]
        node = ToolNode(tool_executor)

        history = _MockHistory()
        tc1 = ToolCall(tool_name="t1", arguments={}, call_id="c1")
        tc2 = ToolCall(tool_name="t2", arguments={}, call_id="c2")

        runtime = _make_runtime()
        runtime.state.iteration = 1
        ctx = AgentContext(
            system_prompt="test",
            history=history,
            tool_manager=InMemoryToolManager(),
            identity=runtime.state.identity,
            runtime=runtime,
            session=SessionInfo.from_str("test.agent"),
        )

        from modex_agent.runtime.enums import ApprovalDenyPolicy
        from modex_agent.agents.react.approval import ApprovalRuntime

        ctx.runtime.services.approval = ApprovalRuntime(
            classifier=type("_Cls", (), {"classify": lambda s, tc, c: "normal"})(),
            default_deny_policy=ApprovalDenyPolicy.CANCEL_TURN,
        )
        ctx.emitter = _MockEmitter()

        t = await node._execute_batch(
            [tc1, tc2],
            [ApprovalDecision.ALLOWED, ApprovalDecision.DENIED],
            ctx,
        )

        assert t.target == ReActNode.END
        assert t.reason == ReActReason.TURN_CANCELLED
        assert (
            len(executed) == 0
        )  # atomic batch: ALLOWED converted to PREEMPTED when any DENIED present

    @pytest.mark.asyncio
    async def test_denied_tool_cancel_path_uses_real_tool_executor(self):
        # DENIED decisions never reach the executor, so a real ToolExecutor
        # with an unused provider is sufficient to exercise the cancel path.
        tool_executor = ToolExecutor(default_tool_timeout=30.0)
        node = ToolNode(tool_executor)

        tc = ToolCall(tool_name="write", arguments={"path": "/tmp/x"}, call_id="c1")
        runtime = _make_runtime()
        runtime.state.iteration = 1
        from modex_agent.agents.react.approval import ApprovalRuntime
        from modex_agent.runtime.enums import ApprovalDenyPolicy

        runtime.services.approval = ApprovalRuntime(
            classifier=type("_Cls", (), {"classify": lambda s, tc, c: "normal"})(),
            default_deny_policy=ApprovalDenyPolicy.CANCEL_TURN,
        )
        ctx = AgentContext(
            system_prompt="test",
            history=_MockHistory(),
            tool_manager=InMemoryToolManager(),
            identity=runtime.state.identity,
            runtime=runtime,
            session=SessionInfo.from_str("test.agent"),
        )
        ctx.emitter = _MockEmitter()

        transition = await node._execute_batch([tc], [ApprovalDecision.DENIED], ctx)

        assert transition.target == ReActNode.END
        assert transition.reason == ReActReason.TURN_CANCELLED

    @pytest.mark.asyncio
    async def test_exceeds_max_tools_routes_to_end(self):
        tool_executor = ToolExecutor(default_tool_timeout=30.0)
        node = ToolNode(tool_executor)

        tc_list = [ToolCall(tool_name=f"t{i}", arguments={}, call_id=f"c{i}") for i in range(5)]
        response = type("_MockResponse", (), {"tool_calls": tc_list})()

        runtime = _make_runtime()
        runtime.state.llm_response = response
        runtime.state.custom[TurnCustomKey.MAX_TOOLS_PER_TURN] = 3
        ctx = AgentContext(
            system_prompt="test",
            history=_MockHistory(),
            tool_manager=InMemoryToolManager(),
            identity=runtime.state.identity,
            runtime=runtime,
            session=SessionInfo.from_str("test.agent"),
        )
        ctx.emitter = _MockEmitter()

        t = await node.execute(ctx)
        assert t.target == ReActNode.END
        assert t.reason == ReActReason.TURN_CANCELLED

    @pytest.mark.asyncio
    async def test_classify_all_returns_allowed_for_normal_tools(self):
        tool_executor = ToolExecutor(default_tool_timeout=30.0)
        node = ToolNode(tool_executor)

        tool_calls = [
            ToolCall(tool_name="search", arguments={}),
            ToolCall(tool_name="read", arguments={}),
        ]
        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo.from_str("test.agent"),
        )

        decisions = node._classify_all(tool_calls, ctx)
        assert decisions == [ApprovalDecision.ALLOWED, ApprovalDecision.ALLOWED]
