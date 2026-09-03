"""TDD tests for the capability compile protocol (task 3, capability-bundles).

Written FIRST, drives the C0/C1/C2 insertion into ``compile_scope`` + the
``registry`` parameter + ``AssemblySpec.capabilities`` (ADR-0047 / SPEC §6).
The compiler knows ONLY the protocol — every capability below is a test
double. Covers:

- C0 enablement matrix — auto-only (predicate True), declared-only
  (predicate False + override mapping), override-``false`` disabling an
  auto-applied capability, override config replacing the default config.
- External agents — predicates never invoked; a non-empty declared block
  is a loud V12 error (defense in depth behind the phase-1 validator).
- C1 contribution — contributed tool/hook names enter the merge BASE, so
  ``tools: [-x]`` / ``hooks: [-y]`` veto them.
- C2 binding — ``bind`` receives the post-merge ``FinalRosterView``; the
  binding lands in ``AssemblySpec.capabilities``; ``CapabilityError``
  propagates as a boot failure.
- V13 — a declared unregistered name raises ``ComponentNotFoundError``.
- Config validation — an unknown config key raises ``ValidationError``.
- ``registry=None`` — byte-identical to an empty-registry compile when
  nothing is declared; a loud ``ValueError`` when capabilities are
  declared (never silently ignored).
- Determinism — same spec + same registry → identical ``spec_hash``;
  capability order is the registry enumeration order, not set iteration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from modex_agent.core.agent import ExecutionStrategyKind, ProviderKind
from modex_agent.plugins.abc import ComponentSlot
from modex_agent.plugins.capability import (
    AgentDeclarationView,
    Capability,
    CapabilityBinding,
    CapabilityContribution,
    CapabilityError,
    CapabilityWiring,
    CompiledCapability,
    FinalRosterView,
    TreePositionView,
)
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentNotFoundError, ComponentRegistry
from modex_agent.scope import (
    POSITION_DEFAULT_HOOKS,
    AgentSpec,
    PoolSpec,
    ScopeKind,
    ScopeSpec,
    compile_scope,
    spec_hash,
)
from modex_agent.tools.presets import ToolPreset
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

# ---- Test doubles ----------------------------------------------------------


class _BundleCapability(Capability):
    """Configurable test bundle recording every protocol call."""

    def __init__(
        self,
        name: str = "bundle",
        *,
        applies_to: bool = False,
        tools: tuple[str, ...] = (),
        hooks: tuple[str, ...] = (),
        bind_payload: dict[str, Any] | None = None,
        bind_drops: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self._applies_to = applies_to
        self._tools = tools
        self._hooks = hooks
        self._bind_payload = bind_payload or {}
        self._bind_drops = bind_drops
        self.applies_views: list[AgentDeclarationView] = []
        self.contribute_calls: list[tuple[TreePositionView, BaseModel]] = []
        self.bind_calls: list[tuple[TreePositionView, BaseModel, FinalRosterView]] = []

    def applies(self, view: AgentDeclarationView) -> bool:
        self.applies_views.append(view)
        return self._applies_to

    def contribute(self, tree: TreePositionView, config: BaseModel) -> CapabilityContribution:
        self.contribute_calls.append((tree, config))
        return CapabilityContribution(tools=self._tools, hooks=self._hooks)

    def bind(
        self, tree: TreePositionView, config: BaseModel, final: FinalRosterView
    ) -> CapabilityBinding:
        self.bind_calls.append((tree, config, final))
        vouched = tuple(h for h in self._hooks if h not in self._bind_drops)
        return CapabilityBinding(payload=dict(self._bind_payload), hooks=vouched)

    async def assemble(self, binding: CapabilityBinding, ctx: object) -> CapabilityWiring:
        return CapabilityWiring()


class _DefaultBindHookCapability(Capability):
    """Contributes a hook but does NOT override ``bind`` — exercises the
    protocol's default "contribution IS the binding" path, which must
    carry the contributed hooks through as vouched (T13)."""

    name = "default_bind_hook"

    def contribute(self, tree: TreePositionView, config: BaseModel) -> CapabilityContribution:
        del tree, config
        return CapabilityContribution(hooks=("cap_hook",))

    async def assemble(self, binding: CapabilityBinding, ctx: object) -> CapabilityWiring:
        return CapabilityWiring()


class _KnobConfig(BaseModel):
    """Config model with one knob (frozen, extra="forbid")."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    limit: int = 3


