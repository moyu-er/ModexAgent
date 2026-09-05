"""TDD tests for the Capability protocol (task 1, capability-bundles).

Written FIRST, drives the implementation of
``src/modex_agent/plugins/capability.py`` + the ``CAPABILITY`` slot +
the ``register_capability`` registration face (ADR-0047 / SPEC §4).
Covers:

- ``ComponentSlot`` — exactly 11 members including ``CAPABILITY``.
- ``Capability`` defaults — ``applies`` False, ``contribute`` empty,
  ``bind`` pass-through (the contribution IS the binding), ``supply``
  None; ``config_model`` defaults to the shared empty frozen
  ``CapabilityConfig``; only ``assemble`` is abstract.
- ``register_capability`` — resolve round-trip returns the same
  instance, unregistered name raises ``ComponentNotFoundError``,
  duplicate raises ``ValueError``, cross-source conflicts resolve by
  source priority (same semantics as ``register_tool``).
- Frozen-model discipline — field assignment rejected, extra fields
  rejected at construction.
"""

from __future__ import annotations

from abc import ABC
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from modex_agent.plugins.abc import ComponentSlot, PluginSource
from modex_agent.plugins.capability import (
    AgentDeclarationView,
    AgentDeclaredFields,
    Capability,
    CapabilityBinding,
    CapabilityConfig,
    CapabilityContribution,
    CapabilitySupply,
    CapabilityWiring,
    ChildSummary,
    FinalRosterView,
    PoolSupplyView,
    PromptSectionSpec,
    SectionPlacement,
    TreePositionView,
)
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentNotFoundError, ComponentRegistry

# ---- Test helpers --------------------------------------------------------


class _MinimalCapability(Capability):
    """Concrete capability implementing ONLY the abstract assemble()."""

    name = "minimal"

    async def assemble(self, binding: CapabilityBinding, ctx: object) -> CapabilityWiring:
        return CapabilityWiring()


class _SectionCapability(Capability):
    """Capability contributing one tool, one hook, one section."""

    name = "sections"

    def contribute(self, tree: TreePositionView, config: BaseModel) -> CapabilityContribution:
        return CapabilityContribution(
            tools=("demo_tool",),
            hooks=("demo_hook",),
            sections=(PromptSectionSpec(section_id="sections.guide", order=10),),
        )

    async def assemble(self, binding: CapabilityBinding, ctx: object) -> CapabilityWiring:
        return CapabilityWiring()


class _WiringCapability(Capability):
    """Capability whose assemble() emits a wiring artifact."""

    name = "wiring"

    async def assemble(self, binding: CapabilityBinding, ctx: object) -> CapabilityWiring:
        return CapabilityWiring(artifacts={"store": object()})


class _StoreSupply(CapabilitySupply):
    """Concrete supply for the marker-ABC check."""


_CHILD = ChildSummary(name="child", description="a child agent")

_DECLARED = AgentDeclaredFields()

_VIEW = AgentDeclarationView(
    pool_name="pool",
    agent_name="root",
    is_root=True,
    parent=None,
    children=(_CHILD,),
    peers=("other_root",),
    declared=_DECLARED,
)

_TREE = TreePositionView(
    pool_name="pool",
    agent_name="root",
    is_root=True,
    parent=None,
    children=(_CHILD,),
    peers=("other_root",),
)

_FINAL = FinalRosterView(tools=("read", "write"), hooks=("demo_hook",))

_POOL_VIEW = PoolSupplyView(pool_name="pool", entries=())


# ---- ComponentSlot --------------------------------------------------------


class TestComponentSlot:
    def test_has_exactly_eleven_members_including_capability(self) -> None:
        members = set(ComponentSlot)
        assert len(members) == 11
        assert members == {
            ComponentSlot.TOOL,
            ComponentSlot.HOOK,
            ComponentSlot.MEMORY_SYSTEM,
            ComponentSlot.LLM_PROVIDER,
            ComponentSlot.SYSTEM_PROMPT_PROVIDER,
            ComponentSlot.INTERCEPTOR,
            ComponentSlot.COMMAND_HANDLER,
            ComponentSlot.EXECUTION_STRATEGY,
            ComponentSlot.INPUT_STAGE,
            ComponentSlot.DATA_NAMESPACE,
            ComponentSlot.CAPABILITY,
        }

    def test_capability_member_value(self) -> None:
        assert ComponentSlot.CAPABILITY == "capability"


