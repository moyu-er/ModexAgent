"""Tests for AgentRuntime and AgentRuntimeServices."""
from __future__ import annotations

from modex_agent.runtime.enums import AgentKind, TurnPhase
from modex_agent.runtime.models import TurnIdentity, TurnStateBase
from modex_agent.core.session_id import SessionInfo
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices, require_runtime_state
from modex_agent.runtime.store import NoOpTurnStateStore
from modex_agent.ioc.configs.llm import Modality, ModelCapabilities, ModelInfo


def _make_state() -> TurnStateBase:
    return TurnStateBase(
        identity=TurnIdentity(agent_id="bot", session=SessionInfo.from_str("s1"), turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )


def test_runtime_services_are_not_part_of_turn_state() -> None:
    services = AgentRuntimeServices(
        turn_store=NoOpTurnStateStore(),
    )
    runtime = AgentRuntime(services=services, state=_make_state())

    assert runtime.services.turn_store is not None
    assert runtime.state.identity.turn_id == "t1"


def test_graph_runtime_is_not_an_agent_runtime_field() -> None:
    assert "graph_runtime" not in AgentRuntime.__dataclass_fields__


def test_require_runtime_state_returns_expected_type() -> None:
    runtime = AgentRuntime(services=AgentRuntimeServices(), state=_make_state())

    assert require_runtime_state(runtime, TurnStateBase) is runtime.state


def test_model_info_field_defaults_to_none() -> None:
    assert AgentRuntimeServices().model_info is None


def test_model_info_property_threads_to_runtime() -> None:
    info = ModelInfo(
        model_name="test",
        capabilities=ModelCapabilities(modalities=frozenset({Modality.TEXT, Modality.IMAGE})),
    )
    services = AgentRuntimeServices(model_info=info)
    runtime = AgentRuntime(services=services, state=_make_state())

    assert runtime.model_info is info
    assert runtime.model_info.capabilities.supports(Modality.IMAGE)
    assert not runtime.model_info.capabilities.supports(Modality.AUDIO)
