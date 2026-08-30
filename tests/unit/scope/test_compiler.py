"""Ticket 06 — ScopeCompiler: tree → per-agent AssemblySpecs + effective
toolsets + O3 accounting (pure functions, SPEC §3.2/§3.4/§5.2).

Covers the ticket checkboxes:

- (a) the shipped declaration compiles with the legacy
  pool_name=root-agent-name convention, and output covers every declared
  agent (the old-road ``SpecBuilder.from_roster`` equivalence served
  tickets 02-10 and died with the legacy road in ticket 11).
- (b) the §5.2 derivation table end to end on a three-level tree
  (task lists DIRECT children only; send_to_agent per non-root; leaf has
  no task entry at all; send_to_peer for roots with links).
- (c)/(g) capability same-name replacement accounting (``edit ← aci_edit``,
  declared via ``capabilities: {aci: {}}`` in the shipped declaration) in
  the provenance data, queryable in the pure-function boundary.
- per-field provenance layers (framework default ← profile ← local).
- (d) byte stability: same input tree → byte-identical output.
- (e) phase-2 validation (V6/V9) driven by real compiler output.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.scope import load_scope_declaration
from modex_agent.scope.compiler import (
    AgentProvenance,
    CompiledAgent,
    ProvenanceLayer,
    ScopeCompilation,
    ToolEntryProvenance,
    ToolOrigin,
    ToolReplacement,
    compile_scope,
)
from modex_agent.scope.defaults import RegistrationTiming
from modex_agent.scope.profile import STANDARD_PROFILES, Profile, ProfileStore
from modex_agent.scope.spec import (
    AgentSpec,
    MemoryDeclaration,
    PoolSpec,
    ScopeKind,
    ScopeSpec,
    SessionMemoryOverride,
    WorkspaceSpec,
)
from modex_agent.scope.validator import RuleId, validate_effective_configs
from modex_agent.tools.presets import ToolPreset, get_preset_tools
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

REPO_ROOT = Path(__file__).resolve().parents[3]
BOT_BASE = REPO_ROOT / "examples" / "bot_project"
BOT_YML = BOT_BASE / "config" / "scopes" / "bot.yml"

TASK = "task"
SEND_TO_AGENT = "send_to_agent"
SEND_TO_PEER = "send_to_peer"


# ─── Helpers ────────────────────────────────────────────────────────────────


def _workspace_ctx() -> WorkspaceContext:
    target = Path("/tmp/test_scope_compiler_ws")
    return WorkspaceContext(target=target, paths=WorkspacePaths(root=target), is_home=False)


@lru_cache(maxsize=1)
def _shipped_registry() -> ComponentRegistry:
    """DefaultPlugin registry — the CAPABILITY-slot source the shipped
    declaration's ``capabilities: {aci: {}}`` blocks resolve against."""
    registry = ComponentRegistry()
    ctx = PluginRegistrationContext(registry)
    DefaultPlugin().register(ctx)
    ctx.flush()
    return registry


def _preset_names(preset: ToolPreset) -> list[str]:
    """Dynamically compute preset tool names (test_spec_builder pattern)."""
    names = [t.name for t in get_preset_tools(preset)]
    if preset in (ToolPreset.FULL, ToolPreset.READ_ONLY, ToolPreset.READ_WRITE):
        names.append("bash")
    return names


def _pools(spec: ScopeSpec) -> list[PoolSpec]:
    if spec.kind is ScopeKind.WORKSPACE:
        assert spec.workspace is not None
        return list(spec.workspace.pools)
    assert spec.pool is not None
    return [spec.pool]


def _by_key(compilation: ScopeCompilation) -> dict[tuple[str, str], CompiledAgent]:
    return {(a.provenance.pool, a.provenance.agent): a for a in compilation.agents}


def _entry(provenance: AgentProvenance, tool: str) -> ToolEntryProvenance | None:
    return next((e for e in provenance.tools if e.tool == tool), None)


# ─── (a) Shipped-declaration compile conventions ────────────────────────────


