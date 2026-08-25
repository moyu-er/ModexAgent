from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from bot.eval.harbor.eval_overlay import EvalArmOverlay, EvalPoolOverlay
from bot.eval.harbor.pool_mode import PoolModeConfig, PoolModeDependencies, execute_pool_entry

from modex_agent.scope import apply_scope_overlay, load_scope_declaration
from modex_agent.tools.terminal.persistent_bash import (
    BashInputTool,
    PersistentBashTool,
    persistent_bash_supported,
)
from modex_agent.tools.terminal.subprocess_tool import SubprocessTool
from modex_agent.tools.workspace_scoped import WorkspaceScopedShellTool

from .test_convergence_characterization import (
    BENCHMARK_BASH_IDENTITY,
    BENCHMARK_MEMORY_DUMP,
    BENCHMARK_ORDERED_TOOLS_CORRECTED,
    DEFAULT_ARM_LIVE_PROMPT,
    DEFAULT_ARM_ORDERED_TOOLS,
    DEFAULT_MEMORY_DUMP,
    BashIdentity,
    _assembled_pool,
    _memory_dump,
)
from .test_pool_mode_assembly import (
    _benchmark_dependencies,
    _benchmark_environment,
    _BenchmarkProvider,
    _execute_and_capture_assembly,
)
from .test_pool_mode_entry import (
    _environment,
    _pricebook,
    _ProviderFactory,
    _ScriptedProvider,
)

_BOT_PROJECT = Path(__file__).resolve().parents[3]
_REGISTERED_TOOL_NAMES = frozenset({"process", "terminal"})


@pytest.mark.asyncio
async def test_benchmark_arm_matches_split_brain_pins(tmp_path: Path) -> None:
    async with _assembled_pool(tmp_path, benchmark=True) as (config, assembly, instance):
        expected_tools = list(BENCHMARK_ORDERED_TOOLS_CORRECTED)
        if sys.platform == "win32":
            expected_tools.remove("bash_input")
        assert instance.tool_manager.list_tools() == expected_tools

        bash = instance.tool_manager.get_tool("bash")
        bash_input = instance.tool_manager.get_tool("bash_input")
        if sys.platform == "win32":
            # CI-gated platform rung: no POSIX pty — bash degrades to the
            # stateless shell, wrapped workspace-scoped by the root provider.
            assert type(bash) is WorkspaceScopedShellTool
            assert type(bash._inner) is SubprocessTool
            assert bash_input is None
        else:
            assert isinstance(bash, PersistentBashTool)
            assert isinstance(bash_input, BashInputTool)
            assert BashIdentity(
                class_name=type(bash).__name__,
                timeout_seconds=bash.manager.timeout_seconds,
                max_output_chars=bash.manager.max_output_chars,
                initial_cwd_is_task_workspace=(
                    bash.manager._initial_cwd == str(config.entry.task_workspace.resolve())
                ),
                bash_input_shares_manager=bash_input.manager is bash.manager,
            ) == BENCHMARK_BASH_IDENTITY

        assert _memory_dump(assembly) == BENCHMARK_MEMORY_DUMP
        benchmark_prompt = (_BOT_PROJECT / "agents" / "benchmark.md").read_text(
            encoding="utf-8"
        )
        root = instance.pool.get(instance.root_agent_name)
        assert root is not None
        assert assembly.pool_data.context_manager.base_system_prompt == benchmark_prompt
        assert root.descriptor.system_prompt_template == benchmark_prompt
        assert assembly.declared.root.spec.system_prompt_provider == "file_prompt"
        assert assembly.declared.root.spec.system_prompt_config == {
            "path": "agents/benchmark.md"
        }


@pytest.mark.asyncio
async def test_default_arm_pins_remain_green(tmp_path: Path) -> None:
    async with _assembled_pool(tmp_path, benchmark=False) as (_config, assembly, instance):
        expected_default = [
            name for name in DEFAULT_ARM_ORDERED_TOOLS if name != "send_to_peer"
        ]
        if sys.platform == "win32":
            expected_default.remove("bash_input")
        assert instance.tool_manager.list_tools() == expected_default
        assert _memory_dump(assembly) == DEFAULT_MEMORY_DUMP
        assert assembly.pool_data.context_manager.base_system_prompt == DEFAULT_ARM_LIVE_PROMPT


def test_benchmark_tools_remove_unknown_name_fails_loudly() -> None:
    arm = EvalArmOverlay(
        pools={"target_pool": EvalPoolOverlay(tools_remove=["bogus-benchmark-tool"])}
    )

    with pytest.raises(ValueError, match="tools_remove.*bogus-benchmark-tool"):
        arm.to_scope_overlay("default", "default", _REGISTERED_TOOL_NAMES)


def test_benchmark_keep_agents_missing_root_fails_loudly() -> None:
    spec = load_scope_declaration(_BOT_PROJECT / "config" / "scopes" / "bot.yml")
    overlay = EvalArmOverlay(
        pools={"default": EvalPoolOverlay(keep_agents=["office-expert"])}
    ).to_scope_overlay("default", "default", _REGISTERED_TOOL_NAMES)

    with pytest.raises(ValueError, match="cannot drop root agent 'default'"):
        apply_scope_overlay(spec, overlay)


@pytest.mark.asyncio
async def test_benchmark_teardown_closes_fallback_bash_once(tmp_path: Path) -> None:
    config = PoolModeConfig.from_environment(_benchmark_environment(tmp_path))
    close_calls: list[PersistentBashTool] = []
    real_close = PersistentBashTool.close

    async def count_close(tool: PersistentBashTool) -> None:
        close_calls.append(tool)
        await real_close(tool)

    with patch.object(PersistentBashTool, "close", count_close):
        _pool_kwargs, _build_call, _outcome, instance = await _execute_and_capture_assembly(
            config, _benchmark_dependencies(_BenchmarkProvider())
        )

    bash = instance.tool_manager.get_tool("bash")
    if persistent_bash_supported():
        assert isinstance(bash, PersistentBashTool)
        assert close_calls.count(bash) == 1
    else:
        assert type(bash) is WorkspaceScopedShellTool
        assert type(bash._inner) is SubprocessTool
        assert close_calls == []


@pytest.mark.asyncio
async def test_benchmark_trials_isolate_data_dirs(tmp_path: Path) -> None:
    data_dirs: list[Path] = []
    child_sessions: list[tuple[str, ...]] = []
    for index, item_id in enumerate(("benchmark-a", "benchmark-b")):
        trial_root = tmp_path / item_id
        trial_root.mkdir()
        environment = _environment(trial_root)
        environment["MODEX_EXPERIMENT_ITEM_ID"] = item_id
        environment["MODEX_EVAL_ROSTER"] = "benchmark"
        environment["MODEX_AGENT_OUTPUT_DIR"] = str(tmp_path / f"agent-logs-{index}")
        config = PoolModeConfig.from_environment(environment)
        data_dirs.append(config.data_dir)
        outcome = await execute_pool_entry(
            config,
            PoolModeDependencies(
                provider_factory=_ProviderFactory(_ScriptedProvider()),
                pricebook=_pricebook(),
            ),
        )
        child_sessions.append(outcome.child_sessions)

    assert data_dirs[0] != data_dirs[1]
    assert all(data_dir.is_dir() for data_dir in data_dirs)
    assert child_sessions == [(), ()]
