"""Declared single-agent assembly tests.

This seam absorbs the useful construction capability that the removed legacy
stage never provided. Its speculative v2 per-invocation pipeline contract and
identity-only builder tests are intentionally not ported.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel, ConfigDict

from modex_agent.commands.handlers import SkillCommandHandler
from modex_agent.commands.models import CommandContext, SlashCommandInvocation
from modex_agent.core.context import InMemoryContextManager
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.provider import LLMProvider
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import Tool, ToolResult
from modex_agent.core.types import InputMessage
from modex_agent.hook import Hook
from modex_agent.memory.context_governance import CompositeGovernance
from modex_agent.multi_agent.factory import DefaultAgentFactory
from modex_agent.plugins.abc import ComponentSlot, SimpleFactory
from modex_agent.plugins.assembly.single_agent import (
    SingleAgentInfra,
    assemble_declared_single_agent,
)
from modex_agent.plugins.capability import (
    Capability,
    CapabilityBinding,
    CapabilityWiring,
    PoolSupplyView,
)
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.defaults.capabilities.skills import (
    SKILLS_CAPABILITY_NAME,
    SkillsSupply,
)
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


class _EmptyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


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


class _StandaloneLifecycleSupply(SkillsSupply):
    def __init__(self, events: list[str]) -> None:
        super().__init__(pool_name="standalone", catalog_by_agent={})
        self._events = events

    async def start(self) -> None:
        self._events.append("start")

    async def stop(self) -> None:
        self._events.append("stop")


class _StandaloneLifecycleCapability(Capability):
    name = SKILLS_CAPABILITY_NAME

    def __init__(self, events: list[str]) -> None:
        self._events = events

    def supply(self, view: PoolSupplyView) -> SkillsSupply:
        self._events.append("construct")
        return _StandaloneLifecycleSupply(self._events)

    async def assemble(self, binding: CapabilityBinding, ctx: object) -> CapabilityWiring:
        return CapabilityWiring()


def _compiled(
    tmp_path: Path,
    agent: AgentSpec,
    registry: ComponentRegistry,
) -> CompiledAgent:
    workspace = WorkspaceContext(
        target=tmp_path,
        paths=WorkspacePaths(root=tmp_path / "data"),
        is_home=False,
    )
    declaration = ScopeSpec(
        kind=ScopeKind.POOL,
        pool=PoolSpec(name="standalone", agents=[agent]),
    )
    return compile_scope(declaration, workspace_ctx=workspace, registry=registry).agents[0]


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
        registry = _registry()
        compiled = _compiled(
            tmp_path,
            AgentSpec(
                name="solo",
                tools=[],
                memory=MemoryDeclaration(archive_enabled=True, core_enabled=True),
            ),
            registry,
        )

        assembled = await assemble_declared_single_agent(
            compiled,
            _infra(),
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            component_registry=registry,
        )

        assert assembled.descriptor.memory_config.archive is not None
        assert assembled.descriptor.memory_config.archive.enabled
        assert assembled.descriptor.memory_config.core is not None
        assert assembled.descriptor.memory_config.core.enabled

    async def test_undeclared_memory_uses_root_position_default(self, tmp_path: Path) -> None:
        registry = _registry()
        compiled = _compiled(tmp_path, AgentSpec(name="solo", tools=[]), registry)

        assembled = await assemble_declared_single_agent(
            compiled,
            _infra(),
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            component_registry=registry,
        )

        expected = memory_config_for_position(defaults_for_position(is_root=True))
        assert assembled.descriptor.memory_config == expected


class TestDeclaredPrompt:
    async def test_file_prompt_becomes_memory_base_prompt(self, tmp_path: Path) -> None:
        prompt_path = tmp_path / "agents" / "solo.md"
        prompt_path.parent.mkdir()
        prompt_path.write_text("<declared-system-prompt/>", encoding="utf-8")
        registry = _registry()
        compiled = _compiled(
            tmp_path,
            AgentSpec(
                name="solo",
                tools=[],
                system_prompt_provider="file_prompt",
                system_prompt_provider_config={"path": "agents/solo.md"},
            ),
            registry,
        )

        assembled = await assemble_declared_single_agent(
            compiled,
            _infra(),
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            component_registry=registry,
        )

        assert assembled.context_manager.base_system_prompt == prompt_path.read_text(
            encoding="utf-8"
        )


class TestDeclaredTools:
    async def test_read_write_uses_platform_bash_ladder(self, tmp_path: Path) -> None:
        registry = _registry()
        compiled = _compiled(
            tmp_path,
            AgentSpec(name="solo", toolset=ToolPreset.READ_WRITE),
            registry,
        )

        assembled = await assemble_declared_single_agent(
            compiled,
            _infra(),
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            component_registry=registry,
        )

        bash = assembled.tool_manager.get_tool("bash")
        if sys.platform == "win32":
            assert type(bash).__name__ == "SubprocessTool"
        else:
            assert type(bash).__name__ == "PersistentBashTool"


class TestGovernanceDerivation:
    async def test_enabled_governance_uses_memory_config(self, tmp_path: Path) -> None:
        registry = _registry()
        compiled = _compiled(tmp_path, AgentSpec(name="solo", tools=[]), registry)

        assembled = await assemble_declared_single_agent(
            compiled,
            _infra(governance_enabled=True),
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            component_registry=registry,
        )

        assert assembled.instance.pipeline is not None
        builder = assembled.instance.pipeline._turn_context_builder
        assert builder is not None
        assert isinstance(builder.governance, CompositeGovernance)

    async def test_governance_can_be_disabled(self, tmp_path: Path) -> None:
        registry = _registry()
        compiled = _compiled(tmp_path, AgentSpec(name="solo", tools=[]), registry)

        assembled = await assemble_declared_single_agent(
            compiled,
            _infra(governance_enabled=False),
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            component_registry=registry,
        )

        assert assembled.instance.pipeline is not None
        builder = assembled.instance.pipeline._turn_context_builder
        assert builder is not None
        assert builder.governance is None


class TestInfraSwitches:
    async def test_extra_hooks_land_on_pipeline_runner(self, tmp_path: Path) -> None:
        hook = _ProbeHook()
        registry = _registry()
        compiled = _compiled(tmp_path, AgentSpec(name="solo", tools=[]), registry)

        assembled = await assemble_declared_single_agent(
            compiled,
            _infra(extra_hooks=(hook,)),
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            component_registry=registry,
        )

        assert assembled.instance.pipeline is not None
        assert hook in [item.hook for item in assembled.instance.pipeline.hook_runner.hook_specs]

    async def test_tool_wrapper_is_applied_during_registration(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        registry = _registry()
        compiled = _compiled(tmp_path, AgentSpec(name="solo", tools=["read"]), registry)
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
            component_registry=registry,
        )

        assert isinstance(assembled.tool_manager.get_tool("read"), _WrappedTool)
        assert events.index("wrap:read") < events.index("factory")

    async def test_poolless_assembly_completes_without_resident_registry(
        self, tmp_path: Path
    ) -> None:
        registry = _registry()
        compiled = _compiled(tmp_path, AgentSpec(name="solo", tools=[]), registry)

        assembled = await assemble_declared_single_agent(
            compiled,
            _infra(),
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            component_registry=registry,
        )

        assert assembled.instance.descriptor is assembled.descriptor

    async def test_post_start_failure_stops_capability_supply(self, tmp_path: Path) -> None:
        events: list[str] = []
        registry = ComponentRegistry()
        with PluginRegistrationContext(registry) as registration:
            registration.register_capability(
                _StandaloneLifecycleCapability.name,
                _StandaloneLifecycleCapability(events),
            )
        compiled = _compiled(
            tmp_path,
            AgentSpec(
                name="solo",
                tools=[],
                capabilities={_StandaloneLifecycleCapability.name: {}},
            ),
            registry,
        )

        with pytest.raises(ValueError, match="no catalog or resolver"):
            await assemble_declared_single_agent(
                compiled,
                _infra(),
                project_dir=tmp_path,
                data_dir=tmp_path / "data",
                component_registry=registry,
            )

        assert events == ["construct", "start", "stop"]


class TestSkillsAssembly:
    @staticmethod
    def _write_skill(tmp_path: Path) -> None:
        skill_dir = tmp_path / "skills" / "solo" / "solo" / "root-only"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: root-only\ndescription: Root disk skill\n---\n\n# Root Only",
            encoding="utf-8",
        )

    async def test_auto_applied_disk_skill_reaches_prompt_and_direct_command(
        self, tmp_path: Path
    ) -> None:
        self._write_skill(tmp_path)
        registry = _registry()
        compiled = _compiled(tmp_path, AgentSpec(name="solo", tools=[]), registry)

        assembled = await assemble_declared_single_agent(
            compiled,
            _infra(),
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            component_registry=registry,
        )
        try:
            pipeline = assembled.instance.pipeline
            assert pipeline is not None
            assert pipeline.skill_resolver is not None
            resolved = await pipeline.skill_resolver.resolve_command("root-only", "")
            assert resolved is not None

            state = await assembled.context_manager.load(
                "s1",
                tool_manager=assembled.tool_manager,
            )
            assert state.system_prompt_pipeline is not None
            prompt = await state.system_prompt_pipeline.get_or_refresh()
            assert "<available_skills>" in prompt
            assert "root-only" in prompt

            command = await SkillCommandHandler().handle(
                SlashCommandInvocation(
                    command="root-only",
                    args="",
                    raw="/root-only",
                ),
                CommandContext(
                    session_id="s1",
                    input_msg=InputMessage(
                        content="/root-only",
                        session=SessionInfo.from_str("s1"),
                    ),
                    agent_name="solo",
                    skill_resolver=pipeline.skill_resolver,
                ),
            )
            assert command.user_content == resolved.xml
        finally:
            await assembled.close()

    async def test_explicit_false_excludes_prompt_and_command_resolver(
        self, tmp_path: Path
    ) -> None:
        self._write_skill(tmp_path)
        registry = _registry()
        compiled = _compiled(
            tmp_path,
            AgentSpec(
                name="solo",
                tools=[],
                capabilities={"skills": False},
            ),
            registry,
        )

        assembled = await assemble_declared_single_agent(
            compiled,
            _infra(),
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            component_registry=registry,
        )
        try:
            pipeline = assembled.instance.pipeline
            assert pipeline is not None
            assert pipeline.skill_resolver is None
            state = await assembled.context_manager.load(
                "s1",
                tool_manager=assembled.tool_manager,
            )
            assert state.system_prompt_pipeline is not None
            prompt = await state.system_prompt_pipeline.get_or_refresh()
            assert "<available_skills>" not in prompt
            assert "root-only" not in prompt
        finally:
            await assembled.close()

    async def test_custom_memory_system_owns_prompt_but_keeps_disk_resolver(
        self, tmp_path: Path
    ) -> None:
        self._write_skill(tmp_path)
        registry = _registry()
        custom_memory = InMemoryContextManager(base_system_prompt="<custom-memory-system/>")
        registry.register(
            ComponentSlot.MEMORY_SYSTEM,
            "custom",
            SimpleFactory(custom_memory, _EmptyConfig),
        )
        compiled = _compiled(
            tmp_path,
            AgentSpec(name="solo", tools=[], memory_system="custom"),
            registry,
        )

        assembled = await assemble_declared_single_agent(
            compiled,
            _infra(),
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            component_registry=registry,
        )
        try:
            assert assembled.instance.context_manager is custom_memory
            state = await custom_memory.load("s1", tool_manager=assembled.tool_manager)
            assert state.system_prompt == "<custom-memory-system/>"
            assert "<available_skills>" not in state.system_prompt
            assert "root-only" not in state.system_prompt

            pipeline = assembled.instance.pipeline
            assert pipeline is not None
            assert pipeline.skill_resolver is not None
            resolved = await pipeline.skill_resolver.resolve_command("root-only", "")
            assert resolved is not None
            assert "# Root Only" in resolved.xml
        finally:
            await assembled.close()
