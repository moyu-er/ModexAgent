"""T1 data-layer: ``roles`` field on ``MainAgentSpec`` and ``SubagentSpec``.

Both wire models are frozen Pydantic ``BaseModel`` (``extra="forbid"``).
The ``roles`` field is ``list[str]`` defaulting to ``[]``; preset values
are :class:`modex_agent.core.constants.AgentRole` members (which serialize
as plain strings via StrEnum), custom strings are preserved verbatim.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_agent.core.constants import AgentRole
from modex_agent.multi_agent.pool_config import MainAgentSpec, SubagentSpec


class TestMainAgentSpecRoles:
    def test_roles_defaults_to_empty_list(self) -> None:
        spec = MainAgentSpec(agent_name="main")
        assert spec.roles == []

    def test_roles_accepts_preset_string_values(self) -> None:
        spec = MainAgentSpec(agent_name="main", roles=["coordinator", "planner"])
        assert spec.roles == ["coordinator", "planner"]

    def test_roles_accepts_agentrole_members(self) -> None:
        # AgentRole members ARE str instances (StrEnum), so they pass
        # list[str] validation transparently and collapse to plain strings.
        spec = MainAgentSpec(
            agent_name="main",
            roles=[AgentRole.COORDINATOR, AgentRole.COMMUNICATOR],
        )
        assert spec.roles == ["coordinator", "communicator"]

    def test_roles_accepts_custom_strings(self) -> None:
        spec = MainAgentSpec(
            agent_name="main",
            roles=["custom-role", "another-role"],
        )
        assert spec.roles == ["custom-role", "another-role"]

    def test_roles_mixed_preset_and_custom(self) -> None:
        spec = MainAgentSpec(
            agent_name="main",
            roles=[AgentRole.PLANNER, "custom-role"],
        )
        assert spec.roles == ["planner", "custom-role"]

    def test_roles_is_frozen(self) -> None:
        spec = MainAgentSpec(agent_name="main", roles=["planner"])
        with pytest.raises(ValidationError):
            spec.roles = ["reviewer"]  # type: ignore[misc]

    def test_roles_rejects_unknown_keys_at_top_level(self) -> None:
        # extra="forbid" — unknown fields still rejected alongside roles.
        with pytest.raises(ValidationError):
            MainAgentSpec(agent_name="main", roles=["planner"], unknown="x")


class TestSubagentSpecRoles:
    def test_roles_defaults_to_empty_list(self) -> None:
        spec = SubagentSpec(agent_name="worker")
        assert spec.roles == []

    def test_roles_accepts_preset_string_values(self) -> None:
        spec = SubagentSpec(agent_name="worker", roles=["implementer", "reviewer"])
        assert spec.roles == ["implementer", "reviewer"]

    def test_roles_accepts_agentrole_members(self) -> None:
        spec = SubagentSpec(
            agent_name="worker",
            roles=[AgentRole.IMPLEMENTER, AgentRole.SCOUT],
        )
        assert spec.roles == ["implementer", "scout"]

    def test_roles_accepts_custom_strings(self) -> None:
        spec = SubagentSpec(
            agent_name="worker",
            roles=["data-cleanup", "refactor"],
        )
        assert spec.roles == ["data-cleanup", "refactor"]

    def test_roles_is_frozen(self) -> None:
        spec = SubagentSpec(agent_name="worker", roles=["implementer"])
        with pytest.raises(ValidationError):
            spec.roles = ["reviewer"]  # type: ignore[misc]

    def test_roles_rejects_unknown_keys_at_top_level(self) -> None:
        with pytest.raises(ValidationError):
            SubagentSpec(agent_name="worker", roles=["planner"], unknown="x")


class TestRolesPreservesOrder:
    """Order matters: roles are an ordered list, not a set."""

    def test_main_agent_spec_preserves_order(self) -> None:
        spec = MainAgentSpec(
            agent_name="main",
            roles=["coordinator", "planner", "reviewer"],
        )
        assert spec.roles == ["coordinator", "planner", "reviewer"]

    def test_subagent_spec_preserves_order(self) -> None:
        spec = SubagentSpec(
            agent_name="worker",
            roles=["reviewer", "planner", "coordinator"],
        )
        assert spec.roles == ["reviewer", "planner", "coordinator"]

    def test_duplicates_preserved(self) -> None:
        # No implicit dedup — caller's responsibility.
        spec = MainAgentSpec(
            agent_name="main",
            roles=["planner", "planner"],
        )
        assert spec.roles == ["planner", "planner"]