class _KnobCapability(Capability):
    """Capability with a config_model, recording the validated configs."""

    name = "knob"
    config_model = _KnobConfig

    def __init__(self) -> None:
        self.received_configs: list[BaseModel] = []

    def contribute(self, tree: TreePositionView, config: BaseModel) -> CapabilityContribution:
        self.received_configs.append(config)
        return CapabilityContribution()

    async def assemble(self, binding: CapabilityBinding, ctx: object) -> CapabilityWiring:
        return CapabilityWiring()


class _AnchorCapability(Capability):
    """Capability whose bind() raises CapabilityError when its anchor dies."""

    name = "anchored"

    def contribute(self, tree: TreePositionView, config: BaseModel) -> CapabilityContribution:
        return CapabilityContribution(tools=("anchor_tool",))

    def bind(
        self, tree: TreePositionView, config: BaseModel, final: FinalRosterView
    ) -> CapabilityBinding:
        if "anchor_tool" not in final.tools:
            raise CapabilityError(
                f"pool {tree.pool_name!r} agent {tree.agent_name!r}: capability "
                f"{self.name!r} requires tool 'anchor_tool' — a tools veto "
                "dismantled the anchor"
            )
        return CapabilityBinding()

    async def assemble(self, binding: CapabilityBinding, ctx: object) -> CapabilityWiring:
        return CapabilityWiring()


# ---- Helpers ----------------------------------------------------------------


def _workspace_ctx() -> WorkspaceContext:
    path = Path("/tmp/test_compiler_capability_protocol_ws")
    return WorkspaceContext(target=path, paths=WorkspacePaths(root=path), is_home=False)


def _registry(*capabilities: Capability) -> ComponentRegistry:
    registry = ComponentRegistry()
    ctx = PluginRegistrationContext(registry)
    for capability in capabilities:
        ctx.register_capability(capability.name, capability)
    ctx.flush()
    return registry


def _tree(
    *,
    root: AgentSpec | None = None,
    sub: AgentSpec | None = None,
) -> ScopeSpec:
    """The minimal two-agent tree (native root + sub)."""
    return ScopeSpec(
        kind=ScopeKind.POOL,
        pool=PoolSpec(
            name="p",
            agents=[root or AgentSpec(name="root"), sub or AgentSpec(name="sub", parent="root")],
        ),
    )


def _external_tree(*, capabilities: dict[str, Any] | None = None) -> ScopeSpec:
    """A single external (pi) root agent pool."""
    return ScopeSpec(
        kind=ScopeKind.POOL,
        pool=PoolSpec(
            name="p",
            agents=[
                AgentSpec(
                    name="root",
                    execution_strategy=ExecutionStrategyKind.EXTERNAL,
                    provider_kind=ProviderKind.PI,
                    capabilities=capabilities,
                )
            ],
        ),
    )


def _compile(spec: ScopeSpec, registry: ComponentRegistry | None = None) -> Any:
    return compile_scope(spec, workspace_ctx=_workspace_ctx(), registry=registry)


# ---- C0 enablement matrix -----------------------------------------------------


