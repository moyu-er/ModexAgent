"""Tests for AgentRuntime and AgentRuntimeServices."""
from __future__ import annotations

from modex_agent.runtime.enums import AgentKind, TurnPhase
from modex_agent.runtime.models import TurnIdentity, TurnStateBase
from modex_agent.core.session_id import SessionInfo
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices, require_runtime_state
from modex_agent.runtime.store import NoOpTurnStateStore


def test_runtime_services_are_not_part_of_turn_state() -> None:
    services = AgentRuntimeServices(
        turn_store=NoOpTurnStateStore(),
    )
    state = TurnStateBase(
        identity=TurnIdentity(agent_id="bot", session=SessionInfo.from_str("s1"), turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    runtime = AgentRuntime(services=services, state=state)

    assert runtime.services.turn_store is not None
    assert runtime.state.identity.turn_id == "t1"


def test_require_runtime_state_returns_expected_type() -> None:
    state = TurnStateBase(
        identity=TurnIdentity(agent_id="bot", session=SessionInfo.from_str("s1"), turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    runtime = AgentRuntime(services=AgentRuntimeServices(), state=state)

    assert require_runtime_state(runtime, TurnStateBase) is state
