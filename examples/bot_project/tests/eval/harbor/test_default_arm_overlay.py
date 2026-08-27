from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Final

import pytest
from bot.eval.harbor.eval_overlay import (
    EvalAgentOverlay,
    EvalArmOverlay,
    EvalPoolOverlay,
    EvalSystemPromptOverlay,
    load_eval_arm,
)

from modex_agent.scope import (
    AgentOverlay,
    PoolOverlay,
    ScopeOverlay,
    apply_scope_overlay,
    load_scope_declaration,
)
from modex_agent.scope.spec import MemoryDeclaration

_BOT_PROJECT: Final = Path(__file__).resolve().parents[3]
# process/terminal/experience stay TOOL-slot registered; send_file_to_user
# left the shipped declaration roster (baf4ad5f) so eval arms no longer
# remove it — tools_remove must reference registered tools only.
_REGISTERED_TOOL_NAMES: Final = frozenset(
    {"process", "terminal", "experience"}
)


def test_eval_overlay_loader_and_arm_file_exist() -> None:
    assert importlib.util.find_spec("bot.eval.harbor.eval_overlay") is not None
    assert (_BOT_PROJECT / "config" / "scopes" / "eval" / "eval.yml").is_file()


def test_eval_arm_schema_mirrors_framework_overlay_with_only_pool_sugar() -> None:
    assert set(EvalAgentOverlay.model_fields) == set(AgentOverlay.model_fields)
    assert set(EvalPoolOverlay.model_fields) == {
        *PoolOverlay.model_fields,
        "single_agent",
        "tools_remove",
        "memory",
        "system_prompt",
        "strip_mcp",
    }
    assert set(EvalArmOverlay.model_fields) == set(ScopeOverlay.model_fields)


def test_checked_in_arms_keep_default_and_benchmark_semantics_separate() -> None:
    path = _BOT_PROJECT / "config" / "scopes" / "eval" / "eval.yml"
    default = load_eval_arm(path, "default").to_scope_overlay(
        "default", "default", _REGISTERED_TOOL_NAMES
    )
    benchmark = load_eval_arm(path, "benchmark").to_scope_overlay(
        "default", "default", _REGISTERED_TOOL_NAMES
    )
    assert default == ScopeOverlay(
        strip_peers=True,
        pools={
            "default": PoolOverlay(
                agents={
                    "default": AgentOverlay(
                        tools=["-experience"],
                        strip_mcp=True,
                    )
                }
            )
        },
    )
    assert benchmark.strip_peers is True
    benchmark_pool = benchmark.pools["default"]
    # No single_agent sugar: the benchmark arm inherits the target pool's
    # subagent topology and keeps only its own deviations.
    assert benchmark_pool.keep_agents is None
    benchmark_root = benchmark_pool.agents["default"]
    assert benchmark_root.tools == [
        "-process",
        "-terminal",
        "-experience",
    ]
    assert benchmark_root.memory == MemoryDeclaration(core_enabled=False)
    assert benchmark_root.system_prompt_provider == "file_prompt"
    assert benchmark_root.system_prompt_provider_config == {"path": "agents/benchmark.md"}
    assert benchmark_root.strip_mcp is True


def test_pool_sugar_expands_to_framework_keep_agents_and_minus_tools() -> None:
    arm = EvalArmOverlay(
        pools={"default": EvalPoolOverlay(single_agent=True, tools_remove=["process", "terminal"])}
    )
    overlay = arm.to_scope_overlay("default", "default", _REGISTERED_TOOL_NAMES)
    assert overlay.pools["default"].keep_agents == ["default"]
    assert overlay.pools["default"].agents["default"].tools == ["-process", "-terminal"]


def test_target_pool_sugar_expands_memory_and_prompt_onto_selected_root() -> None:
    arm = EvalArmOverlay(
        strip_peers=True,
        pools={
            "target_pool": EvalPoolOverlay(
                single_agent=True,
                tools_remove=["process", "terminal"],
                memory=MemoryDeclaration(core_enabled=False),
                system_prompt=EvalSystemPromptOverlay(
                    provider="file_prompt",
                    path="agents/benchmark.md",
                ),
            )
        },
    )

    overlay = arm.to_scope_overlay("coder", "orchestrator", _REGISTERED_TOOL_NAMES)

    assert set(overlay.pools) == {"coder"}
    pool = overlay.pools["coder"]
    assert pool.keep_agents == ["orchestrator"]
    root = pool.agents["orchestrator"]
    assert root.tools == ["-process", "-terminal"]
    assert root.memory == MemoryDeclaration(core_enabled=False)
    assert root.system_prompt_provider == "file_prompt"
    assert root.system_prompt_provider_config == {"path": "agents/benchmark.md"}


def test_tools_remove_rejects_unregistered_tool_name() -> None:
    arm = EvalArmOverlay(pools={"target_pool": EvalPoolOverlay(tools_remove=["nonexistent_tool"])})

    with pytest.raises(ValueError, match="tools_remove.*nonexistent_tool"):
        arm.to_scope_overlay("default", "default", _REGISTERED_TOOL_NAMES)


def test_eval_overlay_rejects_unknown_arm_name() -> None:
    path = _BOT_PROJECT / "config" / "scopes" / "eval" / "eval.yml"
    with pytest.raises(ValueError, match="unknown eval arm"):
        load_eval_arm(path, "nonexistent")


def test_eval_overlay_nonexistent_pool_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "eval.yml"
    path.write_text(
        "arms:\n  default:\n    pools:\n      nonexistent: {}\n",
        encoding="utf-8",
    )
    spec = load_scope_declaration(_BOT_PROJECT / "config" / "scopes" / "bot.yml")
    overlay = load_eval_arm(path, "default").to_scope_overlay(
        "default", "default", _REGISTERED_TOOL_NAMES
    )
    with pytest.raises(ValueError, match="unknown pool.*nonexistent"):
        apply_scope_overlay(spec, overlay)


def test_eval_overlay_keep_agents_missing_root_fails_loudly() -> None:
    spec = load_scope_declaration(_BOT_PROJECT / "config" / "scopes" / "bot.yml")
    overlay = EvalArmOverlay(
        pools={"default": EvalPoolOverlay(keep_agents=["office-expert"])}
    ).to_scope_overlay("default", "default", _REGISTERED_TOOL_NAMES)

    with pytest.raises(ValueError, match="cannot drop root agent 'default'"):
        apply_scope_overlay(spec, overlay)