# ---- Capability defaults ---------------------------------------------------


class TestCapabilityDefaults:
    def test_minimal_subclass_only_implementing_assemble_is_instantiable(self) -> None:
        cap = _MinimalCapability()
        assert cap.name == "minimal"

    def test_config_model_defaults_to_shared_capability_config(self) -> None:
        assert _MinimalCapability.config_model is CapabilityConfig
        with pytest.raises(ValidationError):
            CapabilityConfig(unknown_key=1)  # type: ignore[call-arg]

    def test_applies_default_false(self) -> None:
        assert _MinimalCapability().applies(_VIEW) is False

    def test_contribute_default_empty(self) -> None:
        contribution = _MinimalCapability().contribute(_TREE, CapabilityConfig())
        assert contribution == CapabilityContribution()
        assert contribution.tools == ()
        assert contribution.tool_replacements == ()
        assert contribution.hooks == ()
        assert contribution.sections == ()

    def test_contribute_carries_declared_entries(self) -> None:
        contribution = _SectionCapability().contribute(_TREE, CapabilityConfig())
        assert contribution.tools == ("demo_tool",)
        assert contribution.hooks == ("demo_hook",)
        assert contribution.sections == (
            PromptSectionSpec(section_id="sections.guide", order=10),
        )

    def test_bind_default_pass_through_for_empty_contribution(self) -> None:
        binding = _MinimalCapability().bind(_TREE, CapabilityConfig(), _FINAL)
        assert binding == CapabilityBinding()
        assert binding.active_sections == ()
        assert binding.payload == {}

    def test_bind_default_echoes_contributed_sections(self) -> None:
        # SPEC §4: default bind has no anchor — the contribution IS the
        # binding, so contributed sections pass through unchanged.
        binding = _SectionCapability().bind(_TREE, CapabilityConfig(), _FINAL)
        assert binding.active_sections == (
            PromptSectionSpec(section_id="sections.guide", order=10),
        )
        assert binding.payload == {}

    def test_supply_default_none(self) -> None:
        assert _MinimalCapability().supply(_POOL_VIEW) is None

    async def test_assemble_returns_wiring(self) -> None:
        wiring = await _WiringCapability().assemble(CapabilityBinding(), None)
        assert wiring.prompt_providers == ()
        assert set(wiring.artifacts) == {"store"}


# ---- CapabilitySupply ------------------------------------------------------


class TestCapabilitySupply:
    def test_is_a_marker_abc_subclassable_and_instantiable(self) -> None:
        assert issubclass(CapabilitySupply, ABC)
        supply = _StoreSupply()
        assert isinstance(supply, CapabilitySupply)


# ---- AgentDeclaredFields ---------------------------------------------------


class TestAgentDeclaredFields:
    def test_defaults_mirror_agent_spec_defaults(self) -> None:
        fields = AgentDeclaredFields()
        assert fields.toolset is None
        assert fields.tools is None
        assert fields.hooks is None
        assert fields.mcp == []
        assert fields.use_terminal is False
        assert fields.execution_strategy == "react"
        assert fields.provider_kind is None
        assert fields.eager is None
        assert fields.roles == []
        assert fields.description == ""

    def test_carries_declared_values(self) -> None:
        fields = AgentDeclaredFields(
            toolset="full",
            tools=["read", "write"],
            hooks=["demo_hook"],
            mcp=["server_a"],
            use_terminal=True,
            execution_strategy="external",
            provider_kind="opencode",
            eager=True,
            roles=["dev"],
            description="does things",
        )
        assert fields.toolset == "full"
        assert fields.tools == ["read", "write"]
        assert fields.hooks == ["demo_hook"]
        assert fields.mcp == ["server_a"]
        assert fields.use_terminal is True
        assert fields.execution_strategy == "external"
        assert fields.provider_kind == "opencode"
        assert fields.eager is True
        assert fields.roles == ["dev"]
        assert fields.description == "does things"


