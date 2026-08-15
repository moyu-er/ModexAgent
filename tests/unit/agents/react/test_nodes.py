# ruff: noqa: ANN001, ANN201, ANN202, ANN204, ANN401
"""Tests for ReAct nodes."""

from __future__ import annotations

import pytest

from modex_agent import ToolCall
from modex_agent.agents.react.agent import ReActEvent
from modex_agent.agents.react.constants import ReActNode
from modex_agent.agents.react.context import ReActGraphContext
from modex_agent.agents.react.injection_drainer import InjectionDrainer
from modex_agent.agents.react.llm_client import ReactLlmClient
from modex_agent.agents.react.nodes.end import EndNode
from modex_agent.agents.react.nodes.llm import LLMNode
from modex_agent.agents.react.nodes.start import StartNode
from modex_agent.agents.react.nodes.tool import ToolNode
from modex_agent.agents.react.runtime import ReactGraphRuntime
from modex_agent.agents.react.tool_executor import ToolExecutor
from modex_agent.approval.constants import ApprovalDecision
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import FinishReason, StopReason
from modex_agent.core.emitter import AgentResult
from modex_agent.core.message import ChatMessage
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager, ToolResult
from modex_agent.core.types import MessageRole
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import TurnCustomKey, TurnPhase
from modex_graph.constants import GraphNode


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
    async def test_normal_start_routes_to_before(self, make_runtime, make_graph_ctx):
        node = StartNode()
        runtime = make_runtime()
        ctx = make_graph_ctx(runtime=runtime)
        ctx.agent_ctx.emitter = _MockEmitter()  # type: ignore[assignment]

        await node.run(ctx)
        delivers = ctx.coordinator.collect_consumable_delivers(ReActNode.BEFORE, 0)
        assert [record.content for record in delivers] == [None]
        assert ctx.state.iteration == 0

    @pytest.mark.asyncio
    async def test_resume_routes_to_tool(self, make_runtime, make_graph_ctx):
        node = StartNode()
        runtime = make_runtime()
        runtime.state.resume_target = ReActNode.TOOL
        ctx = make_graph_ctx(runtime=runtime)

        await node.run(ctx)
        delivers = ctx.coordinator.collect_consumable_delivers(ReActNode.TOOL, 0)
        assert [record.content for record in delivers] == [None]
        assert ctx.state.resume_target is None

    @pytest.mark.asyncio
    async def test_resume_target_is_not_approval_specific(
        self, make_runtime, make_graph_ctx
    ):
        node = StartNode()
        runtime = make_runtime()
        runtime.state.resume_target = ReActNode.LLM
        ctx = make_graph_ctx(runtime=runtime)

        await node.run(ctx)
        delivers = ctx.coordinator.collect_consumable_delivers(ReActNode.LLM, 0)
        assert [record.content for record in delivers] == [None]
        assert ctx.state.resume_target is None


