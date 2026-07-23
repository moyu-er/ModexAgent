"""Tests for interceptor context types with typed turn_state."""
from __future__ import annotations

from modex_agent.interceptor.abc import (
    IterationContext,
    LLMCallContext,
    LLMRequest,
    LLMStreamContext,
    ToolCallContext,
    TurnContext,
)
from modex_agent.core.message import ChatMessage
from modex_agent.core.types import MessageRole, ToolCall
from modex_agent.runtime.enums import AgentKind, TurnPhase
from modex_agent.runtime.models import TurnIdentity, TurnStateBase
from modex_agent.core.session_id import SessionInfo


def _state() -> TurnStateBase:
    return TurnStateBase(
        identity=TurnIdentity(agent_id="bot", session=SessionInfo.from_str("s1"), turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.RUNNING,
    )


def test_all_interceptor_contexts_carry_turn_state() -> None:
    state = _state()
    request = LLMRequest(messages=[], model="fake", stream=False)

    tc_ctx = ToolCallContext(
        tool_call=ToolCall(tool_name="read", arguments={}),
        tool_name="read",
        arguments={},
        session_id="s1",
        turn_state=state,
    )
    assert tc_ctx.turn_state is state

    turn_ctx = TurnContext(prompt="p", turn_id="t1", max_iterations=3, turn_state=state)
    assert turn_ctx.turn_state is state

    iter_ctx = IterationContext(iteration=1, turn_id="t1", turn_state=state)
    assert iter_ctx.turn_state is state

    llm_call = LLMCallContext(messages=[], turn_state=state, request=request)
    assert llm_call.turn_state is state

    llm_stream = LLMStreamContext(messages=[], turn_state=state, request=request)
    assert llm_stream.turn_state is state


def test_llm_request_is_typed() -> None:
    req = LLMRequest(messages=[ChatMessage(role=MessageRole.USER, content="hi")], model="gpt-4", stream=True, provider_options={"temperature": 0.5})

    assert req.model == "gpt-4"
    assert req.stream is True
    assert req.provider_options == {"temperature": 0.5}