class TestC0EnablementMatrix:
    def test_auto_only_enable(self) -> None:
        capability = _BundleCapability(applies_to=True, tools=("cap_tool",))
        compilation = _compile(_tree(), _registry(capability))
        root = compilation.agents[0]
        assert "cap_tool" in root.spec.tools
        assert [cap.name for cap in root.spec.capabilities] == ["bundle"]

    def test_declared_only_enable(self) -> None:
        capability = _BundleCapability(applies_to=False, tools=("cap_tool",))
        spec = _tree(root=AgentSpec(name="root", capabilities={"bundle": {}}))
        compilation = _compile(spec, _registry(capability))
        root = compilation.agents[0]
        assert "cap_tool" in root.spec.tools
        assert [cap.name for cap in root.spec.capabilities] == ["bundle"]

    def test_override_false_disables_auto(self) -> None:
        capability = _BundleCapability(applies_to=True, tools=("cap_tool",))
        spec = _tree(root=AgentSpec(name="root", capabilities={"bundle": False}))
        compilation = _compile(spec, _registry(capability))
        root = compilation.agents[0]
        assert "cap_tool" not in root.spec.tools
        assert root.spec.capabilities == ()
        # the override disabled the ROOT only — the sub agent (no
        # override) still auto-applies, so its calls are not the root's
        assert all(tree.agent_name != "root" for tree, _ in capability.contribute_calls)
        assert all(tree.agent_name != "root" for tree, _config, _final in capability.bind_calls)

    def test_override_config_replaces_default_config(self) -> None:
        capability = _KnobCapability()
        spec = _tree(root=AgentSpec(name="root", capabilities={"knob": {"limit": 5}}))
        compilation = _compile(spec, _registry(capability))
        compiled = compilation.agents[0].spec.capabilities[0]
        assert isinstance(compiled, CompiledCapability)
        assert compiled.config == {"limit": 5}
        # contribute received the VALIDATED instance carrying the override
        received = capability.received_configs[0]
        assert isinstance(received, _KnobConfig)
        assert received.limit == 5

    def test_default_config_when_enabled_without_knobs(self) -> None:
        capability = _KnobCapability()
        spec = _tree(root=AgentSpec(name="root", capabilities={"knob": {}}))
        compilation = _compile(spec, _registry(capability))
        compiled = compilation.agents[0].spec.capabilities[0]
        assert compiled.config == {"limit": 3}

    def test_declaration_view_carries_declared_fields(self) -> None:
        capability = _BundleCapability(applies_to=True)
        spec = _tree(
            root=AgentSpec(
                name="root", use_terminal=True, toolset=ToolPreset.READ_ONLY, mcp=["srv"]
            )
        )
        _compile(spec, _registry(capability))
        view = capability.applies_views[0]
        assert view.declared.use_terminal is True
        assert view.declared.toolset == "read_only"
        assert view.declared.mcp == ["srv"]
        # tree facts: the root of pool "p" with one direct child, no peers
        assert (view.pool_name, view.agent_name, view.is_root, view.parent) == (
            "p",
            "root",
            True,
            None,
        )
        assert [child.name for child in view.children] == ["sub"]
        assert view.peers == ()


# ---- External agents ----------------------------------------------------------


class TestExternalAgents:
    def test_predicates_never_invoked(self) -> None:
        capability = _BundleCapability(applies_to=True)
        compilation = _compile(_external_tree(), _registry(capability))
        assert capability.applies_views == []
        assert compilation.agents[0].spec.capabilities == ()

    def test_declared_non_empty_raises_loud(self) -> None:
        capability = _BundleCapability()
        spec = _external_tree(capabilities={"bundle": {}})
        with pytest.raises(ValueError, match="V12"):
            _compile(spec, _registry(capability))


# ---- C1 contribution ----------------------------------------------------------


class TestC1Contribution:
    def test_contributed_tool_enters_merge_base_and_is_vetoable(self) -> None:
        capability = _BundleCapability(applies_to=False, tools=("cap_tool",))
        spec = _tree(root=AgentSpec(name="root", capabilities={"bundle": {}}, tools=["-cap_tool"]))
        compilation = _compile(spec, _registry(capability))
        assert "cap_tool" not in compilation.agents[0].spec.tools

    def test_contributed_tool_present_without_veto(self) -> None:
        capability = _BundleCapability(applies_to=False, tools=("cap_tool",))
        spec = _tree(root=AgentSpec(name="root", capabilities={"bundle": {}}))
        compilation = _compile(spec, _registry(capability))
        assert "cap_tool" in compilation.agents[0].spec.tools

    def test_contributed_hook_enters_merge_base_and_is_vetoable(self) -> None:
        capability = _BundleCapability(applies_to=False, hooks=("cap_hook",))
        spec = _tree(root=AgentSpec(name="root", capabilities={"bundle": {}}, hooks=["-cap_hook"]))
        compilation = _compile(spec, _registry(capability))
        assert "cap_hook" not in compilation.agents[0].spec.hooks

    def test_contributed_hook_present_without_veto(self) -> None:
        capability = _BundleCapability(applies_to=False, hooks=("cap_hook",))
        spec = _tree(root=AgentSpec(name="root", capabilities={"bundle": {}}))
        compilation = _compile(spec, _registry(capability))
        assert "cap_hook" in compilation.agents[0].spec.hooks

    def test_sub_agent_tree_view_facts(self) -> None:
        capability = _BundleCapability()
        spec = _tree(sub=AgentSpec(name="sub", parent="root", capabilities={"bundle": {}}))
        _compile(spec, _registry(capability))
        tree, _config = capability.contribute_calls[0]
        assert (tree.agent_name, tree.is_root, tree.parent) == ("sub", False, "root")
        assert tree.children == ()


