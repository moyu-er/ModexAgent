"""Tests for ``ReactStateFactory`` — the named business ``StateFactory``.

Per ticket 08: the ReAct business ``StateFactory`` lives in ``modex_agent`` and
is registered with a ``StateRegistry`` under ``REACT_STATE_FACTORY_NAME`` so a
``GraphSpec`` with ``state_schema = "react_turn_state"`` can reference it by
name.
"""

from __future__ import annotations

from modex_agent.agents.react.constants import ReActNode
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.agents.react.state_factory import (
    REACT_STATE_FACTORY_NAME,
    ReactStateFactory,
)
from modex_graph.state import StateRegistry


def test_factory_name_constant() -> None:
    assert REACT_STATE_FACTORY_NAME == "react_turn_state"


def test_create_state_returns_react_turn_state() -> None:
    state = ReactStateFactory().create_state()
    assert isinstance(state, ReActTurnState)


def test_restore_state_round_trips_checkpoint() -> None:
    factory = ReactStateFactory()
    state = factory.create_state()
    assert isinstance(state, ReActTurnState)
    state.iteration = 3
    state.current_node = ReActNode.TOOL
    data = state.checkpoint()

    restored = factory.restore_state(dict(data))
    assert isinstance(restored, ReActTurnState)
    assert restored.iteration == 3
    assert restored.current_node is ReActNode.TOOL


def test_state_schema_introspects_react_fields() -> None:
    schema = ReactStateFactory().state_schema()
    assert schema.name == "ReActTurnState"
    assert schema.description == "Introspected from ReActTurnState"
    field_names = {f.name for f in schema.fields}
    # Business fields declared on ReActTurnState are included.
    assert {"current_node", "iteration", "result", "tool_batches"} <= field_names
    # Base GraphState fields are skipped by SimpleStateFactory introspection.
    assert "resume_target" not in field_names
    # Field metadata is populated (channel + type strings).
    by_name = {f.name: f for f in schema.fields}
    assert by_name["current_node"].channel == "last_value"
    assert by_name["iteration"].field_type == "int"


def test_registry_registration_by_name() -> None:
    registry = StateRegistry()
    registry.register(REACT_STATE_FACTORY_NAME, ReactStateFactory())

    assert registry.is_registered("react_turn_state")
    state = registry.create_state("react_turn_state")
    assert isinstance(state, ReActTurnState)
    state.iteration = 5

    restored = registry.restore_state("react_turn_state", state.checkpoint())
    assert isinstance(restored, ReActTurnState)
    assert restored.iteration == 5
    assert registry.get_schema("react_turn_state").name == "ReActTurnState"
