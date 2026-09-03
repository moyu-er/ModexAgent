"""Tests for persisting partial assistant content when an LLM stream is interrupted.

The partial-stash WRITE (in ReactLlmClient's event loop) is tested at
tests/unit/agents/react/test_llm_client.py. This file keeps the agent-level
concerns: the interrupt-reason mapping and the run()-level READ/persist
(_persist_interrupted_partial) that consumes the stashed partial.
"""

import asyncio

import pytest

from modex_agent.agents.react.agent import (
    _interrupt_reason_from,
    _persist_interrupted_partial,
)
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.control.exceptions import (
    AgentCancelledError,
    AgentTimeoutError,
    PolicyViolationError,
)
from modex_agent.core.message import ContentFormat
from modex_agent.core.session_id import SessionInfo
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.tools.manager import InMemoryToolManager


def _make_ctx():
    from modex_agent.core.agent import AgentContext

    state = ReActTurnState(
        identity=TurnIdentity(
            agent_id="test", session=SessionInfo.from_str("s1"), turn_id="t1"
        ),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    runtime = AgentRuntime(services=AgentRuntimeServices(), state=state)
    return AgentContext(
        system_prompt="",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("test.agent"),
        max_iterations=5,
        identity=state.identity,
        runtime=runtime,
    )


class TestInterruptReasonMapping:
    def test_user_stop_for_agent_cancelled(self):
        assert _interrupt_reason_from(AgentCancelledError()) == "user_stop"

    def test_timeout_for_agent_timeout(self):
        assert _interrupt_reason_from(AgentTimeoutError()) == "timeout"

    def test_policy_for_policy_violation(self):
        assert _interrupt_reason_from(PolicyViolationError()) == "policy"

    def test_cancelled_for_asyncio_cancelled(self):
        assert _interrupt_reason_from(asyncio.CancelledError()) == "cancelled"

    def test_error_for_generic_exception(self):
        assert _interrupt_reason_from(RuntimeError("boom")) == "error"


class TestPersistInterruptedPartial:
    @pytest.mark.asyncio
    async def test_appends_xml_message_to_history_and_message_delta(self):
        ctx = _make_ctx()
        ctx.runtime.state.custom[TurnCustomKey.INTERRUPTED_PARTIAL] = {
            "content": "partial content",
            "tool_names": ["read_file"],
        }

        await _persist_interrupted_partial(ctx, "user_stop")

        history = await ctx.history.to_list()
        assert len(history) == 1
        msg = history[0]
        assert msg["role"] == "assistant"
        assert msg["content_format"] == ContentFormat.XML.value
        assert msg["truncatable_paths"] == ["content"]
        assert "<interrupted_response" in msg["content"]
        assert "partial content" in msg["content"]
        # Mirrored into message_delta so _get_turn_messages stays consistent.
        assert len(ctx.runtime.state.message_delta) == 1
        # Stash cleared after persist.
        assert TurnCustomKey.INTERRUPTED_PARTIAL not in ctx.runtime.state.custom

    @pytest.mark.asyncio
    async def test_noop_when_no_partial_stashed(self):
        ctx = _make_ctx()
        await _persist_interrupted_partial(ctx, "error")
        assert await ctx.history.to_list() == []
        assert ctx.runtime.state.message_delta == []

    @pytest.mark.asyncio
    async def test_noop_when_partial_empty(self):
        ctx = _make_ctx()
        ctx.runtime.state.custom[TurnCustomKey.INTERRUPTED_PARTIAL] = {
            "content": "",
            "tool_names": [],
        }
        await _persist_interrupted_partial(ctx, "error")
        assert await ctx.history.to_list() == []