# ---- Frozen-model discipline -----------------------------------------------


class TestFrozenModelDiscipline:
    def test_field_assignment_rejected(self) -> None:
        section = PromptSectionSpec(section_id="cap.section", order=10)
        with pytest.raises(ValidationError):
            section.order = 99
        with pytest.raises(ValidationError):
            _VIEW.pool_name = "other"

    def test_prompt_section_placement_defaults_to_head(self) -> None:
        section = PromptSectionSpec(section_id="cap.section", order=10)

        assert section.placement is SectionPlacement.HEAD

    def test_prompt_section_accepts_tail_placement(self) -> None:
        section = PromptSectionSpec(
            section_id="cap.section",
            order=10,
            placement=SectionPlacement.TAIL,
        )

        assert section.placement is SectionPlacement.TAIL

    def test_extra_fields_rejected_at_construction(self) -> None:
        with pytest.raises(ValidationError):
            PromptSectionSpec(section_id="x", order=1, bogus=1)  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            AgentDeclaredFields(toolset="full", bogus=True)  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            CapabilityBinding(payload={}, bogus=1)  # type: ignore[call-arg]


# ---- register_capability + registry resolution ------------------------------


class TestRegisterCapability:
    def test_register_and_resolve_roundtrip_returns_same_instance(self) -> None:
        registry = ComponentRegistry()
        capability = _MinimalCapability()
        ctx = PluginRegistrationContext(registry)
        ctx.register_capability("minimal", capability)
        ctx.flush()

        resolved: Any = registry.resolve(ComponentSlot.CAPABILITY, "minimal")
        assert resolved is capability
        assert registry.names(ComponentSlot.CAPABILITY) == ("minimal",)

    def test_resolve_unregistered_name_raises_not_found(self) -> None:
        registry = ComponentRegistry()
        with pytest.raises(ComponentNotFoundError) as exc_info:
            registry.resolve(ComponentSlot.CAPABILITY, "ghost")
        assert exc_info.value.name == "ghost"
        assert exc_info.value.slot == ComponentSlot.CAPABILITY

    def test_same_source_duplicate_raises_value_error(self) -> None:
        registry = ComponentRegistry()
        ctx = PluginRegistrationContext(registry, source=PluginSource.BUNDLED)
        ctx.register_capability("dup", _MinimalCapability())
        ctx.register_capability("dup", _MinimalCapability())
        with pytest.raises(ValueError, match="dup.*same-source conflict"):
            ctx.flush()

    def test_direct_duplicate_registration_raises_value_error(self) -> None:
        registry = ComponentRegistry()
        registry.register(
            ComponentSlot.CAPABILITY,
            "dup",
            _MinimalCapability(),  # type: ignore[arg-type]
        )
        with pytest.raises(ValueError, match="already registered"):
            registry.register(
                ComponentSlot.CAPABILITY,
                "dup",
                _MinimalCapability(),  # type: ignore[arg-type]
            )

    def test_cross_source_duplicate_resolves_by_source_priority(self) -> None:
        # Same O2 bookkeeping as register_tool: user > bundled.
        registry = ComponentRegistry()
        bundled = _MinimalCapability()
        user = _MinimalCapability()

        bundled_ctx = PluginRegistrationContext(registry, source=PluginSource.BUNDLED)
        bundled_ctx.register_capability("shared", bundled)
        bundled_ctx.flush()

        user_ctx = PluginRegistrationContext(registry, source=PluginSource.USER)
        user_ctx.register_capability("shared", user)
        user_ctx.flush()

        resolved: Any = registry.resolve(ComponentSlot.CAPABILITY, "shared")
        assert resolved is user
