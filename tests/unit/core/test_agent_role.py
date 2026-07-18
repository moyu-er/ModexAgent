"""Tests for the ``AgentRole`` StrEnum (T1 data layer).

The enum centralizes the seven canonical preset role names. Values
serialize as plain strings (StrEnum behavior) so the wire models
(``MainAgentSpec`` / ``SubagentSpec``) and ``AgentDescriptor`` carry
``list[str]`` — preset values collapse to their string value, custom
strings are preserved verbatim.
"""

from __future__ import annotations

from enum import StrEnum

from modex_agent.core.constants import AgentRole


class TestAgentRoleValues:
    """The seven preset values exist and map to their lowercase string."""

    def test_planner_value(self) -> None:
        assert AgentRole.PLANNER == "planner"
        assert AgentRole.PLANNER.value == "planner"

    def test_implementer_value(self) -> None:
        assert AgentRole.IMPLEMENTER == "implementer"
        assert AgentRole.IMPLEMENTER.value == "implementer"

    def test_reviewer_value(self) -> None:
        assert AgentRole.REVIEWER == "reviewer"
        assert AgentRole.REVIEWER.value == "reviewer"

    def test_scout_value(self) -> None:
        assert AgentRole.SCOUT == "scout"
        assert AgentRole.SCOUT.value == "scout"

    def test_oracle_value(self) -> None:
        assert AgentRole.ORACLE == "oracle"
        assert AgentRole.ORACLE.value == "oracle"

    def test_coordinator_value(self) -> None:
        assert AgentRole.COORDINATOR == "coordinator"
        assert AgentRole.COORDINATOR.value == "coordinator"

    def test_communicator_value(self) -> None:
        assert AgentRole.COMMUNICATOR == "communicator"
        assert AgentRole.COMMUNICATOR.value == "communicator"

    def test_exactly_seven_preset_values(self) -> None:
        # No accidental additions/removals.
        assert len(AgentRole) == 7
        assert {member.name for member in AgentRole} == {
            "PLANNER",
            "IMPLEMENTER",
            "REVIEWER",
            "SCOUT",
            "ORACLE",
            "COORDINATOR",
            "COMMUNICATOR",
        }


class TestAgentRoleSerialization:
    """StrEnum behavior: members are plain strings for serialization."""

    def test_member_is_str_instance(self) -> None:
        # StrEnum members ARE str instances — this is what makes them
        # serialize as plain strings in JSON/YAML.
        assert isinstance(AgentRole.PLANNER, str)
        assert isinstance(AgentRole.PLANNER, StrEnum)

    def test_member_serializes_as_plain_string(self) -> None:
        # When dumped via json/yaml, an AgentRole member renders as its
        # plain string value (no "AgentRole.PLANNER" enum-repr noise).
        import json

        assert json.dumps(AgentRole.PLANNER) == '"planner"'

    def test_string_value_resolves_back_to_member(self) -> None:
        # The reverse direction: a plain string resolves to the member
        # via AgentRole("planner").
        assert AgentRole("planner") is AgentRole.PLANNER
        assert AgentRole("oracle") is AgentRole.ORACLE

    def test_member_usable_as_list_str_element(self) -> None:
        # The roles field is list[str] (not list[AgentRole]); members
        # must work transparently as list elements.
        roles: list[str] = [AgentRole.PLANNER, AgentRole.REVIEWER]
        assert roles == ["planner", "reviewer"]