class TestShippedSpecEquivalence:
    def test_pool_name_keeps_legacy_root_agent_name_convention(self) -> None:
        # The compiler sets pool_name to the root agent's name: coder's
        # agents carry "orchestrator", not "coder" (legacy convention).
        compiled = _by_key(
            compile_scope(
                load_scope_declaration(BOT_YML),
                workspace_ctx=_workspace_ctx(),
                registry=_shipped_registry(),
            )
        )
        assert compiled[("coder", "orchestrator")].spec.pool_name == "orchestrator"
        assert compiled[("coder", "explore")].spec.pool_name == "orchestrator"
        assert compiled[("default", "office-expert")].spec.pool_name == "default"

    def test_output_covers_every_declared_agent(self) -> None:
        spec = load_scope_declaration(BOT_YML)
        compilation = compile_scope(
            spec, workspace_ctx=_workspace_ctx(), registry=_shipped_registry()
        )
        declared = {(p.name, a.name) for p in _pools(spec) for a in p.agents}
        assert set(_by_key(compilation)) == declared
        assert len(compilation.agents) == 9


# ─── (b) §5.2 derivation table on a three-level tree ────────────────────────


def _three_level_spec() -> ScopeSpec:
    # root → {mid → leaf, helper}: the mid level carries a grandchild under
    # root's subtree — root's task must list mid+helper only, never leaf.
    return ScopeSpec(
        kind=ScopeKind.POOL,
        pool=PoolSpec(
            name="t",
            agents=[
                AgentSpec(name="root"),
                AgentSpec(name="mid", parent="root"),
                AgentSpec(name="helper", parent="root"),
                AgentSpec(name="leaf", parent="mid"),
            ],
        ),
    )


class TestDerivationTable:
    """The §5.2 derivation table — now delivered through the subagents
    capability's ``derived_tools`` channel (the retired hardcoded
    ``_derived_entries`` died with the capability migration); these tests
    compile with the DefaultPlugin registry so the capability resolves."""

    def _compiled(self) -> dict[tuple[str, str], CompiledAgent]:
        return _by_key(
            compile_scope(
                _three_level_spec(),
                workspace_ctx=_workspace_ctx(),
                registry=_shipped_registry(),
            )
        )

    def test_root_task_lists_direct_children_only(self) -> None:
        root = self._compiled()[("t", "root")]
        task = _entry(root.provenance, TASK)
        assert task is not None
        assert task.origin is ToolOrigin.DERIVED_TASK
        # Direct children only — the grandchild (leaf) belongs to mid's task.
        assert task.targets == ["mid", "helper"]
        assert root.spec.tools == _preset_names(ToolPreset.FULL) + [TASK]

    def test_mid_level_gets_task_and_send_to_agent(self) -> None:
        mid = self._compiled()[("t", "mid")]
        task = _entry(mid.provenance, TASK)
        assert task is not None
        assert task.targets == ["leaf"]
        send = _entry(mid.provenance, SEND_TO_AGENT)
        assert send is not None
        assert send.origin is ToolOrigin.DERIVED_SEND_TO_AGENT
        assert send.targets == ["root"]
        assert mid.spec.tools == _preset_names(ToolPreset.READ_WRITE) + [TASK, SEND_TO_AGENT]

    def test_leaves_have_no_task_entry_at_all(self) -> None:
        # SPEC §3.2: a leaf's assembly output has NO task tool — not an
        # enabled-but-empty one.
        for name in ("helper", "leaf"):
            agent = self._compiled()[("t", name)]
            assert _entry(agent.provenance, TASK) is None
            assert TASK not in agent.spec.tools

    def test_leaf_send_to_agent_targets_own_parent(self) -> None:
        leaf = self._compiled()[("t", "leaf")]
        send = _entry(leaf.provenance, SEND_TO_AGENT)
        assert send is not None
        assert send.targets == ["mid"]
        assert leaf.spec.tools == _preset_names(ToolPreset.READ_WRITE) + [SEND_TO_AGENT]

    def test_root_without_links_has_no_send_to_peer(self) -> None:
        root = self._compiled()[("t", "root")]
        assert _entry(root.provenance, SEND_TO_PEER) is None
        assert SEND_TO_PEER not in root.spec.tools

    def test_roots_with_links_get_send_to_peer(self) -> None:
        spec = ScopeSpec(
            kind=ScopeKind.WORKSPACE,
            workspace=WorkspaceSpec(
                name="w",
                pools=[
                    PoolSpec(name="a", agents=[AgentSpec(name="main-a")], peers=["b"]),
                    PoolSpec(name="b", agents=[AgentSpec(name="main-b")], peers=["a"]),
                ],
            ),
        )
        compiled = _by_key(
            compile_scope(spec, workspace_ctx=_workspace_ctx(), registry=_shipped_registry())
        )
        for pool, peer in (("a", ["b"]), ("b", ["a"])):
            agent = compiled[(pool, f"main-{pool}")]
            entry = _entry(agent.provenance, SEND_TO_PEER)
            assert entry is not None
            assert entry.origin is ToolOrigin.DERIVED_SEND_TO_PEER
            assert entry.targets == peer
            assert SEND_TO_PEER in agent.spec.tools
            # No declared children → no task even though the agent is a root.
            assert _entry(agent.provenance, TASK) is None

    def test_effective_tools_equal_spec_tools(self) -> None:
        # SPEC §5.2: V6's "effective toolset" IS the derived spec.tools.
        compilation = compile_scope(
            load_scope_declaration(BOT_YML),
            workspace_ctx=_workspace_ctx(),
            registry=_shipped_registry(),
        )
        for agent in compilation.agents:
            assert agent.effective.tools == agent.spec.tools
            assert agent.effective.pool == agent.provenance.pool
            assert agent.effective.agent == agent.provenance.agent


