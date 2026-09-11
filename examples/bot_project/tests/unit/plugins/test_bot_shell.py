from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

BOT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BOT_ROOT))

from modex_agent.core.agent import ExecutionStrategyKind, ProviderKind  # noqa: E402
from modex_agent.plugins.abc import ComponentSlot  # noqa: E402
from modex_agent.plugins.defaults import DefaultPlugin  # noqa: E402
from modex_agent.plugins.loader import (  # noqa: E402
    ComponentRegistryLoader,
    PluginDiscoveryConfig,
    PluginSource,
)
from modex_agent.plugins.registry import ComponentRegistry  # noqa: E402
from modex_agent.scope.compiler import compile_scope  # noqa: E402
from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec  # noqa: E402
from modex_agent.tools.presets import ToolPreset  # noqa: E402
from modex_agent.workspace.context import WorkspaceContext  # noqa: E402
from modex_agent.workspace.paths import WorkspacePaths  # noqa: E402


async def _registry() -> ComponentRegistry:
    registry = ComponentRegistry()
    await ComponentRegistryLoader.load(
        registry,
        PluginDiscoveryConfig(
            bundled_factories=(DefaultPlugin(),),
            project_plugin_paths=(BOT_ROOT / "plugins",),
        ),
    )
    return registry


def _workspace(tmp_path: Path) -> WorkspaceContext:
    return WorkspaceContext(
        target=tmp_path,
        paths=WorkspacePaths(root=tmp_path / ".modex"),
        is_home=False,
    )


@pytest.mark.parametrize(
    ("toolset", "tools", "expected"),
    [
        (None, None, True),
        (ToolPreset.FULL, None, True),
        (ToolPreset.READ_WRITE, None, True),
        (ToolPreset.READ_ONLY, None, True),
        (ToolPreset.NONE, None, False),
        (ToolPreset.WEB, None, False),
        (ToolPreset.NONE, ["+bash"], True),
        (ToolPreset.WEB, ["+bash"], True),
        (ToolPreset.NONE, ["bash", "+read"], False),
        (ToolPreset.NONE, ["bash", "read"], True),
        (None, ["-bash"], False),
    ],
)
async def test_bot_shell_auto_application_follows_tool_selection(
    tmp_path: Path,
    toolset: ToolPreset | None,
    tools: list[str] | None,
    expected: bool,
) -> None:
    shell = importlib.import_module(
        "modex_agent.plugins.defaults.capabilities.shell"
    )
    registry = await _registry()
    compiled = compile_scope(
        ScopeSpec(
            kind=ScopeKind.POOL,
            pool=PoolSpec(
                name="pool",
                agents=[AgentSpec(name="root", toolset=toolset, tools=tools)],
            ),
        ),
        workspace_ctx=_workspace(tmp_path),
        registry=registry,
    ).agents[0]

    names = [entry.name for entry in compiled.spec.tools]
    assert ("bash" in names) is expected
    assert bool(compiled.spec.tool_groups) is expected
    if expected:
        assert tuple(
            variant.name for variant in compiled.spec.tool_groups[0].variants
        ) == ("subprocess", "persistent")
        assert tuple(
            variant.name for variant in shell.SHELL_TOOL_GROUP_SPEC.variants
        ) == ("subprocess", "persistent", "terminal")


async def test_bot_shell_plugin_overrides_only_the_bundled_capability() -> None:
    registry = await _registry()

    assert (
        registry.registration_source(ComponentSlot.CAPABILITY, "shell")
        is PluginSource.PROJECT
    )
    assert registry.registration_source(ComponentSlot.TOOL, "bash") is PluginSource.BUNDLED


async def test_bot_position_defaults_auto_apply_shell_to_root_and_subagent(
    tmp_path: Path,
) -> None:
    registry = await _registry()
    compilation = compile_scope(
        ScopeSpec(
            kind=ScopeKind.POOL,
            pool=PoolSpec(
                name="pool",
                agents=[
                    AgentSpec(name="root"),
                    AgentSpec(name="worker", parent="root"),
                ],
            ),
        ),
        workspace_ctx=_workspace(tmp_path),
        registry=registry,
    )

    assert [
        [entry.name for entry in compiled.spec.tools].count("bash")
        for compiled in compilation.agents
    ] == [1, 1]
    assert all(compiled.spec.tool_groups for compiled in compilation.agents)


async def test_bot_shell_explicit_false_vetoes_auto_application(
    tmp_path: Path,
) -> None:
    registry = await _registry()
    compiled = compile_scope(
        ScopeSpec(
            kind=ScopeKind.POOL,
            pool=PoolSpec(
                name="pool",
                agents=[
                    AgentSpec(name="root", capabilities={"shell": False})
                ],
            ),
        ),
        workspace_ctx=_workspace(tmp_path),
        registry=registry,
    ).agents[0]

    assert "bash" not in [entry.name for entry in compiled.spec.tools]
    assert compiled.spec.tool_groups == ()


async def test_bot_shell_auto_application_is_structurally_excluded_from_external_agents(
    tmp_path: Path,
) -> None:
    registry = await _registry()
    compiled = compile_scope(
        ScopeSpec(
            kind=ScopeKind.POOL,
            pool=PoolSpec(
                name="pool",
                agents=[
                    AgentSpec(
                        name="external",
                        execution_strategy=ExecutionStrategyKind.EXTERNAL,
                        provider_kind=ProviderKind.OPENCODE,
                    )
                ],
            ),
        ),
        workspace_ctx=_workspace(tmp_path),
        registry=registry,
    ).agents[0]

    assert "bash" not in [entry.name for entry in compiled.spec.tools]
    assert compiled.spec.capabilities == ()
    assert compiled.spec.tool_groups == ()
