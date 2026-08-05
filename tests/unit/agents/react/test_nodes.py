# ruff: noqa: ANN401
"""Tests for ReAct nodes."""

from __future__ import annotations

from typing import Any

import pytest

from modex_agent import ToolCall
from modex_agent.agents.react.constants import ReActNode
from modex_agent.agents.react.context import ReActGraphContext
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
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager, ToolResult
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_graph import (
    GraphPersistenceCoordinator,
    NullDeliverStoreFactory,
    NullGraphInstanceStore,
    NullNodeStateStore,
)
from modex_graph.constants import GraphNode


class _AutoRegCoord(GraphPersistenceCoordinator):
    """Test-only coordinator that auto-registers nodes on begin_invocation."""

    def collect_consumable_delivers(
        self, node_name: str, invocation_id: int
    ) -> list[Any]:
        if self.get_deliver_store(node_name) is None:
            self.register_node(node_name)
        return super().collect_consumable_delivers(node_name, invocation_id)
    def route_deliver(
        self,
        target_node: str,
        content: Any,
        source_node: str,
        source_invocation_id: int,
    ) -> int | None:
        if target_node != GraphNode.END and self.get_deliver_store(target_node) is None:
            self.register_node(target_node)
        return super().route_deliver(target_node, content, source_node, source_invocation_id)



def _make_test_coordinator() -> _AutoRegCoord:
    return _AutoRegCoord(
        graph_instance_id=0,
        instance_store=NullGraphInstanceStore(),
        node_state_store=NullNodeStateStore(0),
        default_deliver_store_factory=NullDeliverStoreFactory(),
    )


def _make_state() -> ReActTurnState:
    return ReActTurnState(
        identity=TurnIdentity(agent_id="test", session=SessionInfo.from_str("s1"), turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )


def _make_runtime() -> AgentRuntime:
    state = _make_state()
    runtime = AgentRuntime(services=AgentRuntimeServices(), state=state)
    # ``runtime.graph_runtime`` is kept set for backward-compat with code
    # paths that still reach the AgentRuntime directly (governance in
    # ``LLMNode._build_messages``). A no-op ``ReactGraphRuntime()`` matches
    # the previous behavior (no services wired).
    runtime.graph_runtime = ReactGraphRuntime()
    return runtime


def _make_graph_ctx(
    runtime: AgentRuntime | None = None,
    state: ReActTurnState | None = None,
) -> ReActGraphContext:
    """Build a ``ReActGraphContext`` for direct node-invocation tests.

    Constructs a minimal ``AgentContext`` wrapping the runtime + state, then
    wraps it in a ``ReActGraphContext`` with a no-op ``ReactGraphRuntime``.
    """
    if runtime is None:
        runtime = _make_runtime()
    if state is None:
        state = runtime.state  # type: ignore[assignment] — ReActTurnState at runtime
    agent_ctx = AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        identity=state.identity,
        runtime=runtime,
        session=SessionInfo.from_str("test.agent"),
    )
    graph_runtime = ReactGraphRuntime()
    return ReActGraphContext(
        state=state,
        runtime=graph_runtime,
        user_data=agent_ctx,
        coordinator=_make_test_coordinator(),
    )


def _make_llm_client() -> ReactLlmClient:
    """A ReactLlmClient whose provider is unused (call() is stubbed per-test)."""
    return ReactLlmClient(provider=object())  # type: ignore[arg-type]


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
        ctx = _make_graph_ctx(runtime=runtime)
        ctx.agent_ctx.emitter = _MockEmitter()  # type: ignore[assignment]

        await node.run(ctx)
        assert node._submit_result == {ReActNode.LLM: [None]}
        assert ctx.state.iteration == 0

    @pytest.mark.asyncio
    async def test_resume_routes_to_tool(self):
        node = StartNode()
        runtime = _make_runtime()
        runtime.state.resume_target = ReActNode.TOOL
        ctx = _make_graph_ctx(runtime=runtime)

        await node.run(ctx)
        assert node._submit_result == {ReActNode.TOOL: [None]}
        assert ctx.state.resume_target is None

    @pytest.mark.asyncio
    async def test_resume_target_is_not_approval_specific(self):
        node = StartNode()
        runtime = _make_runtime()
        runtime.state.resume_target = ReActNode.LLM
        ctx = _make_graph_ctx(runtime=runtime)

        await node.run(ctx)
        assert node._submit_result == {ReActNode.LLM: [None]}
        assert ctx.state.resume_target is None