# ─── (c)/(g) Capability same-name replacement accounting (O3) ───────────────


class TestCapabilityReplacementAccounting:
    """The shipped declaration's ``capabilities: {aci: {}}`` blocks ride
    the generic O3 replacement machinery: ``edit ← aci_edit``."""

    def _compiled(self) -> dict[tuple[str, str], CompiledAgent]:
        return _by_key(
            compile_scope(
                load_scope_declaration(BOT_YML),
                workspace_ctx=_workspace_ctx(),
                registry=_shipped_registry(),
            )
        )

    def test_aci_replacement_recorded_and_queryable(self) -> None:
        root = self._compiled()[("default", "default")]
        expected = ToolReplacement(
            default_tool="edit",
            replacement_tool="aci_edit",
            capability="aci",
        )
        assert root.provenance.replacements == [expected]
        # Queryable in the pure-function boundary (AC g): the O3 record for
        # a default tool name is retrievable, None when not replaced.
        assert root.provenance.replacement_of("edit") == expected
        assert root.provenance.replacement_of("write") is None

    def test_replaced_default_entry_absent_and_replacement_annotated(self) -> None:
        root = self._compiled()[("default", "default")]
        assert "edit" not in root.spec.tools
        entry = _entry(root.provenance, "aci_edit")
        assert entry is not None
        assert entry.origin is ToolOrigin.CAPABILITY_DERIVED
        assert entry.capability == "aci"
        assert entry.replaces == "edit"

    def test_pools_without_aci_keep_plain_edit(self) -> None:
        # review's root and general declare the ast_grep capability (no
        # aci): the default edit survives, no replacement records.
        for key in (("review", "reviewer"), ("review", "general")):
            agent = self._compiled()[key]
            assert agent.provenance.replacements == []
            assert agent.provenance.replacement_of("edit") is None
            assert "edit" in agent.spec.tools
            assert _entry(agent.provenance, "edit") is not None

    def test_office_expert_full_origin_map(self) -> None:
        office = self._compiled()[("default", "office-expert")]
        origins = {e.tool: e.origin for e in office.provenance.tools}
        assert origins == {
            **dict.fromkeys(_preset_names(ToolPreset.READ_WRITE), ToolOrigin.PRESET),
            SEND_TO_AGENT: ToolOrigin.DERIVED_SEND_TO_AGENT,
            "todo_read": ToolOrigin.CAPABILITY_DERIVED,
            "todo_write": ToolOrigin.CAPABILITY_DERIVED,
            "aci_edit": ToolOrigin.CAPABILITY_DERIVED,
        }
        send = _entry(office.provenance, SEND_TO_AGENT)
        assert send is not None
        assert send.targets == ["default"]


