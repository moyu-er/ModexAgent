"""Ticket 02 — shipped-declaration goldens: the bot.yml contract.

Originally the ticket-02 split-brain baseline (bot.yml ≡ shipped
pool.yml + templates, freezing the migration baseline for tickets
05-10). Ticket 11 deleted the legacy road (``config/pools`` +
``PoolStore``), so the legacy leg died with it. What remains freezes
the shipped declaration itself — topology, field shapes,
position-derived profiles, memory eligibility, registration timing,
approval goldens — so every edit to bot.yml that changes observable
shape shows up as a golden diff.
"""

from __future__ import annotations

from pathlib import Path

from modex_agent.scope import (
    AgentSpec,
    MemoryPreset,
    PoolSpec,
    RegistrationTiming,
    effective_defaults,
    load_scope_declaration,
)
from modex_agent.tools.presets import ToolPreset

REPO_ROOT = Path(__file__).resolve().parents[3]
BOT_BASE = REPO_ROOT / "examples" / "bot_project"
BOT_YML = BOT_BASE / "config" / "scopes" / "bot.yml"

SHIPPED_POOLS = ("default", "coder", "review", "opencode")
# opencode is the external root — see TestOpencodeExternalRoot for why its
# tool-roster fields are not part of the comparison.
NATIVE_POOLS = ("default", "coder", "review")


def _scope_pools() -> dict[str, PoolSpec]:
    spec = load_scope_declaration(BOT_YML)
    assert spec.workspace is not None
    return {p.name: p for p in spec.workspace.pools}


def _root_of(pool: PoolSpec) -> AgentSpec:
    roots = [a for a in pool.agents if a.parent is None]
    assert len(roots) == 1
    return roots[0]


def _children_of(pool: PoolSpec, name: str) -> list[AgentSpec]:
    return [a for a in pool.agents if a.parent == name]


class TestShippedTopology:
    def test_all_four_pools_declared(self) -> None:
        assert set(_scope_pools()) == set(SHIPPED_POOLS)

    def test_peer_topology_is_bidirectional(self) -> None:
        # Fixed topology (pre-commit 985171b): default↔opencode, default↔review.
        pools = _scope_pools()
        assert pools["default"].peers == ["opencode", "review"]
        assert pools["review"].peers == ["default"]
        assert pools["opencode"].peers == ["default"]
        assert pools["coder"].peers == []
        # ADR-0019 bidirectional invariant: every edge has its reverse.
        for name, pool in pools.items():
            for peer in pool.peers:
                assert name in pools[peer].peers, f"{name}->{peer} lacks reverse"

    def test_root_names_golden(self) -> None:
        # The legacy root-name convention is now a declaration golden
        # (ticket 11: the declaration is the single source of truth).
        pools = _scope_pools()
        assert {name: _root_of(pool).name for name, pool in pools.items()} == {
            "default": "default",
            "coder": "orchestrator",
            "review": "reviewer",
            "opencode": "opencode",
        }

    def test_subagent_trees_golden(self) -> None:
        pools = _scope_pools()
        assert [a.name for a in _children_of(pools["default"], "default")] == [
            "office-expert"
        ]
        assert [a.name for a in _children_of(pools["coder"], "orchestrator")] == [
            "explore",
            "general",
        ]
        assert [a.name for a in _children_of(pools["review"], "reviewer")] == [
            "explore",
            "general",
        ]
        assert _children_of(pools["opencode"], "opencode") == []
        for name in NATIVE_POOLS:
            root = _root_of(pools[name])
            for child in _children_of(pools[name], root.name):
                assert child.parent == root.name


class TestShippedFieldGoldens:
    def test_native_root_hook_rosters_golden(self) -> None:
        # Ticket 09: every native root references the notification +
        # experience-review HOOK-slot components (the migrated glue);
        # default additionally collects references.
        pools = _scope_pools()
        assert _root_of(pools["default"]).hooks == [
            "+reference_collector",
            "+user_notice_cleanup",
            "+experience_review",
        ]
        for name in ("coder", "review"):
            assert _root_of(pools[name]).hooks == [
                "+user_notice_cleanup",
                "+experience_review",
            ]

    def test_native_roots_carry_descriptions(self) -> None:
        pools = _scope_pools()
        for name in SHIPPED_POOLS:
            assert _root_of(pools[name]).description, f"{name} root lost its description"

    def test_default_pool_roster_details(self) -> None:
        # The richest shipped pool: hook roster + hook config + approval +
        # mcp + one subagent — golden, field-for-field.
        pools = _scope_pools()
        root = _root_of(pools["default"])
        assert root.hooks == [
            "+reference_collector",
            "+user_notice_cleanup",
            "+experience_review",
        ]
        assert root.hook_configs == {"reference_collector": {"max_sources": 20}}
        assert root.approval is not None
        assert root.approval.enabled is True
        assert root.approval.tools["write"].allowed_paths == ["./*"]
        assert root.approval.tools["edit"].allowed_paths == ["./*"]
        assert root.mcp == ["playwright"]
        office = _children_of(pools["default"], "default")
        assert [a.name for a in office] == ["office-expert"]
        assert office[0].max_steps == 100
        assert office[0].context_mode.value == "fresh"