class TestEndNode:
    @pytest.mark.asyncio
    async def test_reads_result_and_delivers_to_end(
        self, make_runtime, make_graph_ctx
    ):
        node = EndNode()
        runtime = make_runtime()
        ctx = make_graph_ctx(runtime=runtime)
        emitter = _MockEmitter()
        ctx.agent_ctx.emitter = emitter  # type: ignore[assignment]
        result = AgentResult(content="Done!", stop_reason=StopReason.COMPLETED)
        ctx.state.result = result

        await node.run(ctx)
        assert ctx.coordinator.collect_consumable_delivers(GraphNode.END, 0)
        assert ctx.state.result is result
        assert ("complete", result) in emitter.events
        assert ctx.state.phase == TurnPhase.COMPLETED

    @pytest.mark.asyncio
    async def test_raises_when_result_is_none(self, make_runtime, make_graph_ctx):
        node = EndNode()
        runtime = make_runtime()
        ctx = make_graph_ctx(runtime=runtime)
        ctx.agent_ctx.emitter = _MockEmitter()  # type: ignore[assignment]

        with pytest.raises(RuntimeError, match="AfterTurnNode must set state.result"):
            await node.run(ctx)

    @pytest.mark.asyncio
    async def test_emits_final_output_on_completed_result(
        self, make_runtime, make_graph_ctx
    ):
        node = EndNode()
        runtime = make_runtime()
        ctx = make_graph_ctx(runtime=runtime)
        ctx.agent_ctx.emitter = _MockEmitter()  # type: ignore[assignment]
        result = AgentResult(content="Done!", stop_reason=StopReason.COMPLETED)
        ctx.state.result = result
        emitted: list = []

        async def _spy(event_type, data, _ctx):  # noqa: ANN001
            emitted.append((event_type, data))

        ctx.runtime.emit = _spy  # type: ignore[method-assign]

        await node.run(ctx)
        assert ("final_output", result) in emitted
        assert not any(e[0] == "error" for e in emitted)

    @pytest.mark.asyncio
    async def test_emits_error_event_on_error_stop_reason(
        self, make_runtime, make_graph_ctx
    ):
        node = EndNode()
        runtime = make_runtime()
        ctx = make_graph_ctx(runtime=runtime)
        ctx.agent_ctx.emitter = _MockEmitter()  # type: ignore[assignment]
        result = AgentResult(error="boom", stop_reason=StopReason.ERROR)
        ctx.state.result = result
        emitted: list = []

        async def _spy(event_type, data, _ctx):  # noqa: ANN001
            emitted.append((event_type, data))

        ctx.runtime.emit = _spy  # type: ignore[method-assign]

        await node.run(ctx)
        assert ("error", "boom") in emitted
        assert not any(e[0] == "final_output" for e in emitted)

    @pytest.mark.asyncio
    async def test_no_completion_event_on_cancelled(
        self, make_runtime, make_graph_ctx
    ):
        node = EndNode()
        runtime = make_runtime()
        ctx = make_graph_ctx(runtime=runtime)
        emitter = _MockEmitter()
        ctx.agent_ctx.emitter = emitter  # type: ignore[assignment]
        result = AgentResult(
            content="turn cancelled", stop_reason=StopReason.TURN_CANCELLED
        )
        ctx.state.result = result
        emitted: list = []

        async def _spy(event_type, data, _ctx):  # noqa: ANN001
            emitted.append((event_type, data))

        ctx.runtime.emit = _spy  # type: ignore[method-assign]

        await node.run(ctx)
        assert not any(
            e[0] in ("final_output", "error") for e in emitted
        )
        assert ("complete", result) in emitter.events

    @pytest.mark.asyncio
    async def test_no_completion_event_on_max_iterations(
        self, make_runtime, make_graph_ctx
    ):
        node = EndNode()
        runtime = make_runtime()
        ctx = make_graph_ctx(runtime=runtime)
        ctx.agent_ctx.emitter = _MockEmitter()  # type: ignore[assignment]
        result = AgentResult(
            content="max iterations reached",
            stop_reason=StopReason.MAX_ITERATIONS,
        )
        ctx.state.result = result
        emitted: list = []

        async def _spy(event_type, data, _ctx):  # noqa: ANN001
            emitted.append((event_type, data))

        ctx.runtime.emit = _spy  # type: ignore[method-assign]

        await node.run(ctx)
        assert not any(
            e[0] in ("final_output", "error") for e in emitted
        )