# ─── Per-field provenance layers (framework default ← profile ← local) ──────


class TestProvenanceLayers:
    def _single_pool(self, *agents: AgentSpec) -> ScopeSpec:
        return ScopeSpec(kind=ScopeKind.POOL, pool=PoolSpec(name="p", agents=list(agents)))

    def _layers(self, agent: CompiledAgent) -> dict[str, ProvenanceLayer]:
        return {f.field: f.layer for f in agent.provenance.fields}

    def test_provenance_field_face(self) -> None:
        compilation = compile_scope(
            self._single_pool(AgentSpec(name="root"), AgentSpec(name="sub", parent="root")),
            workspace_ctx=_workspace_ctx(),
        )
        for agent in compilation.agents:
            assert {f.field for f in agent.provenance.fields} == {
                "toolset",
                "tools",
                "capabilities",
                "hooks",
                "eager",
                "max_steps",
                "memory",
            }

    def test_shipped_layers_framework_and_local(self) -> None:
        compiled = _by_key(
            compile_scope(
                load_scope_declaration(BOT_YML),
                workspace_ctx=_workspace_ctx(),
                registry=_shipped_registry(),
            )
        )
        # explore declares toolset read_only → LOCAL; office-expert leaves it
        # to the position default → FRAMEWORK. Declared max_steps → LOCAL.
        explore = compiled[("coder", "explore")]
        assert self._layers(explore)["toolset"] is ProvenanceLayer.LOCAL
        office = compiled[("default", "office-expert")]
        assert self._layers(office)["toolset"] is ProvenanceLayer.FRAMEWORK
        assert self._layers(office)["max_steps"] is ProvenanceLayer.LOCAL
        assert office.spec.max_iterations == 100

    def test_undeclared_max_steps_is_framework(self) -> None:
        compilation = compile_scope(
            self._single_pool(AgentSpec(name="root"), AgentSpec(name="sub", parent="root")),
            workspace_ctx=_workspace_ctx(),
        )
        sub = _by_key(compilation)[("p", "sub")]
        assert self._layers(sub)["max_steps"] is ProvenanceLayer.FRAMEWORK
        assert sub.spec.max_iterations == 100
        assert self._layers(sub)["eager"] is ProvenanceLayer.FRAMEWORK
        assert sub.defaults.registration is RegistrationTiming.LAZY

    def test_profile_layer_applies_when_local_unset(self) -> None:
        # A store profile extending the "read_write" preset (the sub's
        # position binding) contributes the profile layer where the agent
        # declares nothing.
        profiles = dict(STANDARD_PROFILES.profiles)
        profiles["read_write"] = Profile(
            name="read_write",
            toolset=ToolPreset.READ_WRITE,
            max_steps=60,
            eager=True,
            memory=MemoryDeclaration(session=SessionMemoryOverride(max_context_tokens=32000)),
        )
        compilation = compile_scope(
            self._single_pool(AgentSpec(name="root"), AgentSpec(name="sub", parent="root")),
            workspace_ctx=_workspace_ctx(),
            profiles=ProfileStore(profiles=profiles),
        )
        sub = _by_key(compilation)[("p", "sub")]
        assert sub.spec.max_iterations == 60
        assert sub.spec.memory_overrides.max_context_tokens == 32000
        assert sub.defaults.registration is RegistrationTiming.EAGER
        layers = self._layers(sub)
        assert layers["max_steps"] is ProvenanceLayer.PROFILE
        assert layers["memory"] is ProvenanceLayer.PROFILE
        assert layers["eager"] is ProvenanceLayer.PROFILE
        profile_names = {
            f.field: f.profile for f in sub.provenance.fields if f.layer is ProvenanceLayer.PROFILE
        }
        assert profile_names == {
            "max_steps": "read_write",
            "memory": "read_write",
            "eager": "read_write",
        }

    def test_local_declaration_wins_over_profile(self) -> None:
        profiles = dict(STANDARD_PROFILES.profiles)
        profiles["full"] = Profile(name="full", toolset=ToolPreset.FULL, max_steps=60)
        compilation = compile_scope(
            self._single_pool(AgentSpec(name="root", max_steps=50)),
            workspace_ctx=_workspace_ctx(),
            profiles=ProfileStore(profiles=profiles),
        )
        root = _by_key(compilation)[("p", "root")]
        assert root.spec.max_iterations == 50
        assert self._layers(root)["max_steps"] is ProvenanceLayer.LOCAL

    def test_profile_tools_list_is_wholesale(self) -> None:
        # O4/V8: a profile tools list IS the whole list — no preset expansion,
        # no derived entries (V6 guards the task drop at boot).
        profiles = dict(STANDARD_PROFILES.profiles)
        profiles["read_write"] = Profile(
            name="read_write", toolset=ToolPreset.READ_WRITE, tools=["read", "write"]
        )
        compilation = compile_scope(
            self._single_pool(AgentSpec(name="root"), AgentSpec(name="sub", parent="root")),
            workspace_ctx=_workspace_ctx(),
            profiles=ProfileStore(profiles=profiles),
        )
        sub = _by_key(compilation)[("p", "sub")]
        assert sub.spec.tools == ["read", "write"]
        origins = {e.tool: e.origin for e in sub.provenance.tools}
        assert origins == {
            "read": ToolOrigin.PROFILE_TOOLS,
            "write": ToolOrigin.PROFILE_TOOLS,
        }


