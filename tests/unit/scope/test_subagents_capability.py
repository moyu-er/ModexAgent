"""The FW-bundled ``subagents`` capability — the T15 migration faces.

Covers:

- **Predicate matrix** — the SPEC §3.2 tree predicate verbatim: children,
  non-root position, and peer links each enable; a no-children no-peer
  root does not (the zero-config equivalence anchor).
- **Contribute shapes per tree position** — the derived entries
  (``task`` / ``send_to_agent`` / ``send_to_peer`` with origins +
  targets), the ``subagent_auto_send`` hook (non-root only), and the
  three section specs (orders 40/41/42).
- **A3 derived-entries equivalence** — table-driven: the OLD
  ``_derived_entries`` output (machine-captured on the pre-migration
  HEAD by ``goldens/subagents/capture_derived_entries.py`` — the
  function is deleted) vs the NEW
  ``SubagentsCapability.contribute`` output on the 3-level fixture tree
  AND the shipped tree: identical tool names, origins, targets.
- **V6 dual check** — the bind anchor (children ⇒ ``task`` in the final
  roster, ``CapabilityError`` with pool/agent/capability + repair path)
  beside the untouched phase-2 validator V6 (the gap case: the
  capability disabled while children remain).
- **Zero-config auto-apply** — the shipped bot.yml declares no
  ``subagents`` blocks; every topology-participating native agent
  compiles the capability anyway (the derivation source, not a behavior
  change).
- **Capability-level veto** — ``capabilities: {subagents: false}`` kills
  the whole family (tools + hook + sections).
- **Golden split-brain** — the shipped tree's post-migration facets vs
  the machine-captured pre-migration goldens, with the documented
  exemption table (the external-root C0 exclusion, the now-declarative
  hook roster and sections).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from modex_agent.plugins.abc import ComponentSlot
from modex_agent.plugins.capability import (
    AgentDeclarationView,
    AgentDeclaredFields,
    CapabilityError,
    ChildSummary,
    PromptSectionSpec,
    TreePositionView,
)
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.defaults.capabilities.subagents import (
    SUBAGENTS_AUTO_SEND_HOOK_NAME,
    SubagentsCapability,
    SubagentsCapabilityConfig,
)
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.scope.compiler import ToolOrigin, compile_scope
from modex_agent.scope.loader import load_scope_declaration
from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec
from modex_agent.scope.validator import RuleId, validate_effective_configs
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
_GOLDEN_DIR = _DIR / "goldens" / "subagents"
_DECLARATION_PATH = _DIR.parents[2] / "examples" / "bot_project" / "config" / "scopes" / "bot.yml"
_EQUIVALENCE_FIXTURE: dict[str, Any] = json.loads(
    (_GOLDEN_DIR / "derived_entries.json").read_text(encoding="utf-8")
)


def _registry() -> ComponentRegistry:
    registry = ComponentRegistry()
    ctx = PluginRegistrationContext(registry)
    DefaultPlugin().register(ctx)
    ctx.flush()
    return registry


def _workspace_ctx() -> WorkspaceContext:
    target = Path("/tmp/test_subagents_capability_ws")
    return WorkspaceContext(target=target, paths=WorkspacePaths(root=target), is_home=False)


def _compile(spec: ScopeSpec):
    return compile_scope(spec, workspace_ctx=_workspace_ctx(), registry=_registry())


def _declaration_view(
    *,
    is_root: bool = True,
    parent: str | None = None,
    children: tuple[ChildSummary, ...] = (),
    peers: tuple[str, ...] = (),
) -> AgentDeclarationView:
    return AgentDeclarationView(
        pool_name="p",
        agent_name="probe",
        is_root=is_root,
        parent=parent,
        children=children,
        peers=peers,
        declared=AgentDeclaredFields(),
    )


def _tree_view(
    *,
    is_root: bool = True,
    parent: str | None = None,
    children: tuple[ChildSummary, ...] = (),
    peers: tuple[str, ...] = (),
) -> TreePositionView:
    return TreePositionView(
        pool_name="p",
        agent_name="probe",
        is_root=is_root,
        parent=parent,
        children=children,
        peers=peers,
    )


def _tree_views(spec: ScopeSpec) -> dict[tuple[str, str], TreePositionView]:
    """Per-agent tree views — the same facts the compiler feeds C1."""
    from modex_agent.scope.validator import _pools_of

    views: dict[tuple[str, str], TreePositionView] = {}
    for pool in _pools_of(spec):
        children: dict[str, list[str]] = {}
        for agent in pool.agents:
            if agent.parent is not None:
                children.setdefault(agent.parent, []).append(agent.name)
        descriptions = {agent.name: agent.description for agent in pool.agents}
        for agent in pool.agents:
            views[(pool.name, agent.name)] = TreePositionView(
                pool_name=pool.name,
                agent_name=agent.name,
                is_root=agent.parent is None,
                parent=agent.parent,
                children=tuple(
                    ChildSummary(name=name, description=descriptions[name])
                    for name in children.get(agent.name, [])
                ),
                peers=tuple(pool.peers),
            )
    return views


def _contribute_entries(tree: TreePositionView) -> list[dict[str, Any]]:
    """The capability's derived entries in the old capture's JSON shape."""
    contribution = SubagentsCapability().contribute(tree, SubagentsCapabilityConfig())
    return [
        {"tool": spec.tool, "origin": spec.origin.value, "targets": list(spec.targets)}
        for spec in contribution.derived_tools
    ]


# ─── Predicate matrix (SPEC §3.2 verbatim) ───────────────────────────────────


class TestPredicateMatrix:
    def test_lone_root_does_not_apply(self) -> None:
        view = _declaration_view(is_root=True, children=(), peers=())
        assert SubagentsCapability().applies(view) is False

    def test_children_apply(self) -> None:
        view = _declaration_view(children=(ChildSummary(name="sub"),))
        assert SubagentsCapability().applies(view) is True

    def test_non_root_applies(self) -> None:
        view = _declaration_view(is_root=False, parent="root")
        assert SubagentsCapability().applies(view) is True

    def test_root_with_peers_applies(self) -> None:
        view = _declaration_view(is_root=True, peers=("other",))
        assert SubagentsCapability().applies(view) is True

    def test_registered_in_capability_slot(self) -> None:
        registry = _registry()
        assert "subagents" in registry.names(ComponentSlot.CAPABILITY)
        assert registry.resolve_capability("subagents").name == "subagents"

    def test_config_rejects_unknown_keys(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SubagentsCapabilityConfig.model_validate({"bogus": 1})


# ─── Contribute shapes per tree position ─────────────────────────────────────


class TestContributeShapes:
    def test_lone_root_contributes_nothing(self) -> None:
        contribution = SubagentsCapability().contribute(_tree_view(), SubagentsCapabilityConfig())
        assert contribution.derived_tools == ()
        assert contribution.tools == ()
        assert contribution.hooks == ()
        assert contribution.sections == ()

    def test_root_with_children(self) -> None:
        contribution = SubagentsCapability().contribute(
            _tree_view(children=(ChildSummary(name="a"), ChildSummary(name="b"))),
            SubagentsCapabilityConfig(),
        )
        assert [(s.tool, s.origin.value, s.targets) for s in contribution.derived_tools] == [
            ("task", "derived_task", ("a", "b")),
        ]
        assert contribution.hooks == ()
        assert contribution.sections == (
            PromptSectionSpec(section_id="subagents.delegation", order=40),
        )

    def test_mid_level_agent_gets_task_and_send_to_agent(self) -> None:
        contribution = SubagentsCapability().contribute(
            _tree_view(
                is_root=False,
                parent="root",
                children=(ChildSummary(name="leaf"),),
            ),
            SubagentsCapabilityConfig(),
        )
        assert [(s.tool, s.origin.value, s.targets) for s in contribution.derived_tools] == [
            ("task", "derived_task", ("leaf",)),
            ("send_to_agent", "derived_send_to_agent", ("root",)),
        ]
        assert contribution.hooks == (SUBAGENTS_AUTO_SEND_HOOK_NAME,)
        assert contribution.sections == (
            PromptSectionSpec(section_id="subagents.delegation", order=40),
            PromptSectionSpec(section_id="subagents.consultation", order=41),
        )

    def test_leaf_gets_send_to_agent_only(self) -> None:
        contribution = SubagentsCapability().contribute(
            _tree_view(is_root=False, parent="mid"),
            SubagentsCapabilityConfig(),
        )
        assert [(s.tool, s.origin.value, s.targets) for s in contribution.derived_tools] == [
            ("send_to_agent", "derived_send_to_agent", ("mid",)),
        ]
        assert contribution.hooks == (SUBAGENTS_AUTO_SEND_HOOK_NAME,)
        assert contribution.sections == (
            PromptSectionSpec(section_id="subagents.consultation", order=41),
        )

    def test_root_with_peers(self) -> None:
        contribution = SubagentsCapability().contribute(
            _tree_view(peers=("other", "third")),
            SubagentsCapabilityConfig(),
        )
        assert [(s.tool, s.origin.value, s.targets) for s in contribution.derived_tools] == [
            ("send_to_peer", "derived_send_to_peer", ("other", "third")),
        ]
        assert contribution.hooks == ()
        assert contribution.sections == (PromptSectionSpec(section_id="subagents.peer", order=42),)

    def test_non_root_with_peers_gets_no_peer_tool(self) -> None:
        # The peer tool is root-only (SPEC §3.2) — a peer-linked pool's
        # subagent still derives only send_to_agent.
        contribution = SubagentsCapability().contribute(
            _tree_view(is_root=False, parent="root", peers=("other",)),
            SubagentsCapabilityConfig(),
        )
        assert [s.tool for s in contribution.derived_tools] == ["send_to_agent"]


# ─── A3 derived-entries equivalence (the old function vs the capability) ─────


class TestDerivedEntriesEquivalence:
    """The OLD ``_derived_entries`` output (captured on the pre-migration
    HEAD — the function is deleted) vs the NEW capability contribute:
    identical entries, identical origins, identical targets."""

    @pytest.mark.parametrize(
        ("pool_name", "agent_name"),
        [("nested", "root"), ("nested", "mid"), ("nested", "leaf"), ("peered", "root2")],
    )
    def test_fixture_tree(self, pool_name: str, agent_name: str) -> None:
        views = _fixture_tree_views()
        expected = _EQUIVALENCE_FIXTURE["fixture"][pool_name][agent_name]
        assert _contribute_entries(views[(pool_name, agent_name)]) == expected

    def test_fixture_tree_covered_every_position(self) -> None:
        views = _fixture_tree_views()
        assert sorted(views) == [
            ("nested", "leaf"),
            ("nested", "mid"),
            ("nested", "root"),
            ("peered", "root2"),
        ]
        assert _EQUIVALENCE_FIXTURE["fixture"]["nested"]["mid"], "mid is the 3-level pivot"

    @pytest.mark.parametrize(
        ("pool_name", "agent_name"),
        sorted(
            (pool, agent)
            for pool, agents in _EQUIVALENCE_FIXTURE["shipped"].items()
            for agent in agents
        ),
    )
    def test_shipped_tree(self, pool_name: str, agent_name: str) -> None:
        # Equivalence is at the CONTRIBUTE level (same tree view in, same
        # entries out — including the external opencode root's view: the
        # derivation logic is tree-position-only and provider-agnostic).
        # Whether a contribute ever RUNS is the enablement layer's call:
        # C0 predicates never run for external agents, so opencode's
        # entries never reach a compile (pinned by
        # TestAutoApplyZeroConfig and the golden exemption table).
        declaration = load_scope_declaration(_DECLARATION_PATH)
        views = _tree_views(declaration)
        expected = _EQUIVALENCE_FIXTURE["shipped"][pool_name][agent_name]
        assert _contribute_entries(views[(pool_name, agent_name)]) == expected


class TestShippedCompileDerivedEntries:
    """Wave-3 closure spot-check (T17): the shipped bot.yml compiled with
    the real production registry (DefaultPlugin + the bot project's
    plugins — the capture recipe) reproduces the T15-captured
    ``_derived_entries`` output on every NATIVE agent end-to-end — C0
    enablement, the C1 derived channel, and provenance classification
    together, not just the contribute level. The external opencode root
    compiles NO derived entries; its captured dead-weight ``send_to_peer``
    entry is the documented C0 structural exclusion (the golden exemption
    table).
    """

    async def test_compile_provenance_matches_captured_derived_entries(self) -> None:
        from modex_agent.plugins.loader import (
            ComponentRegistryLoader,
            PluginDiscoveryConfig,
        )
        from modex_agent.scope.validator import _pools_of

        registry = ComponentRegistry()
        await ComponentRegistryLoader.load(
            registry,
            PluginDiscoveryConfig(
                bundled_factories=(DefaultPlugin(),),
                project_plugin_paths=(_DECLARATION_PATH.parents[2] / "plugins",),
            ),
        )
        declaration = load_scope_declaration(_DECLARATION_PATH)
        compilation = compile_scope(
            declaration,
            workspace_ctx=_workspace_ctx(),
            registry=registry,
        )
        external_pools = {
            pool.name
            for pool in _pools_of(declaration)
            if pool.root_agent.provider_kind is not None
        }

        def _compiled_entries(agent: Any) -> list[dict[str, Any]]:
            return [
                {
                    "tool": entry.tool,
                    "origin": entry.origin.value,
                    "targets": list(entry.targets),
                }
                for entry in agent.provenance.tools
                if entry.origin.value.startswith("derived_")
            ]

        def _entry_key(entry: dict[str, Any]) -> tuple[str, str, tuple[str, ...]]:
            return (entry["tool"], entry["origin"], tuple(entry["targets"]))

        covered: set[tuple[str, str]] = set()
        for agent in compilation.agents:
            identity = (agent.provenance.pool, agent.provenance.agent)
            covered.add(identity)
            expected = _EQUIVALENCE_FIXTURE["shipped"][identity[0]][identity[1]]
            actual = _compiled_entries(agent)
            if identity[0] in external_pools:
                # C0 structural exclusion: predicates never run for
                # external agents, so the captured dead-weight entry must
                # NOT reach the compile product.
                assert actual == []
                assert expected, "the captured external entry is the dead-weight one"
                continue
            assert sorted(actual, key=_entry_key) == sorted(expected, key=_entry_key)
        # Every captured agent was adjudicated — no silent coverage gap.
        assert covered == {
            (pool, agent)
            for pool, agents in _EQUIVALENCE_FIXTURE["shipped"].items()
            for agent in agents
        }


def _fixture_tree_views() -> dict[tuple[str, str], TreePositionView]:
    spec = ScopeSpec(
        kind=ScopeKind.POOL,
        pool=PoolSpec(
            name="nested",
            agents=[
                AgentSpec(name="root", description="top"),
                AgentSpec(name="mid", parent="root", description="middle"),
                AgentSpec(name="leaf", parent="mid", description="bottom"),
            ],
        ),
    )
    peered = ScopeSpec(
        kind=ScopeKind.POOL,
        pool=PoolSpec(name="peered", peers=["nested"], agents=[AgentSpec(name="root2")]),
    )
    from modex_agent.scope.spec import WorkspaceSpec

    combined = ScopeSpec(
        kind=ScopeKind.WORKSPACE,
        workspace=WorkspaceSpec(
            name="w",
            pools=(spec.pool, peered.pool),  # type: ignore[arg-type]
        ),
    )
    return _tree_views(combined)


# ─── V6 dual check (bind anchor + phase-2 validator) ─────────────────────────


class TestV6DualCheck:
    def test_children_plus_task_veto_fails_bind(self) -> None:
        spec = ScopeSpec(
            kind=ScopeKind.POOL,
            pool=PoolSpec(
                name="p",
                agents=[
                    AgentSpec(name="root", tools=["-task"]),
                    AgentSpec(name="sub", parent="root"),
                ],
            ),
        )
        with pytest.raises(CapabilityError) as excinfo:
            _compile(spec)
        message = str(excinfo.value)
        assert "'subagents'" in message  # capability
        assert "'p'" in message  # pool
        assert "'root'" in message  # agent
        assert "'task'" in message  # the anchor
        assert "sub" in message  # the orphaned child
        assert "tools: [-task]" in message  # the repair path

    def test_children_plus_wholesale_tools_without_task_fails_bind(self) -> None:
        spec = ScopeSpec(
            kind=ScopeKind.POOL,
            pool=PoolSpec(
                name="p",
                agents=[
                    AgentSpec(name="root", tools=["read", "write"]),
                    AgentSpec(name="sub", parent="root"),
                ],
            ),
        )
        with pytest.raises(CapabilityError):
            _compile(spec)

    def test_capability_off_plus_children_hits_validator_v6(self) -> None:
        """The bind's gap case: the capability is disabled, so no anchor
        runs — the untouched phase-2 V6 catches the orphaned subtree."""
        spec = ScopeSpec(
            kind=ScopeKind.POOL,
            pool=PoolSpec(
                name="p",
                agents=[
                    AgentSpec(name="root", capabilities={"subagents": False}),
                    AgentSpec(name="sub", parent="root"),
                ],
            ),
        )
        compilation = _compile(spec)
        root = compilation.agents[0]
        # Only the ROOT vetoed the capability — the non-root sub still
        # auto-applies (the override map is per-agent).
        assert all(cap.name != "subagents" for cap in root.spec.capabilities)
        assert "task" not in root.spec.tools
        issues = validate_effective_configs(spec, [agent.effective for agent in compilation.agents])
        assert [issue.rule for issue in issues] == [RuleId.TASK_TOOL_PRESENT]
        assert "V6" in issues[0].message

    def test_send_to_agent_veto_is_not_anchored(self) -> None:
        # The old world's silent component surgery is preserved: only
        # task is anchored (SPEC §8.4 V6 row).
        spec = ScopeSpec(
            kind=ScopeKind.POOL,
            pool=PoolSpec(
                name="p",
                agents=[
                    AgentSpec(name="root"),
                    AgentSpec(name="sub", parent="root", tools=["-send_to_agent"]),
                ],
            ),
        )
        compilation = _compile(spec)
        sub = compilation.agents[1]
        assert "send_to_agent" not in sub.spec.tools
        assert SUBAGENTS_AUTO_SEND_HOOK_NAME in sub.spec.hooks  # hook unanchored
        assert "task" in compilation.agents[0].spec.tools


# ─── Zero-config auto-apply on the shipped tree ──────────────────────────────


class TestAutoApplyZeroConfig:
    def test_shipped_native_agents_gain_subagents_without_declaration(self) -> None:
        declaration = load_scope_declaration(_DECLARATION_PATH)
        compilation = _compile(declaration)
        expected: dict[tuple[str, str], bool] = {
            ("default", "default"): True,
            ("default", "office-expert"): True,
            ("coder", "orchestrator"): True,
            ("coder", "explore"): True,
            ("coder", "general"): True,
            ("review", "reviewer"): True,
            ("review", "explore"): True,
            ("review", "general"): True,
            ("opencode", "opencode"): False,  # external: predicates never run
        }
        actual = {
            (agent.provenance.pool, agent.provenance.agent): any(
                cap.name == "subagents" for cap in agent.spec.capabilities
            )
            for agent in compilation.agents
        }
        assert actual == expected
        # The C0 exclusion is compile-visible: the external root's retired
        # dead-weight send_to_peer entry does not reach the roster.
        opencode = next(
            agent
            for agent in compilation.agents
            if (agent.provenance.pool, agent.provenance.agent) == ("opencode", "opencode")
        )
        assert "send_to_peer" not in opencode.spec.tools

    def test_lone_root_pool_stays_capability_free(self) -> None:
        spec = ScopeSpec(
            kind=ScopeKind.POOL,
            pool=PoolSpec(
                name="p",
                agents=[AgentSpec(name="solo", capabilities={"skills": False})],
            ),
        )
        compiled = _compile(spec).agents[0]
        assert compiled.spec.capabilities == ()
        assert "task" not in compiled.spec.tools
        assert "send_to_agent" not in compiled.spec.tools
        assert "send_to_peer" not in compiled.spec.tools

    def test_auto_applied_capability_is_vetoable_by_declaration(self) -> None:
        spec = ScopeSpec(
            kind=ScopeKind.POOL,
            pool=PoolSpec(
                name="p",
                agents=[
                    AgentSpec(name="root"),
                    AgentSpec(name="sub", parent="root", capabilities={"subagents": False}),
                ],
            ),
        )
        compilation = _compile(spec)
        sub = compilation.agents[1]
        assert "send_to_agent" not in sub.spec.tools
        assert SUBAGENTS_AUTO_SEND_HOOK_NAME not in sub.spec.hooks
        assert all(
            section.section_id.startswith("subagents.") is False
            for cap in sub.spec.capabilities
            for section in cap.binding.active_sections
        )
        # The root keeps its own derivation (children still task-anchored).
        assert "task" in compilation.agents[0].spec.tools


# ─── Golden split-brain (machine-captured pre-migration facets) ──────────────

# The native-pool facet deltas of this migration. The tool_roster facet —
# names + origins + targets, the A3 core — is EQUAL on every native agent:
# the capability channel reproduces the retired compiler-side derivation
# byte-for-byte. The exemptions are the declarative-channel deltas.
_NATIVE_POOL_EXEMPTIONS = (
    Exemption(
        package="subagents",
        facet_field=FacetField.HOOK_ROSTER,
        agent_pattern=r"(office-expert|explore|general)",
        reason=(
            "subagent_auto_send is now a roster entry the subagents "
            "capability contributes for every non-root agent — the golden "
            "predates declarable hook rosters (the retired "
            "AgentTemplate.materialize injected the hook directly)"
        ),
    ),
    Exemption(
        package="subagents",
        facet_field=FacetField.SECTIONS,
        agent_pattern=r"(default|office-expert|orchestrator|explore|general|reviewer)",
        reason=(
            "subagents.delegation/consultation/peer section specs now "
            "declarative (orders 40/41/42) — the golden predates declarable "
            "sections (empty pre-migration); the byte-parity content "
            "providers land with the subagents supply wave (two-step)"
        ),
    ),
)

# The external opencode pool: the C0 structural exclusion (predicates never
# run for external agents) retires the dead-weight derived entries, and the
# capture's supply-key projection switches to compile-product authority.
_EXTERNAL_POOL_EXEMPTIONS = (
    Exemption(
        package="subagents",
        facet_field=FacetField.TOOL_ROSTER,
        agent_pattern=r"opencode",
        reason=(
            "the retired compiler-side derivation produced a dead-weight "
            "send_to_peer entry on the external root — external agents take "
            "no native tool surface (peer replies route via modexctl), and "
            "SPEC §3.2 C0 structural exclusion means predicates never run "
            "for external agents, so the capability contributes nothing"
        ),
    ),
    Exemption(
        package="subagents",
        facet_field=FacetField.SUPPLY_KEYS,
        agent_pattern=r"opencode",
        reason=(
            "the capture's supply-key projection switches to compile-product "
            "authority the moment any pool compiles subagents; the external "
            "opencode pool compiles no capabilities, while its runtime "
            "communication construction (still unconditional in this wave) "
            "dies with the subagents supply wave"
        ),
    ),
)

_CAPABILITY_ORIGIN_RECLASSIFICATION_REASON = (
    "origin reclassified SUPPLEMENT→CAPABILITY_DERIVED — the channel's true name, SPEC §9"
)

_NUDGE_HOOK_ON_SUBAGENTS_GOLDEN = (
    Exemption(
        package="subagents",
        facet_field=FacetField.HOOK_ROSTER,
        agent_pattern=r"(office-expert|orchestrator|explore|general|reviewer)",
        reason=(
            "todo_planning_nudge is now a roster entry the todo capability "
            "contributes alongside its tools — the golden predates the "
            "todo nudge revival wave"
        ),
    ),
)

_NATIVE_AGENTS_PATTERN = r"(default|office-expert|orchestrator|explore|general|reviewer)"
_POSITION_DEFAULT_HOOKS_REASON = (
    "deliver_retry / length_guard / native_env are compiler position-default "
    "roster rows (SPEC §3.2 hook rows, T23) and model_choice_bind a declared "
    "roster entry on the native mains — the golden predates the W6 glue "
    "eradication (code-wired injections then)"
)


def _capability_origin_exemptions_for(golden: dict[str, Facets]) -> tuple[Exemption, ...]:
    affected_agents = sorted(
        agent
        for agent, facets in golden.items()
        if any(tool.origin is ToolOrigin.SUPPLEMENT for tool in facets.tool_roster)
    )
    if not affected_agents:
        return ()
    return (
        Exemption(
            package="subagents",
            facet_field=FacetField.TOOL_ROSTER,
            agent_pattern=f"({'|'.join(affected_agents)})",
            reason=_CAPABILITY_ORIGIN_RECLASSIFICATION_REASON,
        ),
    )


class TestGoldenSplitBrain:
    async def test_shipped_bot_facets_match_pre_migration_goldens(self) -> None:
        actual = await capture_package_facets(GoldenPackage.SUBAGENTS)

        assert sorted(actual) == ["coder", "default", "opencode", "review"]
        declaration = load_scope_declaration(_DECLARATION_PATH)
        from modex_agent.scope.validator import _pools_of

        external_pools = {
            pool.name
            for pool in _pools_of(declaration)
            if pool.root_agent.provider_kind is not None
        }
        for pool, document in actual.items():
            golden = GoldenFile.model_validate_json(
                (_GOLDEN_DIR / f"{pool}.json").read_text(encoding="utf-8")
            ).root
            # The assertor's unused-exemption check is per call: the
            # external pool needs the C0-exclusion table; the native
            # pools need the declarative-channel table.
            exemptions = (
                _EXTERNAL_POOL_EXEMPTIONS if pool in external_pools else _NATIVE_POOL_EXEMPTIONS
            )
            if pool not in external_pools:
                # The T23 position-default hook rows ride every native
                # pool's hook roster (external agents structurally excluded).
                exemptions += (
                    Exemption(
                        package="subagents",
                        facet_field=FacetField.HOOK_ROSTER,
                        agent_pattern=_NATIVE_AGENTS_PATTERN,
                        reason=_POSITION_DEFAULT_HOOKS_REASON,
                    ),
                )
                # The todo nudge revival wave rides every todo-effective
                # agent's hook roster (the `default` agent declares no
                # todo capability — no drift, no exemption for it).
                exemptions += _NUDGE_HOOK_ON_SUBAGENTS_GOLDEN
            exemptions += _capability_origin_exemptions_for(golden)
            assert_facets_equal(document.root, golden, "subagents", exemptions)