class TestLLMNode:
    @pytest.mark.asyncio
    async def test_routes_to_tool_on_has_tool_calls(
        self, make_runtime, make_graph_ctx, make_response
    ):
        async def _mock_call(messages, ctx):
            return make_response(
                content=None,
                tool_calls=[ToolCall(tool_name="search", arguments={})],
            )

        llm_client = _make_llm_client()
        llm_client.call = _mock_call  # type: ignore[method-assign]
        node = LLMNode(llm_client, InjectionDrainer())

        runtime = make_runtime()
        ctx = make_graph_ctx(runtime=runtime)
        ctx.agent_ctx.emitter = _MockEmitter()  # type: ignore[assignment]
        ctx.agent_ctx.history = _MockHistory()  # type: ignore[assignment]

        await node.run(ctx)
        assert ctx.coordinator.collect_consumable_delivers(ReActNode.TOOL, 0)

    @pytest.mark.asyncio
    async def test_routes_to_after_on_no_tool_calls(
        self, make_runtime, make_graph_ctx, make_response
    ):
        async def _mock_call(messages, ctx):
            return make_response(content="Hello!")

        llm_client = _make_llm_client()
        llm_client.call = _mock_call  # type: ignore[method-assign]
        node = LLMNode(llm_client, InjectionDrainer())

        runtime = make_runtime()
        ctx = make_graph_ctx(runtime=runtime)
        ctx.agent_ctx.emitter = _MockEmitter()  # type: ignore[assignment]
        ctx.agent_ctx.history = _MockHistory()  # type: ignore[assignment]

        await node.run(ctx)
        assert ctx.coordinator.collect_consumable_delivers(ReActNode.AFTER, 0)

    @pytest.mark.asyncio
    async def test_routes_to_after_on_max_iterations(
        self, make_runtime, make_graph_ctx
    ):
        llm_client = _make_llm_client()
        node = LLMNode(llm_client, InjectionDrainer())

        runtime = make_runtime()
        runtime.state.iteration = 5
        ctx = make_graph_ctx(runtime=runtime)
        ctx.agent_ctx.emitter = _MockEmitter()  # type: ignore[assignment]
        ctx.agent_ctx.max_iterations = 5

        await node.run(ctx)
        delivers = ctx.coordinator.collect_consumable_delivers(ReActNode.AFTER, 0)
        assert [record.content for record in delivers] == [None]

    @pytest.mark.asyncio
    async def test_routes_to_after_on_llm_error(
        self, make_runtime, make_graph_ctx, make_response
    ):
        async def _mock_call(messages, ctx):
            return make_response(
                content="API Error",
                finish_reason=FinishReason.ERROR.value,
            )

        llm_client = _make_llm_client()
        llm_client.call = _mock_call  # type: ignore[method-assign]
        node = LLMNode(llm_client, InjectionDrainer())

        runtime = make_runtime()
        ctx = make_graph_ctx(runtime=runtime)
        ctx.agent_ctx.emitter = _MockEmitter()  # type: ignore[assignment]
        ctx.agent_ctx.history = _MockHistory()  # type: ignore[assignment]

        await node.run(ctx)
        delivers = ctx.coordinator.collect_consumable_delivers(ReActNode.AFTER, 0)
        assert delivers
        assert delivers[0].content is not None and "error" in delivers[0].content