# ─── (d) Byte stability ─────────────────────────────────────────────────────


class TestByteStability:
    def test_same_input_compiles_byte_identical(self) -> None:
        spec = load_scope_declaration(BOT_YML)
        first = compile_scope(spec, workspace_ctx=_workspace_ctx(), registry=_shipped_registry())
        second = compile_scope(spec, workspace_ctx=_workspace_ctx(), registry=_shipped_registry())
        assert len(first.agents) == 9  # non-trivial payload
        # workspace_ctx is a runtime object — excluded from the byte-stable
        # comparison (ticket 18 hashes the rest).
        exclude = {"agents": {"__all__": {"spec": {"workspace_ctx": True}}}}
        assert first.model_dump_json(exclude=exclude) == second.model_dump_json(exclude=exclude)


# ─── (e) Phase-2 validation driven by real compiler output ──────────────────


class TestValidatorPhase2Integration:
    def test_shipped_compilation_passes_v6_v9(self) -> None:
        spec = load_scope_declaration(BOT_YML)
        compilation = compile_scope(
            spec, workspace_ctx=_workspace_ctx(), registry=_shipped_registry()
        )
        configs = [a.effective for a in compilation.agents]
        assert validate_effective_configs(spec, configs) == []

    def test_wholesale_tools_dropping_task_fails_v6(self) -> None:
        # An unprefixed tools list IS the whole toolset (O4/V8): no task is
        # injected, and the child-carrying root is a silent orphan — V6 red.
        spec = ScopeSpec(
            kind=ScopeKind.POOL,
            pool=PoolSpec(
                name="p",
                agents=[
                    AgentSpec(name="root", tools=["bash", "edit"]),
                    AgentSpec(name="sub", parent="root"),
                ],
            ),
        )
        compilation = compile_scope(spec, workspace_ctx=_workspace_ctx())
        root = _by_key(compilation)[("p", "root")]
        assert root.spec.tools == ["bash", "edit"]
        issues = validate_effective_configs(spec, [a.effective for a in compilation.agents])
        task_issues = [i for i in issues if i.rule is RuleId.TASK_TOOL_PRESENT]
        assert len(task_issues) == 1
        assert task_issues[0].node == "root"