class TestShippedToolsetProfiles:
    def test_effective_profiles_golden(self) -> None:
        # Position-derived defaults (root full, subagent read_write) with
        # declared deviations: office-expert lands on read_write with NO
        # declaration; explore/general declare read_only/full explicitly.
        pools = _scope_pools()
        for name in NATIVE_POOLS:
            root = _root_of(pools[name])
            assert effective_defaults(root).toolset_profile == ToolPreset.FULL
        office = _children_of(pools["default"], "default")[0]
        assert effective_defaults(office).toolset_profile == ToolPreset.READ_WRITE
        assert office.toolset is None
        expected = {
            "coder": {"explore": ToolPreset.READ_ONLY, "general": ToolPreset.FULL},
            "review": {"explore": ToolPreset.READ_ONLY, "general": ToolPreset.FULL},
        }
        for pool_name, subs in expected.items():
            for sub_name, preset in subs.items():
                agent = next(
                    a for a in pools[pool_name].agents if a.name == sub_name
                )
                assert effective_defaults(agent).toolset_profile == preset

    def test_explore_and_general_declare_explicit_overrides(self) -> None:
        # read_only / full deviate from the non-root read-write default and
        # must be node-level declarations, not silent drift.
        pools = _scope_pools()
        for pool_name in ("coder", "review"):
            explore = next(
                a for a in pools[pool_name].agents if a.name == "explore"
            )
            general = next(
                a for a in pools[pool_name].agents if a.name == "general"
            )
            assert explore.toolset is not None
            assert explore.toolset.value == "read_only"
            assert general.toolset is not None
            assert general.toolset.value == "full"


class TestShippedMemoryEligibility:
    def test_native_roots_archive_core_experience_eligible(self) -> None:
        pools = _scope_pools()
        for name in NATIVE_POOLS:
            d = effective_defaults(_root_of(pools[name]))
            assert d.memory_preset is MemoryPreset.ARCHIVE_CORE_EXPERIENCE
            assert d.experience_enabled is True

    def test_subagents_session_only(self) -> None:
        pools = _scope_pools()
        for name in NATIVE_POOLS:
            pool = pools[name]
            root = _root_of(pool)
            for child in _children_of(pool, root.name):
                d = effective_defaults(child)
                assert d.memory_preset is MemoryPreset.SESSION_ONLY
                assert d.experience_enabled is False

    def test_memory_toggles_default_off(self) -> None:
        # No shipped pool carries a memory block → position defaults with
        # no overrides: archive/core toggles stay off everywhere.
        pools = _scope_pools()
        for name in SHIPPED_POOLS:
            d = effective_defaults(_root_of(pools[name]))
            assert d.archive_enabled is False
            assert d.core_enabled is False


class TestShippedRegistrationAndApproval:
    def test_roots_eager_subagents_lazy(self) -> None:
        pools = _scope_pools()
        for name in NATIVE_POOLS:
            pool = pools[name]
            root = _root_of(pool)
            assert effective_defaults(root).registration is RegistrationTiming.EAGER
            for child in _children_of(pool, root.name):
                assert effective_defaults(child).registration is RegistrationTiming.LAZY

    def test_approval_eligibility_is_positional(self) -> None:
        pools = _scope_pools()
        for name in NATIVE_POOLS:
            pool = pools[name]
            root = _root_of(pool)
            assert effective_defaults(root).approval_eligible is True
            for child in _children_of(pool, root.name):
                assert effective_defaults(child).approval_eligible is False

    def test_approval_configs_golden(self) -> None:
        # default + coder gate write/edit behind approval; review and the
        # external opencode root run free.
        pools = _scope_pools()
        for name in ("default", "coder"):
            approval = _root_of(pools[name]).approval
            assert approval is not None
            assert approval.enabled is True
            assert sorted(approval.tools) == ["edit", "write"]
            assert approval.tools["write"].allowed_paths == ["./*"]
            assert approval.tools["edit"].allowed_paths == ["./*"]
        assert _root_of(pools["review"]).approval is None
        assert _root_of(pools["opencode"]).approval is None


class TestOpencodeExternalRoot:
    """opencode (external root): identity + strategy goldens.

    Tool-roster fields are inert for external roots — the external strategy
    assembles no tools/memory (SPEC §4) — so the golden covers identity,
    strategy, peers, and tree shape only.
    """

    def test_identity_and_strategy(self) -> None:
        pools = _scope_pools()
        root = _root_of(pools["opencode"])
        assert root.name == "opencode"
        assert root.description.startswith("Autonomous software engineering agent")
        assert root.execution_strategy.value == "external"
        assert root.provider_kind is not None
        assert root.provider_kind.value == "opencode"
        assert pools["opencode"].peers == ["default"]

    def test_external_root_has_no_children(self) -> None:
        pools = _scope_pools()
        root = _root_of(pools["opencode"])
        assert _children_of(pools["opencode"], root.name) == []
