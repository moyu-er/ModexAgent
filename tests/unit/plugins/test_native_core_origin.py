"""Assembly-time tool-origin arbitration (spec ``ToolEntry`` → register origin).

The compiler-classified roster origins flow from ``AssemblySpec.tools`` into
``ToolManager.register``, activating the name-slot override policy at
assembly. The pinned shape is the aci ``edit ← aci_edit`` roster: both
entries resolve through the FW registry to tools whose LLM-facing name is
``edit``, and the later CAPABILITY_DERIVED registration displaces the earlier
PRESET one — audited in ``NativeAssemblyResult.tool_overrides``.

Also pins the duplicate-roster equivalence guard: a compiled roster may carry
the same name twice (overlay concatenation); duplicates share the name-keyed
origin and register the slot once instead of tripping the equal-rank guard.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from pydantic import BaseModel, ConfigDict

from modex_agent.core.prompt import SystemPromptProvider
from modex_agent.core.tool_manager import ToolOrigin, ToolOverrideRecord
from modex_agent.hook.runner import HookRunner
from modex_agent.memory.context import InMemoryContextManager
from modex_agent.multi_agent.descriptor import AgentInstance
from modex_agent.multi_agent.factory import AgentFactory
from modex_agent.plugins.abc import AgentType, ComponentSlot
from modex_agent.plugins.assembly.context import AssemblyContext
from modex_agent.plugins.assembly.native_core import (
    LlmDefaults,
    NativeAssemblyInputs,
    NativeAssemblyResult,
    assemble_native_agent,
)
from modex_agent.plugins.assembly.spec import AssemblySpec, MemoryOverrides, ToolEntry
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry, SimpleFactory
from modex_agent.tools.aci.edit_tool import AciEditTool
from modex_agent.tools.standard.file_tool import EditFileTool
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths


class _EmptyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _StaticPromptProvider(SystemPromptProvider):
    async def _fetch_version(self) -> str:
        return "v1"

    async def _fetch_content(self) -> str:
        return "prompt"


def _registry() -> ComponentRegistry:
    """The FW default registry — real ``edit`` / ``aci_edit`` factories."""
    registry = ComponentRegistry()
    ctx = PluginRegistrationContext(registry)
    DefaultPlugin().register(ctx)
    ctx.flush()
    # Keep the assembly hermetic: the default ``file_prompt`` provider reads
    # from disk; a static provider keeps this test about tool origins only.
    registry.register(
        ComponentSlot.SYSTEM_PROMPT_PROVIDER,
        "static_prompt",
        SimpleFactory(_StaticPromptProvider(), _EmptyConfig),
    )
    return registry


def _spec(tools: list[ToolEntry], workspace: WorkspaceContext) -> AssemblySpec:
    return AssemblySpec(
        agent_type=AgentType.native_main,
        agent_name="worker",
        pool_name="pool",
        tools=tools,
        hooks=[],
        llm_provider="default",
        system_prompt_provider="static_prompt",
        system_prompt_config={},
        memory_overrides=MemoryOverrides(),
        execution_strategy="react",
        workspace_ctx=workspace,
    )


def _inputs(workspace: WorkspaceContext) -> NativeAssemblyInputs:
    pipeline = MagicMock()
    pipeline.hook_runner = HookRunner()
    instance = AgentInstance(
        descriptor=MagicMock(),
        context_manager=InMemoryContextManager(base_system_prompt=""),
        pipeline=pipeline,
    )
    agent_factory = MagicMock(spec=AgentFactory)
    agent_factory.create_agent = AsyncMock(return_value=instance)
    return NativeAssemblyInputs(
        agent_factory=agent_factory,
        broker=MagicMock(),
        llm_defaults=LlmDefaults(model="test/model"),
        context_manager=instance.context_manager,
        memory_system=MagicMock(),
        llm_provider=MagicMock(),  # pre-resolved: the LLM slot is not exercised
        project_dir=workspace.target,
    )


def _workspace(tmp_path: Path) -> WorkspaceContext:
    return WorkspaceContext(
        target=tmp_path, paths=WorkspacePaths(root=tmp_path), is_home=False
    )


class TestRosterOriginArbitration:
    async def test_capability_derived_entry_displaces_preset_same_name(
        self, tmp_path: Path
    ) -> None:
        """Roster [edit(PRESET), aci_edit(CAPABILITY_DERIVED)] — both resolve
        to tools NAMED ``edit``; the higher-rank registration wins the slot
        regardless of roster order, and the swap is audited."""
        registry = _registry()
        workspace = _workspace(tmp_path)
        ctx = AssemblyContext(registry=registry, workspace_ctx=workspace)
        spec = _spec(
            [
                ToolEntry(name="edit", origin=ToolOrigin.PRESET),
                ToolEntry(name="aci_edit", origin=ToolOrigin.CAPABILITY_DERIVED),
            ],
            workspace,
        )

        result = await assemble_native_agent(spec, registry, _inputs(workspace), ctx=ctx)

        assert isinstance(result, NativeAssemblyResult)
        winner = result.tool_manager.get_tool("edit")
        assert isinstance(winner, AciEditTool)
        # One name slot: the aci roster entry's tool carries the LLM-facing
        # name "edit", so the manager holds exactly that slot.
        assert result.tool_manager.list_tools() == ["edit"]
        assert result.tool_overrides == (
            ToolOverrideRecord(
                tool_name="edit",
                winner_origin=ToolOrigin.CAPABILITY_DERIVED,
                displaced_origin=ToolOrigin.PRESET,
            ),
        )

    async def test_preset_only_roster_keeps_plain_edit(self, tmp_path: Path) -> None:
        """Without the capability-derived entry the PRESET ``edit`` survives
        and no override is recorded — the no-collision production shape."""
        registry = _registry()
        workspace = _workspace(tmp_path)
        ctx = AssemblyContext(registry=registry, workspace_ctx=workspace)
        spec = _spec([ToolEntry(name="edit", origin=ToolOrigin.PRESET)], workspace)

        result = await assemble_native_agent(spec, registry, _inputs(workspace), ctx=ctx)

        assert isinstance(result.tool_manager.get_tool("edit"), EditFileTool)
        assert result.tool_overrides == ()

    async def test_duplicate_roster_names_register_one_slot(self, tmp_path: Path) -> None:
        """Duplicate names (overlay concatenation) share the name-keyed
        origin — the slot registers once; no equal-rank collision at boot."""
        registry = _registry()
        workspace = _workspace(tmp_path)
        ctx = AssemblyContext(registry=registry, workspace_ctx=workspace)
        spec = _spec(
            [
                ToolEntry(name="read", origin=ToolOrigin.PRESET),
                ToolEntry(name="edit", origin=ToolOrigin.PRESET),
                ToolEntry(name="read", origin=ToolOrigin.PRESET),
            ],
            workspace,
        )

        result = await assemble_native_agent(spec, registry, _inputs(workspace), ctx=ctx)

        assert sorted(result.tool_manager.list_tools()) == ["edit", "read"]
        assert result.tool_overrides == ()


class TestCompileToAssemblyEndToEnd:
    """The aci upgrade through the REAL road: declaration → compile →
    assembly, no hand-built roster.

    Terminal shape of the O3 removal: a declared ``capabilities: {aci: {}}``
    compiles to a roster carrying BOTH entries (``edit`` PRESET +
    ``aci_edit`` CAPABILITY_DERIVED); assembly settles the ``edit`` slot by
    ToolOrigin rank. A ``tools: [-aci_edit]`` veto removes the upgrade
    entry, leaving the plain preset ``edit``.
    """

    @staticmethod
    def _compiled_spec(tmp_path: Path, tools: list[str] | None) -> AssemblySpec:
        from modex_agent.scope.compiler import compile_scope
        from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec

        spec = ScopeSpec(
            kind=ScopeKind.POOL,
            pool=PoolSpec(
                name="p",
                agents=[
                    AgentSpec(
                        name="root",
                        capabilities={"aci": {}, "skills": False},
                        tools=tools,
                    )
                ],
            ),
        )
        compilation = compile_scope(spec, workspace_ctx=_workspace(tmp_path), registry=_registry())
        return compilation.agents[0].spec

    async def test_declared_aci_roster_assembles_aci_edit_into_edit_slot(
        self, tmp_path: Path
    ) -> None:
        registry = _registry()
        workspace = _workspace(tmp_path)
        ctx = AssemblyContext(registry=registry, workspace_ctx=workspace)
        spec = self._compiled_spec(tmp_path, tools=None)

        assert [entry.name for entry in spec.tools].count("edit") == 1
        assert "aci_edit" in [entry.name for entry in spec.tools]

        result = await assemble_native_agent(spec, registry, _inputs(workspace), ctx=ctx)

        assert isinstance(result.tool_manager.get_tool("edit"), AciEditTool)
        assert result.tool_overrides == (
            ToolOverrideRecord(
                tool_name="edit",
                winner_origin=ToolOrigin.CAPABILITY_DERIVED,
                displaced_origin=ToolOrigin.PRESET,
            ),
        )

    async def test_minus_aci_edit_veto_keeps_plain_edit(self, tmp_path: Path) -> None:
        registry = _registry()
        workspace = _workspace(tmp_path)
        ctx = AssemblyContext(registry=registry, workspace_ctx=workspace)
        spec = self._compiled_spec(tmp_path, tools=["-aci_edit"])

        assert "aci_edit" not in [entry.name for entry in spec.tools]

        result = await assemble_native_agent(spec, registry, _inputs(workspace), ctx=ctx)

        assert isinstance(result.tool_manager.get_tool("edit"), EditFileTool)
        assert result.tool_overrides == ()
