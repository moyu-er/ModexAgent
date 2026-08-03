# ruff: noqa: ANN401
"""Integration: the probe hook keeps the ReAct loop alive and keeps the XML out
of the user-facing stream (it lands only in ctx.history = LLM memory)."""
from __future__ import annotations

from typing import Any

import pytest

from modex_agent.agents.react.constants import ReActNode
from modex_agent.agents.react.context import ReActGraphContext
from modex_agent.agents.react.injection_drainer import InjectionDrainer
from modex_agent.agents.react.llm_client import ReactLlmClient
from modex_agent.agents.react.nodes.llm import LLMNode
from modex_agent.agents.react.runtime import ReactGraphRuntime
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import FinishReason
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.core.types import LLMResponse, TodoStatus
from modex_agent.hook import HookSpec
from modex_agent.hook.runner import HookRunner
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.runtime.store import JsonFileTodoStore, TodoItem
from modex_agent.tools.standard import TodoCompletionProbeHook, TodoReadTool
from modex_graph import (    GraphNode,

    GraphPersistenceCoordinator,
    InvocationContext,
    NullDeliverStoreFactory,
    NullGraphMetadataStore,
    NullNodeStateFactory,
)


class _AutoRegCoord(GraphPersistenceCoordinator):
    """Test-only coordinator that auto-registers nodes on begin_invocation."""

    def begin_invocation(self, node_name: str) -> InvocationContext:
        if self.get_deliver_store(node_name) is None:
            self.register_node(node_name)
        return super().begin_invocation(node_name)
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



class _RecordingEmitter:
    """Captures every string emitted to the user-facing stream."""
    def __init__(self) -> None:
        self.streamed: list[str] = []

    async def emit(self, event, data=None): ...
    async def emit_complete(self, result): ...
    async def emit_delta(self, delta: str) -> None:
        self.streamed.append(delta)
    async def emit_content(self, content: str) -> None:
        self.streamed.append(content)
    async def emit_stream_end(self, resuming=False): ...
    def wants_streaming(self) -> bool:
        return False  # forces _call_non_streaming -> emit_content path


@pytest.mark.asyncio
async def test_probe_continues_loop_and_keeps_xml_out_of_stream(tmp_path, monkeypatch):
    # --- collaborators ------------------------------------------------------
    store = JsonFileTodoStore(tmp_path)
    tm = InMemoryToolManager()
    tm.register(TodoReadTool(store))

    state = ReActTurnState(
        identity=TurnIdentity(agent_id="t", session=SessionInfo.from_str("s1"), turn_id="t1"),
        agent_kind=AgentKind.REACT, phase=TurnPhase.CREATED,
    )
    services = AgentRuntimeServices()
    services.hooks = HookRunner(
        [HookSpec(hook=TodoCompletionProbeHook(store=store, tool_manager=tm))]
    )
    runtime = AgentRuntime(services=services, state=state)
    graph_runtime = ReactGraphRuntime(hook_runner=services.hooks)

    agent_ctx = AgentContext(
        system_prompt="", history=ListMessageHistory(), tool_manager=tm,
        identity=state.identity, runtime=runtime,
        session=SessionInfo.from_str("s1"),
    )
    agent_ctx.emitter = _RecordingEmitter()
    ctx = ReActGraphContext(
        state=state,
        runtime=graph_runtime,
        user_data=agent_ctx,
        coordinator=_AutoRegCoord(
            graph_instance_id=0,
            graph_metadata_store=NullGraphMetadataStore(),
            default_node_state_factory=NullNodeStateFactory(),
            default_deliver_store_factory=NullDeliverStoreFactory(),
        ),
    )

    # Seed an unfinished todo for THIS session id, so gate 3 passes.
    sid = agent_ctx.session.session_id
    await store.save(sid, [TodoItem(content="ship feature", status=TodoStatus.PENDING)])

    # --- stub the LLM: stream "done." then return a plain (no-tool) response -
    client = ReactLlmClient(provider=object())  # type: ignore[arg-type]

    async def _fake_call(messages, ctx):
        await agent_ctx.emitter.emit_content("done.")   # user-facing stream (pre-hook)
        return LLMResponse(content="done.", tool_calls=[], finish_reason=FinishReason.STOP)

    monkeypatch.setattr(client, "call", _fake_call)

    node = LLMNode(llm_client=client, injection_drainer=InjectionDrainer())

    # --- act ----------------------------------------------------------------
    await node.run(ctx)

    # --- the loop continues (probe injected a tool call → routed to TOOL) --
    assert ReActNode.TOOL in node.result

    messages = await agent_ctx.history.to_list()
    assistant = [m for m in messages if m.role == "assistant"][-1]
    assert any(
        tc.tool_name == "todo_read"
        for tc in (assistant.tool_calls or [])
    )
    assert "<system_note" in (assistant.content or "")

    # --- session: the user-facing stream saw the plain text but NOT the XML -
    assert "done." in agent_ctx.emitter.streamed  # type: ignore[union-attr]
    assert not any("<system_note" in s for s in agent_ctx.emitter.streamed)  # type: ignore[union-attr]