# ---- C2 binding ---------------------------------------------------------------


class TestC2Binding:
    def test_bind_receives_final_roster_view(self) -> None:
        capability = _BundleCapability(applies_to=False, tools=("cap_tool",), hooks=("cap_hook",))
        spec = _tree(root=AgentSpec(name="root", capabilities={"bundle": {}}))
        compilation = _compile(spec, _registry(capability))
        root = compilation.agents[0]
        _tree_view, _config, final = capability.bind_calls[0]
        assert final.tools == tuple(root.spec.tools)
        assert final.hooks == tuple(root.spec.hooks)
        assert "cap_tool" in final.tools
        assert "cap_hook" in final.hooks

    def test_binding_lands_in_assembly_spec_capabilities(self) -> None:
        capability = _BundleCapability(applies_to=False, bind_payload={"marker": "wired"})
        spec = _tree(root=AgentSpec(name="root", capabilities={"bundle": {}}))
        compilation = _compile(spec, _registry(capability))
        compiled = compilation.agents[0].spec.capabilities[0]
        assert compiled.name == "bundle"
        assert compiled.binding.payload == {"marker": "wired"}

    def test_capability_error_propagates_as_boot_failure(self) -> None:
        capability = _AnchorCapability()
        spec = _tree(
            root=AgentSpec(name="root", capabilities={"anchored": {}}, tools=["-anchor_tool"])
        )
        with pytest.raises(CapabilityError, match="anchor_tool"):
            _compile(spec, _registry(capability))


# ---- Binding-declared hook gating (T13) ----------------------------------------


class TestBindingDeclaredHookGating:
    """The binding is the authority for the hooks a capability CONTRIBUTED:
    after C2, a contributed name whose capability's binding does not vouch
    for it is removed from merged_hooks (generic post-merge anchor gating —
    the compiler stays capability-agnostic). Handwritten roster entries are
    never gated; gating only removes, never adds."""

    def test_contributed_hook_removed_when_binding_drops_it(self) -> None:
        capability = _BundleCapability(hooks=("cap_hook",), bind_drops=("cap_hook",))
        spec = _tree(root=AgentSpec(name="root", capabilities={"bundle": {}}))
        compilation = _compile(spec, _registry(capability))
        assert "cap_hook" not in compilation.agents[0].spec.hooks

    def test_contributed_hook_kept_when_binding_vouches(self) -> None:
        capability = _BundleCapability(hooks=("cap_hook",), bind_drops=())
        spec = _tree(root=AgentSpec(name="root", capabilities={"bundle": {}}))
        compilation = _compile(spec, _registry(capability))
        assert "cap_hook" in compilation.agents[0].spec.hooks

    def test_vouched_hooks_land_in_compile_product(self) -> None:
        capability = _BundleCapability(hooks=("cap_hook",), bind_drops=())
        spec = _tree(root=AgentSpec(name="root", capabilities={"bundle": {}}))
        compilation = _compile(spec, _registry(capability))
        compiled = compilation.agents[0].spec.capabilities[0]
        assert isinstance(compiled, CompiledCapability)
        assert compiled.binding.hooks == ("cap_hook",)

    def test_handwritten_same_name_entry_survives_gating(self) -> None:
        capability = _BundleCapability(hooks=("cap_hook",), bind_drops=("cap_hook",))
        spec = _tree(root=AgentSpec(name="root", capabilities={"bundle": {}}, hooks=["+cap_hook"]))
        compilation = _compile(spec, _registry(capability))
        assert compilation.agents[0].spec.hooks == [*POSITION_DEFAULT_HOOKS, "cap_hook"]

    def test_purely_handwritten_entry_without_capability_untouched(self) -> None:
        capability = _BundleCapability(hooks=("cap_hook",), bind_drops=("cap_hook",))
        spec = _tree(root=AgentSpec(name="root", hooks=["+cap_hook"]))
        compilation = _compile(spec, _registry(capability))
        assert compilation.agents[0].spec.hooks == [*POSITION_DEFAULT_HOOKS, "cap_hook"]

    def test_minus_vetoed_contributed_hook_stays_out_when_vouched(self) -> None:
        capability = _BundleCapability(hooks=("cap_hook",), bind_drops=())
        spec = _tree(root=AgentSpec(name="root", capabilities={"bundle": {}}, hooks=["-cap_hook"]))
        compilation = _compile(spec, _registry(capability))
        assert "cap_hook" not in compilation.agents[0].spec.hooks

    def test_default_bind_vouches_all_contributed_hooks(self) -> None:
        capability = _DefaultBindHookCapability()
        spec = _tree(root=AgentSpec(name="root", capabilities={"default_bind_hook": {}}))
        compilation = _compile(spec, _registry(capability))
        assert "cap_hook" in compilation.agents[0].spec.hooks
        compiled = compilation.agents[0].spec.capabilities[0]
        assert compiled.binding.hooks == ("cap_hook",)

    def test_ungated_hooks_of_non_contributing_capability_unaffected(self) -> None:
        # A capability that contributed NO hooks must not gate anyone:
        # its empty binding.hooks removes nothing from the roster.
        contributor = _BundleCapability(hooks=("cap_hook",), bind_drops=())
        bystander = _BundleCapability(name="bystander", tools=("other_tool",))
        spec = _tree(root=AgentSpec(name="root", capabilities={"bundle": {}, "bystander": {}}))
        compilation = _compile(spec, _registry(contributor, bystander))
        assert "cap_hook" in compilation.agents[0].spec.hooks


