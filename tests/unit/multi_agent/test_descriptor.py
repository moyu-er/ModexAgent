from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.descriptor import (
    AgentDescriptor,
    AgentInstance,
    AgentLLMConfig,
    ContextGovernanceConfig,
)
from modex_agent.multi_agent.state import AgentState


class TestAgentLLMConfig:
    def test_defaults(self) -> None:
        cfg = AgentLLMConfig()
        assert cfg.temperature == 0.7
        assert cfg.max_output_tokens is None
        assert cfg.top_p == 1.0
        assert cfg.extra_params == {}


class TestContextGovernanceConfig:
    def test_defaults(self) -> None:
        cfg = ContextGovernanceConfig()
        assert cfg.enable_microcompact is True
        assert cfg.max_tool_result_chars == 4000


class TestAgentDescriptor:
    def test_defaults(self) -> None:
        addr = AgentAddress(kind="agent", name="coder")
        desc = AgentDescriptor(address=addr)
        assert desc.max_iterations == 15
        assert desc.execution_strategy == "react"
        assert desc.context_strategy == "persistent"
        assert desc.inbox_strategy == "drain_all"
        assert desc.allowed_callers is None

    def test_full_fields(self) -> None:
        addr = AgentAddress(
            kind="agent", name="reviewer", role="code_reviewer", capabilities=["python"]
        )
        desc = AgentDescriptor(
            address=addr,
            allowed_tools=["read_file", "write_file"],
            denied_tools=["bash"],
            max_iterations=5,
            execution_strategy="single_turn",
            context_strategy="ephemeral",
            allowed_callers=["planner"],
        )
        assert desc.address == addr
        assert desc.allowed_tools == ["read_file", "write_file"]
        assert desc.denied_tools == ["bash"]
        assert desc.max_iterations == 5
        assert desc.execution_strategy == "single_turn"
        assert desc.context_strategy == "ephemeral"
        assert desc.allowed_callers == ["planner"]

    def test_rejects_legacy_allowed_skills_authority(self) -> None:
        assert "allowed_skills" not in AgentDescriptor.model_fields
        with pytest.raises(ValidationError):
            AgentDescriptor.model_validate(
                {
                    "address": AgentAddress(kind="agent", name="reviewer"),
                    "allowed_skills": ["refactor"],
                }
            )


class TestAgentDescriptorRolesField:
    """T1 data-layer: ``roles`` is metadata, NOT identity.

    The ``compare=False`` flag on the dataclass field excludes ``roles``
    from the auto-generated ``__eq__`` / ``__hash__``. Two descriptors
    that differ only in ``roles`` MUST be equal — pool registration dedup
    keys on identity (address + capabilities), not role tags.
    """

    def test_roles_defaults_to_empty_list(self) -> None:
        desc = AgentDescriptor(address=AgentAddress(kind="agent", name="coder"))
        assert desc.roles == []

    def test_roles_accepts_preset_and_custom_strings(self) -> None:
        desc = AgentDescriptor(
            address=AgentAddress(kind="agent", name="coder"),
            roles=["planner", "custom-role"],
        )
        assert desc.roles == ["planner", "custom-role"]

    def test_eq_excludes_roles(self) -> None:
        """Two descriptors differing only in roles are equal."""
        addr = AgentAddress(kind="agent", name="coder")
        a = AgentDescriptor(address=addr, roles=["planner"])
        b = AgentDescriptor(address=addr, roles=["reviewer"])
        assert a == b

    def test_eq_excludes_roles_even_when_one_empty(self) -> None:
        """Empty roles vs non-empty roles: still equal."""
        addr = AgentAddress(kind="agent", name="coder")
        a = AgentDescriptor(address=addr, roles=[])
        b = AgentDescriptor(address=addr, roles=["planner", "oracle"])
        assert a == b

    def test_eq_still_includes_other_fields(self) -> None:
        """Sanity: equality still keys on non-roles fields."""
        addr = AgentAddress(kind="agent", name="coder")
        a = AgentDescriptor(address=addr, max_iterations=10)
        b = AgentDescriptor(address=addr, max_iterations=20)
        assert a != b

    def test_eq_still_includes_address(self) -> None:
        """Sanity: different addresses still unequal."""
        a = AgentDescriptor(address=AgentAddress(kind="agent", name="coder"))
        b = AgentDescriptor(address=AgentAddress(kind="agent", name="reviewer"))
        assert a != b


class TestAgentInstance:
    def test_creation_and_stop(self) -> None:
        from unittest.mock import MagicMock

        addr = AgentAddress(kind="agent", name="coder")
        desc = AgentDescriptor(address=addr)
        ctx = MagicMock()

        instance = AgentInstance(
            descriptor=desc,
            context_manager=ctx,
        )
        assert instance.descriptor == desc
        assert instance.context_manager is ctx
        assert instance.pipeline is None

    def test_agent_state_enum(self) -> None:
        assert AgentState.INITIALIZING.value == "initializing"
        assert AgentState.IDLE.value == "idle"
        assert AgentState.WORKING.value == "working"
        assert AgentState.ERROR.value == "error"
        assert AgentState.SHUTTING_DOWN.value == "shutting_down"
        assert AgentState.SHUTDOWN.value == "shutdown"