class TestEndNode:
    @pytest.mark.asyncio
    async def test_writes_result_to_state(self):
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
        ctx = _make_graph_ctx(runtime=runtime)
        ctx.agent_ctx.emitter = _MockEmitter()  # type: ignore[assignment]

        await node.run(ctx)
        assert GraphNode.END in node._submit_result
        assert ctx.state.result is not None
        assert ctx.state.result.content == "Done!"

    @pytest.mark.asyncio
    async def test_max_iterations_writes_fallback_result(self):
        node = EndNode()
        runtime = _make_runtime()
        ctx = _make_graph_ctx(runtime=runtime)
        ctx.agent_ctx.emitter = _MockEmitter()  # type: ignore[assignment]

        await node.run(ctx)
        assert GraphNode.END in node._submit_result
        assert ctx.state.result is not None
        assert ctx.state.result.content == "max iterations reached"
        assert ctx.state.result.stop_reason == "max_iterations"

    @pytest.mark.asyncio
    async def test_turn_cancelled_writes_cancelled_result(self):
        node = EndNode()
        runtime = _make_runtime()
        runtime.state.phase = TurnPhase.CANCELLED
        ctx = _make_graph_ctx(runtime=runtime)
        ctx.agent_ctx.emitter = _MockEmitter()  # type: ignore[assignment]

        await node.run(ctx)
        assert GraphNode.END in node._submit_result
        assert ctx.state.result is not None
        assert ctx.state.result.stop_reason == "turn_cancelled"


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
        llm_client.call = _mock_call  # type: ignore[method-assign]
        node = LLMNode(llm_client, InjectionDrainer())

        runtime = _make_runtime()
        ctx = _make_graph_ctx(runtime=runtime)
        ctx.agent_ctx.emitter = _MockEmitter()  # type: ignore[assignment]
        ctx.agent_ctx.history = _MockHistory()  # type: ignore[assignment]

        await node.run(ctx)
        assert ReActNode.TOOL in node._submit_result

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
        llm_client.call = _mock_call  # type: ignore[method-assign]
        node = LLMNode(llm_client, InjectionDrainer())

        runtime = _make_runtime()
        ctx = _make_graph_ctx(runtime=runtime)
        ctx.agent_ctx.emitter = _MockEmitter()  # type: ignore[assignment]
        ctx.agent_ctx.history = _MockHistory()  # type: ignore[assignment]

        await node.run(ctx)
        assert ReActNode.END in node._submit_result

    @pytest.mark.asyncio
    async def test_routes_to_end_on_max_iterations(self):
        llm_client = _make_llm_client()
        node = LLMNode(llm_client, InjectionDrainer())

        runtime = _make_runtime()
        runtime.state.iteration = 5
        ctx = _make_graph_ctx(runtime=runtime)
        ctx.agent_ctx.emitter = _MockEmitter()  # type: ignore[assignment]
        ctx.agent_ctx.max_iterations = 5

        await node.run(ctx)
        assert node._submit_result == {ReActNode.END: [None]}

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
        llm_client.call = _mock_call  # type: ignore[method-assign]
        node = LLMNode(llm_client, InjectionDrainer())

        runtime = _make_runtime()
        ctx = _make_graph_ctx(runtime=runtime)
        ctx.agent_ctx.emitter = _MockEmitter()  # type: ignore[assignment]
        ctx.agent_ctx.history = _MockHistory()  # type: ignore[assignment]

        await node.run(ctx)
        assert ReActNode.END in node._submit_result


