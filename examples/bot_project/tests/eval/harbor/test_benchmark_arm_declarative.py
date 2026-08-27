from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from bot.eval.harbor.eval_overlay import EvalArmOverlay, EvalPoolOverlay
from bot.eval.harbor.pool_mode import PoolModeConfig, PoolModeDependencies, execute_pool_entry

from modex_agent.scope import apply_scope_overlay, load_scope_declaration
from modex_agent.tools.terminal.persistent_bash import (
    PersistentBashTool,
    persistent_bash_supported,
)
from modex_agent.tools.terminal.subprocess_tool import SubprocessTool
from modex_agent.tools.workspace_scoped import WorkspaceScopedShellTool

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
