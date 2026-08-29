"""The FW-bundled ``experience`` capability — the T13 migration faces.

Covers:

- **Protocol shape** — ``ExperienceCapability`` is a pure opt-in bundle
  contributing the ``experience`` tool name, the ``experience_review``
  hook name, and the ``experience.injection`` section spec (order=50).
- **Binding-declared hooks (the anchor)** — ``bind`` vouches the review
  hook iff BOTH the tool and the hook survived the merge; the tool's
  death drops the hook (package coherence via the compiler's generic
  post-bind gating), a hook minus-veto drops it via the merge. No state
  raises — the historical shapes were silent degradations.
- **Declaration matrix** — the eight shapes of the retired
  experience-supplement face, migrated to
  ``capabilities: {experience: {}}``: plain, tool-veto, hook-veto,
  whole-package veto, handwritten-plus, nothing, plus-dedup, wholesale
  replace. The bare ``tools: [+experience]`` mode is the documented
  divergence: the tool rides the roster, but nothing else does (the
  capability is the package switch).
- **Golden split-brain** — the shipped bot.yml's post-migration facets
  vs the machine-captured pre-migration goldens
  (``tests/unit/scope/goldens/experience/``, captured on this wave's
  parent commit), with the documented section exemption (the injection
  provider lands with T14).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from modex_agent.plugins.abc import ComponentSlot
from modex_agent.plugins.capability import (
    AgentDeclarationView,
    AgentDeclaredFields,
    CapabilityBinding,
    FinalRosterView,
    PromptSectionSpec,
    TreePositionView,
)
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.defaults.capabilities.experience import (
    EXPERIENCE_TOOL_NAME,
    ExperienceCapability,
)
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.scope.compiler import CompiledAgent, ToolOrigin, compile_scope
from modex_agent.scope.defaults import POSITION_DEFAULT_HOOKS
from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec
from modex_agent.tools.presets import EXPERIENCE_REVIEW_HOOK_NAME
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths
from tests.unit.scope.goldens.assertor import (
    Exemption,
    FacetField,
    Facets,
    GoldenFile,
    assert_facets_equal,
)
from tests.unit.scope.goldens.capture import GoldenPackage, capture_package_facets

_DIR = Path(__file__).resolve().parent
_GOLDEN_DIR = _DIR / "goldens" / "experience"

# The shipped bot.yml agents that declared the experience package
# pre-migration (the golden's sections facet differs for exactly these).
_EXPERIENCE_AGENTS_PATTERN = r"(default|reviewer)"

_EXPERIENCE_EXEMPTIONS = (
    Exemption(
        package="experience",
        facet_field=FacetField.SECTIONS,
        agent_pattern=_EXPERIENCE_AGENTS_PATTERN,
        reason=(
            "experience.injection section spec now declarative (order=50) — the "
            "golden predates declarable sections (empty pre-migration). The "
            "T14 supply wave landed the byte-parity content provider (pinned "
            "in test_experience_supply.py against the pre-migration capture); "
            "the section renders at the capability anchor (SPEC §7.3) instead "
            "of the retired position 8 — the designed position delta, content "
            "byte-equal"
        ),
    ),
)

# The subagents wave (T15) rides this golden's facets on every native
# topology agent (hook roster + sections) and on the external peer pool
# (the retired dead-weight derived entry + the supply-key projection
# switch) — the T10 cross-golden contamination pattern.
_SUBAGENTS_ON_EXPERIENCE_GOLDEN = (
    Exemption(
        package="experience",
        facet_field=FacetField.HOOK_ROSTER,
        agent_pattern=r"(office-expert|explore|general)",
        reason=(
            "subagent_auto_send is now a roster entry the subagents "
            "capability contributes for every non-root agent — the golden "
            "predates the subagents migration (T15)"
        ),
    ),
    Exemption(
        package="experience",
        facet_field=FacetField.SECTIONS,
        agent_pattern=r"(default|office-expert|orchestrator|explore|general|reviewer)",
        reason=(
            "subagents.delegation/consultation/peer section specs now "
            "declarative (orders 40/41/42) — the golden predates the "
            "subagents migration (T15); the content providers land with the "
            "subagents supply wave (two-step)"
        ),
    ),
)

_SUBAGENTS_ON_EXTERNAL_POOL_GOLDEN = (
    Exemption(
        package="experience",
        facet_field=FacetField.TOOL_ROSTER,
        agent_pattern=r"opencode",
        reason=(
            "the retired compiler-side tree derivation produced a dead-weight "
            "send_to_peer entry on the external root; SPEC §3.2 C0 structural "
            "exclusion means subagents predicates never run for external "
            "agents (T15)"
        ),
    ),
    Exemption(
        package="experience",
        facet_field=FacetField.SUPPLY_KEYS,
        agent_pattern=r"opencode",
        reason=(
            "the capture's subagents supply-key projection switched to "
            "compile-product authority with the subagents migration (T15); "
            "the external opencode pool compiles no capabilities"
        ),
    ),
)

_CAPABILITY_ORIGIN_RECLASSIFICATION_REASON = (
    "origin reclassified SUPPLEMENT→CAPABILITY_DERIVED — the channel's true name, SPEC §9"
)


def _capability_origin_exemptions_for(golden: Mapping[str, Facets]) -> tuple[Exemption, ...]:
    affected_agents = sorted(
        agent
        for agent, facets in golden.items()
        if any(tool.origin is ToolOrigin.SUPPLEMENT for tool in facets.tool_roster)
    )
    if not affected_agents:
        return ()
    return (
        Exemption(
            package="experience",
            facet_field=FacetField.TOOL_ROSTER,
            agent_pattern=f"({'|'.join(affected_agents)})",
            reason=_CAPABILITY_ORIGIN_RECLASSIFICATION_REASON,
        ),
    )


def _subagents_exemptions_for(golden: Mapping[str, Facets]) -> tuple[Exemption, ...]:
    """The subagents-wave exemptions riding one pool's comparison — derived
    from the golden's own derived-entry origins: a pool carrying
    task/send_to_agent entries has native topology agents (hook + sections
    deltas); a pool carrying ONLY derived_send_to_peer is the external peer
    pool (dead-weight entry + projection deltas)."""
    origins = {
        tool.origin.value
        for facets in golden.values()
        for tool in facets.tool_roster
        if tool.origin.value.startswith("derived_")
    }
    if "derived_task" in origins or "derived_send_to_agent" in origins:
        return _SUBAGENTS_ON_EXPERIENCE_GOLDEN
    if origins:
        return _SUBAGENTS_ON_EXTERNAL_POOL_GOLDEN
    return ()


_NATIVE_AGENTS_PATTERN = r"(default|office-expert|orchestrator|explore|general|reviewer)"
_POSITION_DEFAULT_HOOKS_REASON = (
    "deliver_retry / length_guard / native_env are compiler position-default "
    "roster rows (SPEC §3.2 hook rows, T23) and model_choice_bind a declared "
    "roster entry on the native mains — the golden predates the W6 glue "
    "eradication (code-wired injections then)"
)


def _position_default_hook_exemption_for(golden: Mapping[str, Facets]) -> tuple[Exemption, ...]:
    """The T23 position-default hook rows ride every NATIVE pool's hook
    roster — external agents are structurally excluded, so the external
    pool's facets carry no drift and the table must not ride its call
    (the assertor's unused-exemption check is per call)."""
    origins = {
        tool.origin.value
        for facets in golden.values()
        for tool in facets.tool_roster
        if tool.origin.value.startswith("derived_")
    }
    if not ({"derived_task", "derived_send_to_agent"} & origins):
        return ()
    return (
        Exemption(
            package="experience",
            facet_field=FacetField.HOOK_ROSTER,
            agent_pattern=_NATIVE_AGENTS_PATTERN,
            reason=_POSITION_DEFAULT_HOOKS_REASON,
        ),
    )


def _registry() -> ComponentRegistry:
    """A registry carrying the FW defaults (the experience capability
    lives in DefaultPlugin — the production registration face)."""
    registry = ComponentRegistry()
    ctx = PluginRegistrationContext(registry)
    DefaultPlugin().register(ctx)
    ctx.flush()
    return registry


def _tree_view() -> TreePositionView:
    return TreePositionView(
        pool_name="p", agent_name="root", is_root=True, parent=None, children=(), peers=()
    )


def _declaration_view() -> AgentDeclarationView:
    return AgentDeclarationView(
        pool_name="p",
        agent_name="root",
        is_root=True,
        parent=None,
        children=(),
        peers=(),
        declared=AgentDeclaredFields(),
    )


def _workspace_ctx() -> WorkspaceContext:
    target = Path("/tmp/test_experience_capability_ws")
    return WorkspaceContext(target=target, paths=WorkspacePaths(root=target), is_home=False)


def _compile_root(agent: AgentSpec) -> CompiledAgent:
    """Compile a single-root pool declaration through the production
    registry → its one compiled agent."""
    compilation = compile_scope(
        ScopeSpec(kind=ScopeKind.POOL, pool=PoolSpec(name="p", agents=[agent])),
        workspace_ctx=_workspace_ctx(),
        registry=_registry(),
    )
    assert len(compilation.agents) == 1
    return compilation.agents[0]


def _experience_binding(compiled: CompiledAgent) -> CapabilityBinding:
    entry = next(c for c in compiled.spec.capabilities if c.name == "experience")
    return entry.binding


# ── Protocol shape ────────────────────────────────────────────────────────────


class TestProtocolShape:
    def test_registered_in_capability_slot(self) -> None:
        registry = _registry()
        assert registry.resolve(ComponentSlot.CAPABILITY, "experience") is not None
        assert isinstance(registry.resolve_capability("experience"), ExperienceCapability)

    def test_applies_default_false(self) -> None:
        assert ExperienceCapability().applies(_declaration_view()) is False

    def test_contribute_shape(self) -> None:
        contribution = ExperienceCapability().contribute(
            _tree_view(), ExperienceCapability().config_model()
        )
        assert contribution.tools == (EXPERIENCE_TOOL_NAME,)
        assert contribution.hooks == (EXPERIENCE_REVIEW_HOOK_NAME,)
        assert contribution.sections == (
            PromptSectionSpec(section_id="experience.injection", order=50),
        )
        assert contribution.tool_replacements == ()

    def test_config_rejects_unknown_keys(self) -> None:
        with pytest.raises(ValidationError):
            ExperienceCapability().config_model.model_validate({"bogus": 1})

    def test_config_carries_the_reviewer_knobs(self) -> None:
        config = ExperienceCapability().config_model.model_validate({"min_messages": 5})
        assert isinstance(config, BaseModel)
        assert config.min_messages == 5

    def test_contributed_tool_name_matches_constructed_tool(self, tmp_path: Path) -> None:
        """Drift guard: the contributed roster name equals a constructed
        ExperienceTool's ``.name`` (the tool is factory-built at assembly
        — the name is the only compile-time contract)."""
        from modex_agent.core.experience import PerFileExperienceMetaStore
        from modex_agent.memory.tools.experience import ExperienceTool

        tool = ExperienceTool(tmp_path, PerFileExperienceMetaStore(tmp_path))
        assert tool.name == EXPERIENCE_TOOL_NAME


# ── Binding anchor states (C2) ────────────────────────────────────────────────


class TestBindingAnchorStates:
    def test_tool_and_hook_alive_vouches_the_hook(self) -> None:
        binding = ExperienceCapability().bind(
            _tree_view(),
            ExperienceCapability().config_model(),
            FinalRosterView(tools=(EXPERIENCE_TOOL_NAME,), hooks=(EXPERIENCE_REVIEW_HOOK_NAME,)),
        )
        assert binding.hooks == (EXPERIENCE_REVIEW_HOOK_NAME,)
        assert [s.section_id for s in binding.active_sections] == ["experience.injection"]

    def test_tool_dead_drops_hook_and_section(self) -> None:
        binding = ExperienceCapability().bind(
            _tree_view(),
            ExperienceCapability().config_model(),
            FinalRosterView(tools=(), hooks=(EXPERIENCE_REVIEW_HOOK_NAME,)),
        )
        assert binding.hooks == ()
        assert binding.active_sections == ()

    def test_hook_vetoed_keeps_tool_drops_hook(self) -> None:
        binding = ExperienceCapability().bind(
            _tree_view(),
            ExperienceCapability().config_model(),
            FinalRosterView(tools=(EXPERIENCE_TOOL_NAME,), hooks=()),
        )
        assert binding.hooks == ()
        assert [s.section_id for s in binding.active_sections] == ["experience.injection"]

    def test_no_anchor_state_raises(self) -> None:
        capability = ExperienceCapability()
        config = capability.config_model()
        for roster in (
            FinalRosterView(tools=(), hooks=()),
            FinalRosterView(tools=(EXPERIENCE_TOOL_NAME,), hooks=()),
            FinalRosterView(tools=(), hooks=(EXPERIENCE_REVIEW_HOOK_NAME,)),
        ):
            capability.bind(_tree_view(), config, roster)  # silent, never CapabilityError


# ── Declaration matrix (the migrated supplement-binding rows) ────────────────


class TestDeclarationMatrix:
    def test_declared_capability_binds_tool_and_review_hook(self) -> None:
        # Row 1: capabilities: {experience: {}} → tool AND hook.
        compiled = _compile_root(AgentSpec(name="root", capabilities={"experience": {}}))
        assert EXPERIENCE_TOOL_NAME in compiled.spec.tools
        assert EXPERIENCE_REVIEW_HOOK_NAME in compiled.spec.hooks

    def test_minus_tool_entry_kills_tool_and_hook(self) -> None:
        # Row 2 (THE whole-package veto): capability + tools:
        # [-experience] → neither tool NOR hook (the binding drops the
        # unvouched contributed hook — package coherence).
        compiled = _compile_root(
            AgentSpec(
                name="root",
                capabilities={"experience": {}},
                tools=[f"-{EXPERIENCE_TOOL_NAME}"],
            )
        )
        assert EXPERIENCE_TOOL_NAME not in compiled.spec.tools
        assert EXPERIENCE_REVIEW_HOOK_NAME not in compiled.spec.hooks
        binding = _experience_binding(compiled)
        assert binding.hooks == ()
        assert binding.active_sections == ()

    def test_bare_plus_tool_entry_binds_tool_only(self) -> None:
        # Row 3 (bare-tool degraded mode — the documented divergence):
        # tools: [+experience] WITHOUT the capability → the tool rides
        # the roster, but no hook/section/manager ride it. The retired
        # supplement face bound the whole package to the tool name; the
        # capability face is the package switch.
        compiled = _compile_root(AgentSpec(name="root", tools=[f"+{EXPERIENCE_TOOL_NAME}"]))
        assert EXPERIENCE_TOOL_NAME in compiled.spec.tools
        assert EXPERIENCE_REVIEW_HOOK_NAME not in compiled.spec.hooks
        assert compiled.spec.capabilities == ()

    def test_minus_hook_entry_wins_keeps_tool(self) -> None:
        # Row 4 (minus-wins): tool present + hooks: [-experience_review]
        # → tool yes, hook NO.
        compiled = _compile_root(
            AgentSpec(
                name="root",
                capabilities={"experience": {}},
                hooks=[f"-{EXPERIENCE_REVIEW_HOOK_NAME}"],
            )
        )
        assert EXPERIENCE_TOOL_NAME in compiled.spec.tools
        assert EXPERIENCE_REVIEW_HOOK_NAME not in compiled.spec.hooks

    def test_both_vetoes_kill_tool_and_hook(self) -> None:
        # Row 4b (both-veto): tools: [-experience] AND hooks:
        # [-experience_review] together → neither; identical to the
        # tool-veto-only roster (the hook was already gone either way —
        # the old injection and the new binding gating agree).
        compiled = _compile_root(
            AgentSpec(
                name="root",
                capabilities={"experience": {}},
                tools=[f"-{EXPERIENCE_TOOL_NAME}"],
                hooks=[f"-{EXPERIENCE_REVIEW_HOOK_NAME}"],
            )
        )
        assert EXPERIENCE_TOOL_NAME not in compiled.spec.tools
        assert EXPERIENCE_REVIEW_HOOK_NAME not in compiled.spec.hooks

    def test_handwritten_plus_hook_dedups_to_one_entry(self) -> None:
        # Row 5 (dedup): capability + handwritten hooks:
        # [+experience_review] → exactly ONE hook entry.
        compiled = _compile_root(
            AgentSpec(
                name="root",
                capabilities={"experience": {}},
                hooks=[f"+{EXPERIENCE_REVIEW_HOOK_NAME}"],
            )
        )
        assert compiled.spec.hooks.count(EXPERIENCE_REVIEW_HOOK_NAME) == 1
        assert compiled.spec.hooks == [*POSITION_DEFAULT_HOOKS, EXPERIENCE_REVIEW_HOOK_NAME]

    def test_nothing_declared_has_neither(self) -> None:
        # Row 6 (new default): nothing declared → neither.
        compiled = _compile_root(AgentSpec(name="root"))
        assert EXPERIENCE_TOOL_NAME not in compiled.spec.tools
        assert compiled.spec.hooks == list(POSITION_DEFAULT_HOOKS)

    def test_capability_and_plus_entry_dedup_to_one_tool(self) -> None:
        # Row 7 (merge dedup): capability + tools: [+experience] →
        # exactly ONE tool entry.
        compiled = _compile_root(
            AgentSpec(
                name="root",
                capabilities={"experience": {}},
                tools=[f"+{EXPERIENCE_TOOL_NAME}"],
            )
        )
        assert compiled.spec.tools.count(EXPERIENCE_TOOL_NAME) == 1

    def test_wholesale_tools_replace_kills_tool_and_hook(self) -> None:
        # Row 8 (O4/V8 wholesale-replace interaction): capability +
        # unprefixed tools: [read, write] → the wholesale list REPLACES
        # the merge base including the contributed name, and the hook
        # goes with it.
        compiled = _compile_root(
            AgentSpec(
                name="root",
                capabilities={"experience": {}},
                tools=["read", "write"],
            )
        )
        assert compiled.spec.tools == ["read", "write"]
        assert EXPERIENCE_REVIEW_HOOK_NAME not in compiled.spec.hooks

    def test_handwritten_hook_survives_when_capability_drops_it(self) -> None:
        # The gating boundary: the tool died (binding vouches nothing)
        # but the agent's own ``+experience_review`` declaration keeps
        # the hook alive — a handwritten entry belongs to the
        # declaration, never the binding.
        compiled = _compile_root(
            AgentSpec(
                name="root",
                capabilities={"experience": {}},
                tools=[f"-{EXPERIENCE_TOOL_NAME}"],
                hooks=[f"+{EXPERIENCE_REVIEW_HOOK_NAME}"],
            )
        )
        assert EXPERIENCE_TOOL_NAME not in compiled.spec.tools
        assert compiled.spec.hooks == [*POSITION_DEFAULT_HOOKS, EXPERIENCE_REVIEW_HOOK_NAME]

    def test_handwritten_hook_without_capability_untouched(self) -> None:
        # Purely handwritten (no capability): the name is not
        # contributed → not gateable.
        compiled = _compile_root(AgentSpec(name="root", hooks=[f"+{EXPERIENCE_REVIEW_HOOK_NAME}"]))
        assert compiled.spec.hooks == [*POSITION_DEFAULT_HOOKS, EXPERIENCE_REVIEW_HOOK_NAME]

    def test_capability_false_disables_whole_bundle(self) -> None:
        compiled = _compile_root(
            AgentSpec(
                name="root",
                capabilities={"experience": False},
                tools=[f"+{EXPERIENCE_TOOL_NAME}"],
            )
        )
        assert EXPERIENCE_TOOL_NAME in compiled.spec.tools  # bare tool entry
        assert EXPERIENCE_REVIEW_HOOK_NAME not in compiled.spec.hooks
        assert compiled.spec.capabilities == ()

    def test_contributed_entry_classifies_capability_origin(self) -> None:
        compiled = _compile_root(AgentSpec(name="root", capabilities={"experience": {}}))
        entry = next(
            (e for e in compiled.provenance.tools if e.tool == EXPERIENCE_TOOL_NAME),
            None,
        )
        assert entry is not None
        assert entry.origin is ToolOrigin.CAPABILITY_DERIVED
        assert entry.capability == "experience"

    def test_config_knob_lands_in_compile_product(self) -> None:
        compiled = _compile_root(
            AgentSpec(name="root", capabilities={"experience": {"min_messages": 5}})
        )
        binding = _experience_binding(compiled)
        entry = compiled.spec.capabilities[0]
        assert entry.config["min_messages"] == 5
        assert binding.hooks == (EXPERIENCE_REVIEW_HOOK_NAME,)


# ── Golden split-brain ────────────────────────────────────────────────────────


class TestGoldenSplitBrain:
    async def test_shipped_bot_facets_match_pre_migration_goldens(self) -> None:
        actual = await capture_package_facets(GoldenPackage.EXPERIENCE)

        assert sorted(actual) == ["coder", "default", "opencode", "review"]
        for pool, document in actual.items():
            golden = GoldenFile.model_validate_json(
                (_GOLDEN_DIR / f"{pool}.json").read_text(encoding="utf-8")
            ).root
            # The assertor's unused-exemption check is per call: pools with
            # no experience-effective agent (coder, opencode) have no
            # experience facet deltas, so the experience exemption table
            # must not ride their comparison. The subagents wave's deltas
            # ride every pool with topology-participating agents.
            pool_has_experience = any(
                "experience" in facets.effective_set for facets in golden.values()
            )
            exemptions = _EXPERIENCE_EXEMPTIONS if pool_has_experience else ()
            exemptions += _subagents_exemptions_for(golden)
            exemptions += _capability_origin_exemptions_for(golden)
            exemptions += _position_default_hook_exemption_for(golden)
            assert_facets_equal(
                document.root,
                golden,
                "experience",
                exemptions,
            )