class TestToolNode:
    @pytest.mark.asyncio
    async def test_execute_batch_all_allowed(
        self, make_runtime, make_graph_ctx, make_response
    ):
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

        runtime = make_runtime()
        runtime.state.iteration = 1
        ctx = make_graph_ctx(runtime=runtime)
        ctx.agent_ctx.emitter = _MockEmitter()  # type: ignore[assignment]
        ctx.agent_ctx.history = history  # type: ignore[assignment]
        await history.append(
            ChatMessage(role=MessageRole.ASSISTANT, content="", tool_calls=[tc1, tc2])
        )

        await node.run(ctx)
        delivers = ctx.coordinator.collect_consumable_delivers(ReActNode.LLM, 0)
        assert [record.content for record in delivers] == [None]
        assert len(executed) == 2
        assert len(history.msgs) == 3

    @pytest.mark.asyncio
    async def test_missing_call_id_canonicalized_before_events(
        self, make_runtime, make_coordinator
    ):
        """Providers may omit ``call_id``. The node assigns ONE id up front so
        TOOL_CALL_START/END event payloads, the executed call, and the history
        tool message all carry the same id — streamed start/end pairs and
        persisted call/result records can then be matched by id."""
        captured: dict[str, str | None] = {}

        tool_executor = ToolExecutor()

        async def _mock_execute(tc, ctx):
            captured["call_id"] = tc.call_id
            return ToolResult.from_text(tc.tool_name, "ok")

        tool_executor.execute = _mock_execute  # type: ignore[method-assign]
        node = ToolNode(tool_executor)

        history = _MockHistory()
        tc = ToolCall(tool_name="read", arguments={})  # provider omitted call_id

        runtime = make_runtime()
        runtime.state.iteration = 1
        emitter = _MockEmitter()
        agent_ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            identity=runtime.state.identity,
            runtime=runtime,
            session=SessionInfo.from_str("test.agent"),
        )
        ctx = ReActGraphContext(
            state=runtime.state,  # type: ignore[arg-type]
            runtime=ReactGraphRuntime(emitter=emitter),  # type: ignore[arg-type]
            user_data=agent_ctx,
            coordinator=make_coordinator(),
        )
        ctx.agent_ctx.history = history  # type: ignore[assignment]
        await history.append(
            ChatMessage(role=MessageRole.ASSISTANT, content="", tool_calls=[tc])
        )

        await node.run(ctx)

        call_id = captured["call_id"]
        assert call_id  # assigned, non-empty
        starts = [d for e, d in emitter.events if e == ReActEvent.TOOL_CALL_START]
        ends = [d for e, d in emitter.events if e == ReActEvent.TOOL_CALL_END]
        assert len(starts) == 1
        assert len(ends) == 1
        assert starts[0].call_id == call_id
        assert ends[0][0].call_id == call_id
        assert history.msgs[1].tool_call_id == call_id

    @pytest.mark.asyncio
    async def test_denied_tool_cascades_and_cancels(
        self, make_runtime, make_graph_ctx, make_response
    ):
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

        from modex_agent.approval.runtime import ApprovalRuntime
        from modex_agent.runtime.enums import ApprovalDenyPolicy

        runtime = make_runtime()
        runtime.state.iteration = 1
        runtime.services.approval = ApprovalRuntime(
            classifier=type(
                "_Cls",
                (),
                {"classify": lambda s, tc, c: "normal" if tc.tool_name == "t1" else "hardline"},
            )(),  # type: ignore[arg-type]
            default_deny_policy=ApprovalDenyPolicy.CANCEL_TURN,
        )
        ctx = make_graph_ctx(runtime=runtime)
        ctx.agent_ctx.emitter = _MockEmitter()  # type: ignore[assignment]
        ctx.agent_ctx.history = history  # type: ignore[assignment]
        await history.append(
            ChatMessage(role=MessageRole.ASSISTANT, content="", tool_calls=[tc1, tc2])
        )

        await node.run(ctx)

        # Atomic batch: one DENIED cascades to PREEMPT the ALLOWED call,
        # nothing executes, and CANCEL_TURN routes the turn to AFTER.
        delivers = ctx.coordinator.collect_consumable_delivers(ReActNode.AFTER, 0)
        assert [record.content for record in delivers] == [None]
        assert len(executed) == 0

    @pytest.mark.asyncio
    async def test_denied_tool_cancel_path_uses_real_tool_executor(
        self, make_runtime, make_graph_ctx, make_response
    ):
        # DENIED decisions never reach the executor, so a real ToolExecutor
        # with an unused provider is sufficient to exercise the cancel path.
        tool_executor = ToolExecutor()
        node = ToolNode(tool_executor)

        tc = ToolCall(tool_name="write", arguments={"path": "/tmp/x"}, call_id="c1")

        from modex_agent.approval.runtime import ApprovalRuntime
        from modex_agent.runtime.enums import ApprovalDenyPolicy

        runtime = make_runtime()
        runtime.state.iteration = 1
        runtime.services.approval = ApprovalRuntime(
            classifier=type("_Cls", (), {"classify": lambda s, tc, c: "hardline"})(),  # type: ignore[arg-type]
            default_deny_policy=ApprovalDenyPolicy.CANCEL_TURN,
        )
        ctx = make_graph_ctx(runtime=runtime)
        ctx.agent_ctx.emitter = _MockEmitter()  # type: ignore[assignment]
        ctx.agent_ctx.history = _MockHistory()  # type: ignore[assignment]
        await ctx.agent_ctx.history.append(
            ChatMessage(role=MessageRole.ASSISTANT, content="", tool_calls=[tc])
        )

        await node.run(ctx)

        delivers = ctx.coordinator.collect_consumable_delivers(ReActNode.AFTER, 0)
        assert [record.content for record in delivers] == [None]

    @pytest.mark.asyncio
    async def test_exceeds_max_tools_routes_to_after(
        self, make_runtime, make_graph_ctx, make_response
    ):
        tool_executor = ToolExecutor()
        node = ToolNode(tool_executor)

        tc_list = [ToolCall(tool_name=f"t{i}", arguments={}, call_id=f"c{i}") for i in range(5)]

        runtime = make_runtime()
        runtime.state.custom[TurnCustomKey.MAX_TOOLS_PER_TURN] = 3
        ctx = make_graph_ctx(runtime=runtime)
        ctx.agent_ctx.emitter = _MockEmitter()  # type: ignore[assignment]
        ctx.agent_ctx.history = _MockHistory()  # type: ignore[assignment]
        await ctx.agent_ctx.history.append(
            ChatMessage(role=MessageRole.ASSISTANT, content="", tool_calls=tc_list)
        )

        await node.run(ctx)
        delivers = ctx.coordinator.collect_consumable_delivers(ReActNode.AFTER, 0)
        assert [record.content for record in delivers] == [None]
        assert ctx.state.phase == TurnPhase.FAILED

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
