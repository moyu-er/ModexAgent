"""Ticket 03 — ScopeTreeValidator two-phase validation matrix (SPEC §7).

Phase 1 (``validate_declaration``): declaration shape, pre-derivation —
V1 acyclic, V2 connected, V3 single root, V4 kind hierarchy, V5 peer
topology, V7 profile single-level references, V10 graph agent references,
V11 name uniqueness. Phase 2 (``validate_effective_configs``): effective
values, post-derivation — V6 ``task`` in the derived toolset of
child-carrying agents, V9 non-root approval. Phase-2 inputs are hand-built
fixtures standing in for compiler output until ticket 06.

Every rule has a positive and a negative case. V8 (wholesale list
replacement) is documentation semantics — no runtime rule.
"""

from __future__ import annotations

from pathlib import Path

from modex_agent.ioc.configs.approval import ApprovalConfig
from modex_agent.scope import (
    AgentSpec,
    EffectiveAgentConfig,
    GraphAgentReference,
    PoolSpec,
    ProfileDeclaration,
    RuleId,
    ScopeKind,
    ScopeSpec,
    ScopeValidationIssue,
    WorkspaceSpec,
    load_scope_declaration,
    validate_declaration,
    validate_effective_configs,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "scope"
REPO_ROOT = Path(__file__).resolve().parents[3]
BOT_YML = REPO_ROOT / "examples" / "bot_project" / "config" / "scopes" / "bot.yml"
GRAPHS_DIR = REPO_ROOT / "examples" / "bot_project" / "config" / "graphs"


# -- tree builders -----------------------------------------------------------


def _pool(name: str, agents: list[AgentSpec], peers: list[str] | None = None) -> PoolSpec:
    return PoolSpec(name=name, agents=agents, peers=peers or [])


def _workspace(pools: list[PoolSpec], name: str = "ws") -> ScopeSpec:
    return ScopeSpec(
        kind=ScopeKind.WORKSPACE,
        workspace=WorkspaceSpec(name=name, pools=pools),
    )


def _standalone(pool: PoolSpec) -> ScopeSpec:
    return ScopeSpec(kind=ScopeKind.POOL, pool=pool)


def _issues_for(issues: list[ScopeValidationIssue], rule: RuleId) -> list[ScopeValidationIssue]:
    return [i for i in issues if i.rule is rule]


# -- V1: acyclic --------------------------------------------------------------


class TestV1Acyclic:
    def test_linear_tree_is_acyclic(self) -> None:
        spec = _workspace(
            [
                _pool(
                    "p",
                    [
                        AgentSpec(name="root"),
                        AgentSpec(name="a", parent="root"),
                        AgentSpec(name="b", parent="a"),
                    ],
                )
            ]
        )
        assert validate_declaration(spec) == []

    def test_two_node_parent_cycle_reported(self) -> None:
        spec = _workspace(
            [
                _pool(
                    "p",
                    [
                        AgentSpec(name="a", parent="b"),
                        AgentSpec(name="b", parent="a"),
                    ],
                )
            ]
        )
        issues = _issues_for(validate_declaration(spec), RuleId.ACYCLIC)
        assert len(issues) == 1
        assert issues[0].node == "a"
        assert "a → b → a" in issues[0].message
        assert "cycle" in issues[0].message

    def test_self_parent_cycle_reported(self) -> None:
        spec = _workspace([_pool("p", [AgentSpec(name="a", parent="a")])])
        issues = _issues_for(validate_declaration(spec), RuleId.ACYCLIC)
        assert len(issues) == 1
        assert issues[0].node == "a"
        assert "a → a" in issues[0].message

    def test_cycle_reported_once_not_per_member(self) -> None:
        # A cycle fires one issue naming the cycle, not one per member.
        spec = _workspace(
            [
                _pool(
                    "p",
                    [
                        AgentSpec(name="a", parent="c"),
                        AgentSpec(name="b", parent="a"),
                        AgentSpec(name="c", parent="b"),
                    ],
                )
            ]
        )
        issues = _issues_for(validate_declaration(spec), RuleId.ACYCLIC)
        assert len(issues) == 1


# -- V2: connected ------------------------------------------------------------


class TestV2Connected:
    def test_all_nodes_reachable_from_root(self) -> None:
        spec = _workspace(
            [
                _pool(
                    "p",
                    [
                        AgentSpec(name="root"),
                        AgentSpec(name="a", parent="root"),
                        AgentSpec(name="b", parent="a"),
                    ],
                )
            ]
        )
        assert validate_declaration(spec) == []

    def test_cycle_members_unreachable_from_root(self) -> None:
        # root is reachable; the a↔b cycle members are not — V2 names them.
        spec = _workspace(
            [
                _pool(
                    "p",
                    [
                        AgentSpec(name="root"),
                        AgentSpec(name="child", parent="root"),
                        AgentSpec(name="a", parent="b"),
                        AgentSpec(name="b", parent="a"),
                    ],
                )
            ]
        )
        issues = _issues_for(validate_declaration(spec), RuleId.CONNECTED)
        assert {i.node for i in issues} == {"a", "b"}
        assert all("reachable" in i.message for i in issues)


# -- V3: exactly one root -----------------------------------------------------


class TestV3SingleRoot:
    def test_single_root_accepted(self) -> None:
        spec = _workspace(
            [
                _pool(
                    "p",
                    [AgentSpec(name="root"), AgentSpec(name="a", parent="root")],
                )
            ]
        )
        assert validate_declaration(spec) == []

    def test_two_roots_reported(self) -> None:
        spec = _workspace(
            [_pool("p", [AgentSpec(name="a"), AgentSpec(name="b")])]
        )
        issues = _issues_for(validate_declaration(spec), RuleId.SINGLE_ROOT)
        assert len(issues) == 1
        assert issues[0].node == "p"
        assert "'a'" in issues[0].message and "'b'" in issues[0].message

    def test_zero_roots_reported(self) -> None:
        # Zero roots requires a parent cycle (V1 fires too) — V3 still names
        # the missing root.
        spec = _workspace(
            [
                _pool(
                    "p",
                    [
                        AgentSpec(name="a", parent="b"),
                        AgentSpec(name="b", parent="a"),
                    ],
                )
            ]
        )
        issues = _issues_for(validate_declaration(spec), RuleId.SINGLE_ROOT)
        assert len(issues) == 1
        assert issues[0].node == "p"
        assert "no root" in issues[0].message


# -- V4: kind hierarchy -------------------------------------------------------


class TestV4KindHierarchy:
    def test_deep_agent_nesting_accepted(self) -> None:
        # Agents are pool-internal data — nesting depth is unlimited.
        spec = _workspace(
            [
                _pool(
                    "p",
                    [
                        AgentSpec(name="l0"),
                        AgentSpec(name="l1", parent="l0"),
                        AgentSpec(name="l2", parent="l1"),
                        AgentSpec(name="l3", parent="l2"),
                        AgentSpec(name="l4", parent="l3"),
                        AgentSpec(name="l5", parent="l4"),
                    ],
                )
            ]
        )
        assert validate_declaration(spec) == []

    def test_pool_as_root_form_accepted(self) -> None:
        spec = _standalone(
            _pool("solo", [AgentSpec(name="root"), AgentSpec(name="a", parent="root")])
        )
        assert validate_declaration(spec) == []

    def test_form_kind_mismatch_reported(self) -> None:
        # ScopeSpec's own validator already rejects this shape at
        # construction; the re-check keeps the validator self-contained for
        # bypassed input (TopologyValidator precedent).
        pool = _pool("p", [AgentSpec(name="root")])
        spec = ScopeSpec.model_construct(
            kind=ScopeKind.WORKSPACE, workspace=None, pool=pool
        )
        issues = validate_declaration(spec)
        assert [i.rule for i in issues] == [RuleId.KIND_HIERARCHY]
        assert issues[0].node == "workspace"


# -- V5: peer topology --------------------------------------------------------


class TestV5PeerTopology:
    def test_bidirectional_same_workspace_peers_accepted(self) -> None:
        # Pool-level links + unique roots per pool: the link is root-to-root
        # by construction (ADR-0019 semantics).
        spec = _workspace(
            [
                _pool("a", [AgentSpec(name="root-a")], peers=["b"]),
                _pool("b", [AgentSpec(name="root-b")], peers=["a"]),
            ]
        )
        assert validate_declaration(spec) == []

    def test_missing_endpoint_reported(self) -> None:
        # A peer name not declared in this workspace — the same error shape
        # covers cross-workspace links, which v1 forbids (N5).
        spec = _workspace([_pool("a", [AgentSpec(name="root-a")], peers=["ghost"])])
        issues = _issues_for(validate_declaration(spec), RuleId.PEER_TOPOLOGY)
        assert len(issues) == 1
        assert issues[0].node == "a"
        assert "'ghost'" in issues[0].message
        assert "workspace" in issues[0].message

    def test_non_bidirectional_link_reported(self) -> None:
        spec = _workspace(
            [
                _pool("a", [AgentSpec(name="root-a")], peers=["b"]),
                _pool("b", [AgentSpec(name="root-b")]),
            ]
        )
        issues = _issues_for(validate_declaration(spec), RuleId.PEER_TOPOLOGY)
        assert len(issues) == 1
        # The fix lands in the pool missing the back-reference.
        assert issues[0].node == "b"
        assert "'a'" in issues[0].message
        assert "bidirectional" in issues[0].message

    def test_pool_as_root_with_peers_reported(self) -> None:
        # "Same workspace" has no meaning for a pool-as-root declaration —
        # v1 refuses peers there outright.
        spec = _standalone(_pool("solo", [AgentSpec(name="root")], peers=["a"]))
        issues = _issues_for(validate_declaration(spec), RuleId.PEER_TOPOLOGY)
        assert len(issues) == 1
        assert issues[0].node == "solo"
        assert "pool-as-root" in issues[0].message


# -- V7: profile single-level references --------------------------------------


class TestV7ProfileSingleLevel:
    def test_profiles_without_references_accepted(self) -> None:
        spec = _workspace([_pool("p", [AgentSpec(name="root")])])
        profiles = [ProfileDeclaration(name="std"), ProfileDeclaration(name="bot")]
        assert validate_declaration(spec, profiles=profiles) == []

    def test_profile_referencing_profile_reported(self) -> None:
        spec = _workspace([_pool("p", [AgentSpec(name="root")])])
        profiles = [ProfileDeclaration(name="bot", profile="std")]
        issues = _issues_for(
            validate_declaration(spec, profiles=profiles),
            RuleId.PROFILE_SINGLE_LEVEL,
        )
        assert len(issues) == 1
        assert issues[0].node == "bot"
        assert "'std'" in issues[0].message


# -- V10: graph agent references ----------------------------------------------


class TestV10GraphAgentReferences:
    def test_declared_agent_reference_accepted(self) -> None:
        spec = _workspace(
            [
                _pool(
                    "p",
                    [AgentSpec(name="root"), AgentSpec(name="sub", parent="root")],
                )
            ]
        )
        refs = [
            GraphAgentReference(graph="g", node="n1", pool="p", agent="root"),
            GraphAgentReference(graph="g", node="n2", pool="p", agent="sub"),
        ]
        assert validate_declaration(spec, graph_agent_refs=refs) == []

    def test_unknown_pool_reported(self) -> None:
        spec = _workspace([_pool("p", [AgentSpec(name="root")])])
        refs = [GraphAgentReference(graph="g", node="n", pool="nope", agent="root")]
        issues = _issues_for(
            validate_declaration(spec, graph_agent_refs=refs),
            RuleId.GRAPH_AGENT_REFERENCE,
        )
        assert len(issues) == 1
        assert issues[0].node == "n"
        assert "'nope'" in issues[0].message

    def test_unknown_agent_reported(self) -> None:
        spec = _workspace([_pool("p", [AgentSpec(name="root")])])
        refs = [GraphAgentReference(graph="g", node="n", pool="p", agent="ghost")]
        issues = _issues_for(
            validate_declaration(spec, graph_agent_refs=refs),
            RuleId.GRAPH_AGENT_REFERENCE,
        )
        assert len(issues) == 1
        assert issues[0].node == "n"
        assert "'ghost'" in issues[0].message
        assert "'p'" in issues[0].message


# -- V11: name uniqueness -----------------------------------------------------


class TestV11NameUniqueness:
    def test_distinct_names_accepted(self) -> None:
        spec = _workspace(
            [
                _pool(
                    "p",
                    [AgentSpec(name="root"), AgentSpec(name="a", parent="root")],
                )
            ]
        )
        assert validate_declaration(spec) == []

    def test_duplicate_agent_names_within_pool_reported(self) -> None:
        # Same name under different parents still collides: the flat model
        # keys agents by name alone.
        spec = _workspace(
            [
                _pool(
                    "p",
                    [
                        AgentSpec(name="root"),
                        AgentSpec(name="a", parent="root"),
                        AgentSpec(name="b", parent="root"),
                        AgentSpec(name="a", parent="b"),
                    ],
                )
            ]
        )
        issues = _issues_for(validate_declaration(spec), RuleId.NAME_UNIQUENESS)
        assert len(issues) == 1
        assert issues[0].node == "a"
        assert "'p'" in issues[0].message

    def test_duplicate_pool_names_within_workspace_reported(self) -> None:
        spec = _workspace(
            [_pool("dup", [AgentSpec(name="root")]), _pool("dup", [AgentSpec(name="root")])]
        )
        issues = _issues_for(validate_declaration(spec), RuleId.NAME_UNIQUENESS)
        assert len(issues) == 1
        assert issues[0].node == "dup"


# -- V6: task tool present (phase 2) -------------------------------------------


class TestV6TaskToolPresent:
    def _spec(self) -> ScopeSpec:
        return _workspace(
            [
                _pool(
                    "p",
                    [AgentSpec(name="root"), AgentSpec(name="sub", parent="root")],
                )
            ]
        )

    def test_parent_with_task_in_toolset_accepted(self) -> None:
        configs = [
            EffectiveAgentConfig(pool="p", agent="root", tools=["bash", "task"]),
            EffectiveAgentConfig(pool="p", agent="sub", tools=["send_to_agent"]),
        ]
        assert validate_effective_configs(self._spec(), configs) == []

    def test_leaf_without_task_accepted(self) -> None:
        # Only child-carrying agents need `task`; leaves may drop it.
        configs = [
            EffectiveAgentConfig(pool="p", agent="root", tools=["task"]),
            EffectiveAgentConfig(pool="p", agent="sub", tools=[]),
        ]
        assert validate_effective_configs(self._spec(), configs) == []

    def test_wholesale_tool_list_dropping_task_reported(self) -> None:
        # The AC (c) case: an explicit tools list that replaces the profile
        # selection and drops `task` while children are declared.
        configs = [
            EffectiveAgentConfig(pool="p", agent="root", tools=["bash", "edit"]),
            EffectiveAgentConfig(pool="p", agent="sub", tools=["send_to_agent"]),
        ]
        issues = _issues_for(
            validate_effective_configs(self._spec(), configs),
            RuleId.TASK_TOOL_PRESENT,
        )
        assert len(issues) == 1
        assert issues[0].node == "root"
        assert "'task'" in issues[0].message

    def test_missing_effective_toolset_reported(self) -> None:
        # Phase-2 input must cover every declared agent — a child-carrying
        # agent with no derived toolset is exactly the silent-orphan shape.
        configs = [
            EffectiveAgentConfig(pool="p", agent="sub", tools=["send_to_agent"]),
        ]
        issues = _issues_for(
            validate_effective_configs(self._spec(), configs),
            RuleId.TASK_TOOL_PRESENT,
        )
        assert len(issues) == 1
        assert issues[0].node == "root"


# -- V9: non-root approval (phase 2) -------------------------------------------


class TestV9NonRootApproval:
    def _spec(self) -> ScopeSpec:
        return _workspace(
            [
                _pool(
                    "p",
                    [AgentSpec(name="root"), AgentSpec(name="sub", parent="root")],
                )
            ]
        )

    def test_root_approval_accepted(self) -> None:
        spec = _workspace(
            [
                _pool(
                    "p",
                    [
                        AgentSpec(name="root", approval=ApprovalConfig(enabled=True)),
                        AgentSpec(name="sub", parent="root"),
                    ],
                )
            ]
        )
        configs = [
            EffectiveAgentConfig(pool="p", agent="root", tools=["task"]),
            EffectiveAgentConfig(pool="p", agent="sub", tools=["send_to_agent"]),
        ]
        assert validate_effective_configs(spec, configs) == []

    def test_non_root_approval_reported(self) -> None:
        spec = _workspace(
            [
                _pool(
                    "p",
                    [
                        AgentSpec(name="root"),
                        AgentSpec(
                            name="sub", parent="root", approval=ApprovalConfig()
                        ),
                    ],
                )
            ]
        )
        configs = [
            EffectiveAgentConfig(pool="p", agent="root", tools=["task"]),
            EffectiveAgentConfig(pool="p", agent="sub", tools=["send_to_agent"]),
        ]
        issues = _issues_for(
            validate_effective_configs(spec, configs),
            RuleId.NON_ROOT_APPROVAL,
        )
        assert len(issues) == 1
        assert issues[0].node == "sub"
        assert "approval" in issues[0].message


# -- shipped configs (AC b) ----------------------------------------------------


def _shipped_graph_agent_refs() -> list[GraphAgentReference]:
    """Extract (pool, agent) refs from the shipped graph specs.

    Mirrors BotAgentNodeConfig (agent required, pool defaults to
    "default") — the extraction shape boot (ticket 07/08) feeds to V10.
    """
    from modex_agent.graph.spec_loader import GraphSpecLoader
    from modex_graph import InMemoryGraphSpecStore

    specs = GraphSpecLoader(InMemoryGraphSpecStore()).load_from_dir(GRAPHS_DIR)
    refs: list[GraphAgentReference] = []
    for graph_spec in specs:
        for node in graph_spec.nodes:
            if node.node_type != "agent":
                continue
            agent = node.config.get("agent")
            if not isinstance(agent, str):
                continue
            pool = node.config.get("pool", "default")
            refs.append(
                GraphAgentReference(
                    graph=graph_spec.name,
                    node=node.name,
                    pool=str(pool),
                    agent=agent,
                )
            )
    return refs


class TestShippedDeclaration:
    def test_shipped_bot_yml_validates_clean_with_graphs(self) -> None:
        # Real file path through the real loader — not an inline tree. The
        # shipped graph (review_cycle.yml) references (review, reviewer)
        # and (coder, orchestrator); both resolve in bot.yml.
        spec = load_scope_declaration(BOT_YML)
        assert validate_declaration(
            spec, graph_agent_refs=_shipped_graph_agent_refs()
        ) == []

    def test_shipped_graph_reference_form(self) -> None:
        # Records the V10 input form drawn from the shipped graph specs.
        refs = _shipped_graph_agent_refs()
        assert [(r.pool, r.agent) for r in refs] == [
            ("review", "reviewer"),
            ("coder", "orchestrator"),
        ]

    def test_shipped_graph_typo_fails_v10(self) -> None:
        spec = load_scope_declaration(BOT_YML)
        bad = GraphAgentReference(
            graph="review_cycle", node="reviewer", pool="review", agent="reviwer"
        )
        issues = validate_declaration(spec, graph_agent_refs=[bad])
        assert [i.rule for i in issues] == [RuleId.GRAPH_AGENT_REFERENCE]
        assert "reviwer" in issues[0].message

    def test_shipped_phase2_clean_with_hand_built_toolsets(self) -> None:
        # Hand-built compiler-output fixture: every child-carrying agent
        # keeps `task`. Ticket 06 replaces this with real compiler output.
        spec = load_scope_declaration(BOT_YML)
        assert spec.workspace is not None
        configs: list[EffectiveAgentConfig] = []
        for pool in spec.workspace.pools:
            for agent in pool.agents:
                has_children = any(a.parent == agent.name for a in pool.agents)
                if has_children:
                    tools = ["task", "bash"]
                elif agent.parent is not None:
                    tools = ["send_to_agent"]
                else:
                    tools = ["bash"]
                configs.append(
                    EffectiveAgentConfig(
                        pool=pool.name, agent=agent.name, tools=tools
                    )
                )
        assert validate_effective_configs(spec, configs) == []

    def test_fixture_declarations_validate_clean(self) -> None:
        # The ticket-02 fixtures (three-level nesting + pool-as-root) pass
        # phase 1 through the real loader.
        for name in ("nested.yml", "pool-as-root.yml"):
            spec = load_scope_declaration(FIXTURES / name)
            assert validate_declaration(spec) == [], name
