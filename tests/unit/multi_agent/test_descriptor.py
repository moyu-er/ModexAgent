from __future__ import annotations

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
        assert cfg.max_tokens is None
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
        addr = AgentAddress(kind="agent", name="reviewer", role="code_reviewer", capabilities=["python"])
        desc = AgentDescriptor(
            address=addr,
            allowed_tools=["read_file", "write_file"],
            denied_tools=["bash"],
            allowed_skills=["refactor"],
            max_iterations=5,
            execution_strategy="single_turn",
            context_strategy="ephemeral",
            allowed_callers=["planner"],
        )
        assert desc.address == addr
        assert desc.allowed_tools == ["read_file", "write_file"]
        assert desc.denied_tools == ["bash"]
        assert desc.allowed_skills == ["refactor"]
        assert desc.max_iterations == 5
        assert desc.execution_strategy == "single_turn"
        assert desc.context_strategy == "ephemeral"
        assert desc.allowed_callers == ["planner"]


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
