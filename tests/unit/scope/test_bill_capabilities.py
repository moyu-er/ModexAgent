from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from modex_agent.plugins.abc import PluginSource
from modex_agent.plugins.assembly.context import AgentContext as AssemblyAgentContext
from modex_agent.plugins.capability import (
    AgentDeclarationView,
    Capability,
    CapabilityBinding,
    CapabilityContribution,
    CapabilityWiring,
    DerivedToolOrigin,
    DerivedToolSpec,
    FinalRosterView,
    PromptSectionSpec,
    TreePositionView,
)
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.scope import (
    AgentSpec,
    CapabilityContributionKind,
    CapabilityGateResult,
    CapabilityState,
    CompiledAgent,
    PoolSpec,
    ScopeKind,
    ScopeSpec,
    ToolOrigin,
    compile_scope,
)
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths


class _BillCapability(Capability):
    def __init__(
        self,
        name: str,
        *,
        auto: bool,
        contribution: CapabilityContribution,
        drops_non_tools: bool = False,
    ) -> None:
        self.name = name
        self._auto = auto
        self._contribution = contribution
        self._drops_non_tools = drops_non_tools

    def applies(self, view: AgentDeclarationView) -> bool:
        return self._auto and view.is_root

    def contribute(self, tree: TreePositionView, config: BaseModel) -> CapabilityContribution:
        return self._contribution

    def bind(
        self, tree: TreePositionView, config: BaseModel, final: FinalRosterView
    ) -> CapabilityBinding:
        if self._drops_non_tools:
            return CapabilityBinding()
        return super().bind(tree, config, final)

    async def assemble(
        self, binding: CapabilityBinding, ctx: AssemblyAgentContext
    ) -> CapabilityWiring:
        return CapabilityWiring()


def _register(
    registry: ComponentRegistry,
    capability: Capability,
    source: PluginSource,
) -> None:
    registration = PluginRegistrationContext(registry, source=source)
    registration.register_capability(capability.name, capability)
    registration.flush()


def _compile(
    tmp_path: Path,
    root: AgentSpec,
    registry: ComponentRegistry,
    *,
    child: AgentSpec | None = None,
) -> CompiledAgent:
    agents = [root]
    if child is not None:
        agents.append(child)
    scope = ScopeSpec(
        kind=ScopeKind.POOL,
        pool=PoolSpec(name="audit", agents=agents),
    )
    workspace = WorkspaceContext(
        target=tmp_path,
        paths=WorkspacePaths(root=tmp_path),
        is_home=False,
    )
    return compile_scope(scope, workspace_ctx=workspace, registry=registry).agents[0]


def test_three_states_carry_registration_sources_and_vouched_contributions(
    tmp_path: Path,
) -> None:
    registry = ComponentRegistry()
    _register(
        registry,
        _BillCapability(
            "auto_cap",
            auto=True,
            contribution=CapabilityContribution(
                tools=("auto_tool",),
                hooks=("auto_hook",),
                sections=(PromptSectionSpec(section_id="auto_cap.section", order=10),),
            ),
        ),
        PluginSource.PROJECT,
    )
    _register(
        registry,
        _BillCapability(
            "declared_cap",
            auto=False,
            contribution=CapabilityContribution(tools=("declared_tool",)),
        ),
        PluginSource.USER,
    )
    _register(
        registry,
        _BillCapability(
            "vetoed_cap",
            auto=True,
            contribution=CapabilityContribution(tools=("vetoed_tool",)),
        ),
        PluginSource.ENTRY_POINTS,
    )

    compiled = _compile(
        tmp_path,
        AgentSpec(
            name="root",
            capabilities={"declared_cap": {}, "vetoed_cap": False},
        ),
        registry,
    )

    capabilities = {entry.capability: entry for entry in compiled.provenance.capabilities}
    assert list(capabilities) == ["auto_cap", "declared_cap", "vetoed_cap"]
    assert capabilities["auto_cap"].state is CapabilityState.AUTO
    assert capabilities["auto_cap"].registration_source is PluginSource.PROJECT
    assert capabilities["declared_cap"].state is CapabilityState.DECLARED
    assert capabilities["declared_cap"].registration_source is PluginSource.USER
    assert capabilities["vetoed_cap"].state is CapabilityState.VETOED
    assert capabilities["vetoed_cap"].registration_source is PluginSource.ENTRY_POINTS
    assert capabilities["vetoed_cap"].contributions == []

    assert [
        (entry.kind, entry.name, entry.gate) for entry in capabilities["auto_cap"].contributions
    ] == [
        (CapabilityContributionKind.TOOL, "auto_tool", CapabilityGateResult.VOUCHED),
        (CapabilityContributionKind.HOOK, "auto_hook", CapabilityGateResult.VOUCHED),
        (
            CapabilityContributionKind.SECTION,
            "auto_cap.section",
            CapabilityGateResult.VOUCHED,
        ),
    ]
    tools = {entry.tool: entry for entry in compiled.provenance.tools}
    assert tools["auto_tool"].origin is ToolOrigin.CAPABILITY_DERIVED
    assert tools["auto_tool"].capability == "auto_cap"
    assert tools["declared_tool"].origin is ToolOrigin.CAPABILITY_DERIVED
    assert tools["declared_tool"].capability == "declared_cap"
    assert "vetoed_tool" not in tools


def test_contribution_gating_reports_component_vetoes(tmp_path: Path) -> None:
    registry = ComponentRegistry()
    _register(
        registry,
        _BillCapability(
            "drop_cap",
            auto=False,
            drops_non_tools=True,
            contribution=CapabilityContribution(
                tools=("drop_tool",),
                hooks=("drop_hook",),
                sections=(PromptSectionSpec(section_id="drop_cap.section", order=20),),
            ),
        ),
        PluginSource.PROJECT,
    )

    compiled = _compile(
        tmp_path,
        AgentSpec(
            name="root",
            capabilities={"drop_cap": {}},
            tools=["-drop_tool"],
            hooks=["-drop_hook"],
        ),
        registry,
    )

    capability = compiled.provenance.capabilities[0]
    assert capability.state is CapabilityState.DECLARED
    assert [(entry.kind, entry.name, entry.gate) for entry in capability.contributions] == [
        (CapabilityContributionKind.TOOL, "drop_tool", CapabilityGateResult.DROPPED),
        (CapabilityContributionKind.HOOK, "drop_hook", CapabilityGateResult.DROPPED),
        (
            CapabilityContributionKind.SECTION,
            "drop_cap.section",
            CapabilityGateResult.DROPPED,
        ),
    ]


def test_derived_tool_keeps_richer_per_kind_origin(tmp_path: Path) -> None:
    registry = ComponentRegistry()
    _register(
        registry,
        _BillCapability(
            "tree_cap",
            auto=True,
            contribution=CapabilityContribution(
                derived_tools=(
                    DerivedToolSpec(
                        tool="task",
                        origin=DerivedToolOrigin.DERIVED_TASK,
                        targets=("child",),
                    ),
                ),
            ),
        ),
        PluginSource.PROJECT,
    )

    compiled = _compile(
        tmp_path,
        AgentSpec(name="root"),
        registry,
        child=AgentSpec(name="child", parent="root"),
    )

    task = next(entry for entry in compiled.provenance.tools if entry.tool == "task")
    assert task.origin is ToolOrigin.DERIVED_TASK
    assert task.targets == ["child"]