class TestToolNode:
    @pytest.mark.asyncio
    async def test_execute_batch_all_allowed(self):
        executed: list[str] = []

        tool_executor = ToolExecutor()

        async def _mock_execute(tc, ctx):
            executed.append(tc.tool_name)
            return ToolResult.from_text(tc.tool_name, f"ok_{tc.tool_name}")

        tool_executor.execute = _mock_execute  # type: ignore[method-assign]
        node = ToolNode(tool_executor)

        history = _MockHistory()
        tc1 = ToolCall(tool_name="search", arguments={}, call_id="c1")
        tc2 = ToolCall(tool_name="read", arguments={}, call_id="c2")
        response = type("_MockResponse", (), {"tool_calls": [tc1, tc2]})()

        runtime = _make_runtime()
        runtime.state.llm_response = response  # type: ignore[assignment]
        runtime.state.iteration = 1
        ctx = _make_graph_ctx(runtime=runtime)
        ctx.agent_ctx.emitter = _MockEmitter()  # type: ignore[assignment]
        ctx.agent_ctx.history = history  # type: ignore[assignment]

        await node.run(ctx)
        assert ReActNode.LLM in node._submit_result
        assert len(executed) == 2
        assert len(history.msgs) == 2

    @pytest.mark.asyncio
    async def test_denied_tool_cascades_and_cancels(self):
        executed: list[str] = []

        tool_executor = ToolExecutor()

        async def _mock_execute(tc, ctx):
            executed.append(tc.tool_name)
            return ToolResult.from_text(tc.tool_name, "ok")

        tool_executor.execute = _mock_execute  # type: ignore[method-assign]
        node = ToolNode(tool_executor)

        history = _MockHistory()
        tc1 = ToolCall(tool_name="t1", arguments={}, call_id="c1")
        tc2 = ToolCall(tool_name="t2", arguments={}, call_id="c2")
        response = type("_MockResponse", (), {"tool_calls": [tc1, tc2]})()

        from modex_agent.approval.runtime import ApprovalRuntime
        from modex_agent.runtime.enums import ApprovalDenyPolicy

        runtime = _make_runtime()
        runtime.state.iteration = 1
        runtime.state.llm_response = response  # type: ignore[assignment]
        runtime.services.approval = ApprovalRuntime(
            classifier=type(
                "_Cls",
                (),
                {"classify": lambda s, tc, c: "normal" if tc.tool_name == "t1" else "hardline"},
            )(),  # type: ignore[arg-type]
            default_deny_policy=ApprovalDenyPolicy.CANCEL_TURN,
        )
        ctx = _make_graph_ctx(runtime=runtime)
        ctx.agent_ctx.emitter = _MockEmitter()  # type: ignore[assignment]
        ctx.agent_ctx.history = history  # type: ignore[assignment]

        await node.run(ctx)

        # Atomic batch: one DENIED cascades to PREEMPT the ALLOWED call,
        # nothing executes, and CANCEL_TURN routes the turn to END.
        assert node._submit_result == {ReActNode.END: [None]}
        assert len(executed) == 0

    @pytest.mark.asyncio
    async def test_denied_tool_cancel_path_uses_real_tool_executor(self):
        # DENIED decisions never reach the executor, so a real ToolExecutor
        # with an unused provider is sufficient to exercise the cancel path.
        tool_executor = ToolExecutor()
        node = ToolNode(tool_executor)

        tc = ToolCall(tool_name="write", arguments={"path": "/tmp/x"}, call_id="c1")
        response = type("_MockResponse", (), {"tool_calls": [tc]})()

        from modex_agent.approval.runtime import ApprovalRuntime
        from modex_agent.runtime.enums import ApprovalDenyPolicy

        runtime = _make_runtime()
        runtime.state.iteration = 1
        runtime.state.llm_response = response  # type: ignore[assignment]
        runtime.services.approval = ApprovalRuntime(
            classifier=type("_Cls", (), {"classify": lambda s, tc, c: "hardline"})(),  # type: ignore[arg-type]
            default_deny_policy=ApprovalDenyPolicy.CANCEL_TURN,
        )
        ctx = _make_graph_ctx(runtime=runtime)
        ctx.agent_ctx.emitter = _MockEmitter()  # type: ignore[assignment]
        ctx.agent_ctx.history = _MockHistory()  # type: ignore[assignment]

        await node.run(ctx)

        assert node._submit_result == {ReActNode.END: [None]}

    @pytest.mark.asyncio
    async def test_exceeds_max_tools_routes_to_end(self):
        tool_executor = ToolExecutor()
        node = ToolNode(tool_executor)

        tc_list = [ToolCall(tool_name=f"t{i}", arguments={}, call_id=f"c{i}") for i in range(5)]
        response = type("_MockResponse", (), {"tool_calls": tc_list})()

        runtime = _make_runtime()
        runtime.state.llm_response = response  # type: ignore[assignment]
        runtime.state.custom[TurnCustomKey.MAX_TOOLS_PER_TURN] = 3
        ctx = _make_graph_ctx(runtime=runtime)
        ctx.agent_ctx.emitter = _MockEmitter()  # type: ignore[assignment]
        ctx.agent_ctx.history = _MockHistory()  # type: ignore[assignment]

        await node.run(ctx)
        assert node._submit_result == {ReActNode.END: [None]}

    @pytest.mark.asyncio
    async def test_classify_all_returns_allowed_for_normal_tools(self):
        tool_executor = ToolExecutor()
        node = ToolNode(tool_executor)

        tool_calls = [
            ToolCall(tool_name="search", arguments={}),
            ToolCall(tool_name="read", arguments={}),
        ]
        agent_ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo.from_str("test.agent"),
        )

        decisions = node._classify_all(tool_calls, agent_ctx)
        assert decisions == [ApprovalDecision.ALLOWED, ApprovalDecision.ALLOWED]
