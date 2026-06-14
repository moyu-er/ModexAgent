"""Tests for clean runtime mode — typed state, no persistence."""
from __future__ import annotations

from framework.agents.react.state import ReActTurnState
from framework.runtime.enums import AgentKind, TurnPhase
from framework.runtime.models import TurnIdentity
from framework.runtime.services import AgentRuntime, AgentRuntimeServices
from framework.core.session_id import SessionId
from framework.runtime.store import NoOpTurnStateStore


def test_clean_mode_still_has_typed_react_state() -> None:
    state = ReActTurnState(
        identity=TurnIdentity(agent_id="bot", session=SessionId.from_str("s1"), turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    runtime = AgentRuntime(
        services=AgentRuntimeServices(turn_store=NoOpTurnStateStore()),
        state=state,
    )

    assert isinstance(runtime.state, ReActTurnState)
    assert runtime.services.turn_store is not None