# ---- V13 + config validation ----------------------------------------------------


class TestV13AndConfigValidation:
    def test_unregistered_name_raises_component_not_found(self) -> None:
        spec = _tree(root=AgentSpec(name="root", capabilities={"nope": {}}))
        with pytest.raises(ComponentNotFoundError) as exc_info:
            _compile(spec, _registry())
        assert exc_info.value.name == "nope"
        assert exc_info.value.slot == ComponentSlot.CAPABILITY
        assert "nope" in str(exc_info.value)

    def test_unknown_config_key_raises_validation_error(self) -> None:
        capability = _KnobCapability()
        spec = _tree(root=AgentSpec(name="root", capabilities={"knob": {"bogus": 1}}))
        with pytest.raises(ValidationError):
            _compile(spec, _registry(capability))


# ---- registry=None semantics -----------------------------------------------------


class TestRegistryNone:
    def test_none_matches_empty_registry_and_pre_change_shape(self) -> None:
        spec = _tree()
        none_compilation = _compile(spec)
        empty_compilation = _compile(spec, _registry())
        assert none_compilation == empty_compilation
        assert spec_hash(none_compilation) == spec_hash(empty_compilation)
        # pre-change shape: the capabilities field defaults to ()
        assert all(agent.spec.capabilities == () for agent in none_compilation.agents)

    def test_none_with_declarations_raises_loud(self) -> None:
        spec = _tree(root=AgentSpec(name="root", capabilities={"bundle": {}}))
        with pytest.raises(ValueError, match="registry required"):
            _compile(spec)


# ---- Determinism + ordering --------------------------------------------------------


class TestDeterminismAndOrdering:
    def test_same_spec_same_registry_identical_spec_hash(self) -> None:
        capability = _BundleCapability(applies_to=True, tools=("cap_tool",))
        spec = _tree()
        registry = _registry(capability)
        first = _compile(spec, registry)
        second = _compile(spec, registry)
        assert spec_hash(first) == spec_hash(second)

    def test_capability_order_is_registry_enumeration_order(self) -> None:
        # Registered in REVERSE alphabetical order — the compile product
        # must follow the registry's sorted enumeration, never insertion
        # or set iteration order.
        late = _BundleCapability(name="zzz_late", applies_to=True, tools=("late_tool",))
        early = _BundleCapability(name="aaa_early", applies_to=True, tools=("early_tool",))
        registry = _registry(late, early)
        spec = _tree()
        first = _compile(spec, registry)
        root = first.agents[0]
        assert [cap.name for cap in root.spec.capabilities] == ["aaa_early", "zzz_late"]
        # contribution order follows the same enumeration: early_tool first
        assert root.spec.tools.index("early_tool") < root.spec.tools.index("late_tool")
        second = _compile(spec, registry)
        assert spec_hash(first) == spec_hash(second)


# ---- Registry accessor convergence ----------------------------------------------------


class TestResolveCapabilityAccessor:
    def test_returns_the_registered_instance(self) -> None:
        capability = _BundleCapability()
        registry = _registry(capability)
        assert registry.resolve_capability("bundle") is capability

    def test_unregistered_name_raises_not_found(self) -> None:
        with pytest.raises(ComponentNotFoundError):
            ComponentRegistry().resolve_capability("ghost")
