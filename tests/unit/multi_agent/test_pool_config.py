"""Unit tests for the new pool_config models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_agent.ioc.configs.approval import ApprovalConfig
from modex_agent.multi_agent.pool_config import (
    MainAgentSpec,
    MediaConfig,
    PoolAssemblyDeps,
    PoolSpec,
    SubagentSpec,
)
from modex_agent.tools.presets import (
    ContextMode,
    SystemPromptMode,
    ToolPreset,
    ToolSupplement,
)


class TestSubagentSpec:
    """SubagentSpec boundary tests."""

    def test_subagent_spec_accepts_legal_fields(self) -> None:
        spec = SubagentSpec(
            agent_name="worker",
            description="does work",
            max_steps=60,
            tool_preset=ToolPreset.READ_ONLY,
            tool_supplements=[ToolSupplement.AST_GREP],
            context_mode=ContextMode.FORK,
            mcp=["mcp-server"],
            system_prompt_mode=SystemPromptMode.APPEND,
            fork_max_messages=50,
        )
        assert spec.agent_name == "worker"
        assert spec.description == "does work"
        assert spec.max_steps == 60
        assert spec.tool_preset == ToolPreset.READ_ONLY
        assert spec.tool_supplements == [ToolSupplement.AST_GREP]
        assert spec.context_mode == ContextMode.FORK
        assert spec.mcp == ["mcp-server"]
        assert spec.system_prompt_mode == SystemPromptMode.APPEND
        assert spec.fork_max_messages == 50

    def test_subagent_spec_rejects_approval(self) -> None:
        with pytest.raises(ValidationError) as exc:
            SubagentSpec(
                agent_name="worker",
                approval=ApprovalConfig(),
            )
        assert "approval" in str(exc.value)

    def test_subagent_spec_rejects_experience(self) -> None:
        with pytest.raises(ValidationError) as exc:
            SubagentSpec(
                agent_name="worker",
                experience={"enabled": True},
            )
        assert "experience" in str(exc.value)

    def test_subagent_spec_rejects_unknown_keys(self) -> None:
        with pytest.raises(ValidationError):
            SubagentSpec(agent_name="worker", unknown_key=True)

    def test_subagent_spec_is_frozen(self) -> None:
        spec = SubagentSpec(agent_name="worker")
        with pytest.raises(ValidationError):
            spec.agent_name = "other"

    def test_subagent_spec_defaults(self) -> None:
        spec = SubagentSpec(agent_name="worker")
        assert spec.description == ""
        assert spec.max_steps == 80
        assert spec.tool_preset == ToolPreset.READ_WRITE
        assert spec.tool_supplements == []
        assert spec.context_mode == ContextMode.FRESH
        assert spec.mcp == []
        assert spec.system_prompt_mode == SystemPromptMode.REPLACE
        assert spec.fork_max_messages == 80

    def test_subagent_spec_fork_max_messages_bounds(self) -> None:
        with pytest.raises(ValidationError):
            SubagentSpec(agent_name="worker", fork_max_messages=0)
        with pytest.raises(ValidationError):
            SubagentSpec(agent_name="worker", fork_max_messages=101)


class TestMainAgentSpec:
    """MainAgentSpec boundary tests."""

    def test_main_agent_spec_accepts_approval(self) -> None:
        spec = MainAgentSpec(
            agent_name="main",
            approval=ApprovalConfig(enabled=True),
        )
        assert spec.approval is not None
        assert spec.approval.enabled is True

    def test_main_agent_spec_defaults(self) -> None:
        spec = MainAgentSpec(agent_name="main")
        assert spec.description == ""
        assert spec.max_steps == 100
        assert spec.use_terminal is False
        assert spec.terminal_visibility is False
        assert spec.tool_preset == ToolPreset.FULL
        assert spec.tool_supplements == [ToolSupplement.TODO]
        assert spec.approval is None
        assert spec.mcp == []

    def test_main_agent_spec_rejects_unknown_keys(self) -> None:
        with pytest.raises(ValidationError):
            MainAgentSpec(agent_name="main", unknown_key=True)

    def test_main_agent_spec_is_frozen(self) -> None:
        spec = MainAgentSpec(agent_name="main")
        with pytest.raises(ValidationError):
            spec.agent_name = "other"


class TestPoolSpec:
    """PoolSpec round-trip and boundary tests."""

    def test_pool_spec_round_trip(self) -> None:
        main = MainAgentSpec(agent_name="main")
        sub = SubagentSpec(agent_name="helper")
        pool = PoolSpec(
            name="test-pool",
            main_agent_name="main",
            main=main,
            subagents=[sub],
            peers=["other-pool"],
            restart_required=True,
        )
        assert pool.name == "test-pool"
        assert pool.main_agent_name == "main"
        assert pool.main.agent_name == "main"
        assert len(pool.subagents) == 1
        assert pool.subagents[0].agent_name == "helper"
        assert pool.peers == ["other-pool"]
        assert pool.restart_required is True

    def test_pool_spec_defaults(self) -> None:
        pool = PoolSpec(
            name="test-pool",
            main_agent_name="main",
            main=MainAgentSpec(agent_name="main"),
        )
        assert pool.subagents == []
        assert pool.peers == []
        assert pool.restart_required is False

    def test_pool_spec_rejects_unknown_keys(self) -> None:
        with pytest.raises(ValidationError):
            PoolSpec(
                name="test-pool",
                main_agent_name="main",
                main=MainAgentSpec(agent_name="main"),
                unknown_key=True,
            )

    def test_pool_spec_is_frozen(self) -> None:
        pool = PoolSpec(
            name="test-pool",
            main_agent_name="main",
            main=MainAgentSpec(agent_name="main"),
        )
        with pytest.raises(ValidationError):
            pool.name = "other-pool"


class TestPoolAssemblyDeps:
    """PoolAssemblyDeps value-object tests."""

    def test_default_deps(self) -> None:
        deps = PoolAssemblyDeps()
        assert deps.memory is None
        assert deps.experience is None
        assert isinstance(deps.media, MediaConfig)
        assert deps.media.max_image_bytes == 20 * 1024 * 1024

    def test_deps_is_frozen(self) -> None:
        deps = PoolAssemblyDeps()
        with pytest.raises(ValidationError):
            deps.memory = None

    def test_deps_rejects_unknown_keys(self) -> None:
        with pytest.raises(ValidationError):
            PoolAssemblyDeps(unknown_key=True)
