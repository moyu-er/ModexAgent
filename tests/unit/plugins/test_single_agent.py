"""Declared single-agent assembly tests.

This seam absorbs the useful construction capability that the removed legacy
stage never provided. Its speculative v2 per-invocation pipeline contract and
identity-only builder tests are intentionally not ported.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.provider import LLMProvider
from modex_agent.core.tool_manager import Tool, ToolResult
from modex_agent.hook import Hook
from modex_agent.memory.context_governance import CompositeGovernance
from modex_agent.multi_agent.factory import DefaultAgentFactory
from modex_agent.plugins.assembly.single_agent import (
    SingleAgentInfra,
    assemble_declared_single_agent,
)
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.scope.compiler import CompiledAgent, compile_scope
from modex_agent.scope.defaults import defaults_for_position, memory_config_for_position
from modex_agent.scope.spec import (
    AgentSpec,
    MemoryDeclaration,
    PoolSpec,
    ScopeKind,
    ScopeSpec,
)
from modex_agent.tools.presets import ToolPreset
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths


class _ProbeHook(Hook):
    pass


class _WrappedTool(Tool):
    def __init__(self, inner: Tool) -> None:
        super().__init__()
        self._inner = inner

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def description(self) -> str:
        return self._inner.description

    @property
    def parameters(self) -> dict[str, object]:
        return self._inner.parameters

    async def execute(self, **kwargs: object) -> ToolResult | str:
        return await self._inner.execute(**kwargs)


def _compiled(tmp_path: Path, agent: AgentSpec) -> CompiledAgent:
    workspace = WorkspaceContext(
        target=tmp_path,
        paths=WorkspacePaths(root=tmp_path / "data"),
        is_home=False,
    )
    declaration = ScopeSpec(
        kind=ScopeKind.POOL,
        pool=PoolSpec(name="standalone", agents=[agent]),
    )
    return compile_scope(declaration, workspace_ctx=workspace).agents[0]


def _registry() -> ComponentRegistry:
    registry = ComponentRegistry()
    with PluginRegistrationContext(registry) as registration:
        DefaultPlugin().register(registration)
    return registry


def _infra(
    *,
    governance_enabled: bool = True,
    extra_hooks: tuple[Hook, ...] = (),
    tool_wrapper=None,
) -> SingleAgentInfra:
    return SingleAgentInfra(
        llm_provider=MagicMock(spec=LLMProvider),
        safety=RuntimeSafetyPolicy(),
        root_provider=None,
        governance_enabled=governance_enabled,
        extra_hooks=extra_hooks,
        tool_wrapper=tool_wrapper,
    )


class TestRootMemoryFamily:
    async def test_declared_archive_and_core_layers_are_enabled(self, tmp_path: Path) -> None:
        compiled = _compiled(
            tmp_path,
            AgentSpec(
                name="solo",
                tools=[],
                memory=MemoryDeclaration(archive_enabled=True, core_enabled=True),
            ),
        )

        assembled = await assemble_declared_single_agent(
            compiled,
            _infra(),
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            component_registry=_registry(),
        )

        assert assembled.descriptor.memory_config.archive is not None
        assert assembled.descriptor.memory_config.archive.enabled
        assert assembled.descriptor.memory_config.core is not None
        assert assembled.descriptor.memory_config.core.enabled

    async def test_undeclared_memory_uses_root_position_default(self, tmp_path: Path) -> None:
        compiled = _compiled(tmp_path, AgentSpec(name="solo", tools=[]))

        assembled = await assemble_declared_single_agent(
            compiled,
            _infra(),
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            component_registry=_registry(),
        )

        expected = memory_config_for_position(defaults_for_position(is_root=True))
        assert assembled.descriptor.memory_config == expected


class TestDeclaredPrompt:
    async def test_file_prompt_becomes_memory_base_prompt(self, tmp_path: Path) -> None:
        prompt_path = tmp_path / "agents" / "solo.md"
        prompt_path.parent.mkdir()
        prompt_path.write_text("<declared-system-prompt/>", encoding="utf-8")
        compiled = _compiled(
            tmp_path,
            AgentSpec(
                name="solo",
                tools=[],
                system_prompt_provider="file_prompt",
                system_prompt_provider_config={"path": "agents/solo.md"},
            ),
        )

        assembled = await assemble_declared_single_agent(
            compiled,
            _infra(),
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            component_registry=_registry(),
        )

        assert assembled.context_manager.base_system_prompt == prompt_path.read_text(
            encoding="utf-8"
        )


class TestDeclaredTools:
    async def test_read_write_uses_platform_bash_ladder(self, tmp_path: Path) -> None:
        compiled = _compiled(
            tmp_path,
            AgentSpec(name="solo", toolset=ToolPreset.READ_WRITE),
        )

        assembled = await assemble_declared_single_agent(
            compiled,
            _infra(),
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            component_registry=_registry(),
        )

        bash = assembled.tool_manager.get_tool("bash")
        if sys.platform == "win32":
            assert type(bash).__name__ == "SubprocessTool"
        else:
            assert type(bash).__name__ == "PersistentBashTool"


class TestGovernanceDerivation:
    async def test_enabled_governance_uses_memory_config(self, tmp_path: Path) -> None:
        compiled = _compiled(tmp_path, AgentSpec(name="solo", tools=[]))

        assembled = await assemble_declared_single_agent(
            compiled,
            _infra(governance_enabled=True),
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            component_registry=_registry(),
        )

        assert assembled.instance.pipeline is not None
        builder = assembled.instance.pipeline._turn_context_builder
        assert builder is not None
        assert isinstance(builder.governance, CompositeGovernance)

    async def test_governance_can_be_disabled(self, tmp_path: Path) -> None:
        compiled = _compiled(tmp_path, AgentSpec(name="solo", tools=[]))

        assembled = await assemble_declared_single_agent(
            compiled,
            _infra(governance_enabled=False),
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            component_registry=_registry(),
        )

        assert assembled.instance.pipeline is not None
        builder = assembled.instance.pipeline._turn_context_builder
        assert builder is not None
        assert builder.governance is None


class TestInfraSwitches:
    async def test_extra_hooks_land_on_pipeline_runner(self, tmp_path: Path) -> None:
        hook = _ProbeHook()
        compiled = _compiled(tmp_path, AgentSpec(name="solo", tools=[]))

        assembled = await assemble_declared_single_agent(
            compiled,
            _infra(extra_hooks=(hook,)),
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            component_registry=_registry(),
        )

        assert assembled.instance.pipeline is not None
        assert hook in [item.hook for item in assembled.instance.pipeline.hook_runner.hook_specs]

    async def test_tool_wrapper_is_applied_during_registration(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        compiled = _compiled(tmp_path, AgentSpec(name="solo", tools=["read"]))
        events: list[str] = []
        original_create_agent = DefaultAgentFactory.create_agent

        def wrap(tool: Tool) -> Tool:
            events.append(f"wrap:{tool.name}")
            return _WrappedTool(tool)

        async def observe_create_agent(self, *args, **kwargs):
            events.append("factory")
            return await original_create_agent(self, *args, **kwargs)

        monkeypatch.setattr(DefaultAgentFactory, "create_agent", observe_create_agent)

        assembled = await assemble_declared_single_agent(
            compiled,
            _infra(tool_wrapper=wrap),
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            component_registry=_registry(),
        )

        assert isinstance(assembled.tool_manager.get_tool("read"), _WrappedTool)
        assert events.index("wrap:read") < events.index("factory")

    async def test_poolless_assembly_completes_without_resident_registry(
        self, tmp_path: Path
    ) -> None:
        compiled = _compiled(tmp_path, AgentSpec(name="solo", tools=[]))

        assembled = await assemble_declared_single_agent(
            compiled,
            _infra(),
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            component_registry=_registry(),
        )

        assert assembled.instance.descriptor is assembled.descriptor
