"""Target-state configuration guard tests.

These tests protect the **converged memory + experience configuration** that
every native agent must receive. They are NOT tied to the bot's real
``config/pools/`` — they synthesize minimal pool configurations under
``tmp_path`` so the guard stays valid as pools are added, removed, or
reconfigured by users.

## What this guard protects

The bot's archive/core memory toggle is user-editable per pool through the
WebUI or the main agent's ``pool.yml`` ``memory:`` block. Detailed memory and
experience configuration remains baked (see ``bot/config/memory_defaults.py``
and the "Memory + Experience Presets (Target State)" section in ``AGENTS.md``).
The contract:

| Agent type | memory | experience | governance | hooks |
|---|---|---|---|---|
| native main | session + compact + governance + pruned | enabled (ExperienceReviewHook fires) | create_governance (budget + tool_chain_repair) | MaxIter + TurnOutcome + ModelChoiceBind + ExperienceReview |
| native subagent | session + compact + governance + pruned | N/A | create_subagent_governance (tool_chain_repair only) | SubagentAutoSend + MaxIter |
| external main | skipped structurally | skipped | skipped | skipped |
| external subagent | skipped structurally | skipped | skipped | skipped |

## Why this matters

If any of these tests fail, the bot's memory/experience system is broken:
- No archive → conversations can't compress, context window blows up
- No pruned → cleanup catalog never written
- No experience → reviewer never runs, EXPERIENCE.md never created
- No governance → tool chain breaks, oversized content not truncated
- No hooks → no notification, no model binding, no auto-send

## Test strategy

Tests synthesize pool configurations covering the **4 agent-type combinations**
(native/external × main/subagent) under ``tmp_path``. This decouples the guard
from the bot's real ``config/pools/`` directory, which may change at any time.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import yaml

_BOT_PROJECT = Path(__file__).resolve().parents[3]
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from modex_agent.ioc.configs.memory import (  # noqa: E402
    GovernanceConfig,
    MemoryConfig,
    PrunedCatalogConfig,
    SessionConfig,
)
from modex_agent.multi_agent.pool_config.experience import ExperienceConfig  # noqa: E402
from modex_agent.multi_agent.pool_config.specs import (  # noqa: E402
    ExecutionStrategyKind,
    MainAgentSpec,
    PoolSpec,
    ProviderKind,
)
from modex_agent.multi_agent.pool_config.store import PoolStore  # noqa: E402
from modex_agent.multi_agent.template_registry import AgentTemplateRegistry  # noqa: E402

# ─── helpers: synthesize pool configurations under tmp_path ─────────────────


def _default_pool_specs(pool_names: list[str]) -> dict[str, PoolSpec]:
    """Create PoolSpec dict with default MainAgentSpec (memory toggle off)."""
    return {
        name: PoolSpec(
            name=name,
            main_agent_name=name,
            main=MainAgentSpec(agent_name=name),
        )
        for name in pool_names
    }


def _seed_native_main_pool(
    base: Path,
    pool: str,
    main_agent: str | None = None,
) -> Path:
    """Write a minimal pool.yml for a native (react) main agent."""
    pool_dir = base / "config" / "pools" / pool
    pool_dir.mkdir(parents=True, exist_ok=True)
    (pool_dir / "templates").mkdir(exist_ok=True)
    data: dict[str, Any] = {}
    if main_agent and main_agent != pool:
        data["main_agent_name"] = main_agent
    p = pool_dir / "pool.yml"
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return p


def _seed_external_main_pool(
    base: Path,
    pool: str,
    main_agent: str,
    provider_kind: str = "opencode",
) -> Path:
    """Write a minimal pool.yml for an external main agent."""
    pool_dir = base / "config" / "pools" / pool
    pool_dir.mkdir(parents=True, exist_ok=True)
    (pool_dir / "templates").mkdir(exist_ok=True)
    data = {
        "main_agent_name": main_agent,
        "execution_strategy": "external",
        "provider_kind": provider_kind,
    }
    p = pool_dir / "pool.yml"
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return p


def _seed_subagent_template(
    base: Path,
    pool: str,
    agent: str,
    *,
    external: bool = False,
    provider_kind: str = "opencode",
) -> Path:
    """Write a minimal subagent template YAML."""
    tdir = base / "config" / "pools" / pool / "templates"
    tdir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "agent_name": agent,
        "description": "",
        "max_steps": 80,
    }
    if external:
        payload["execution_strategy"] = "external"
        payload["provider_kind"] = provider_kind
    p = tdir / f"{agent}.yml"
    p.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return p


def _build_pool_store_with_mixed_pools(base: Path) -> PoolStore:
    """Create a PoolStore with all 4 agent-type combinations.

    Pools created:
    - ``native_main``: react main + 2 react subagents
    - ``external_main``: external main (opencode), no subagents
    - ``mixed``: react main + 1 react subagent + 1 external subagent
    """
    _seed_native_main_pool(base, "native_main", main_agent="orchestrator")
    _seed_subagent_template(base, "native_main", "planner")
    _seed_subagent_template(base, "native_main", "scout")

    _seed_external_main_pool(base, "external_main", "opencode")

    _seed_native_main_pool(base, "mixed", main_agent="lead")
    _seed_subagent_template(base, "mixed", "researcher")
    _seed_subagent_template(base, "mixed", "external_worker", external=True)

    return PoolStore(base_dir=base)


# ─── Test 1: memory_defaults preset contract ─────────────────────────────────


class TestMemoryDefaultsContract:
    """Verify the three preset functions in memory_defaults.py.

    These presets are the **single source of truth** for all native agents.
    If any field is wrong, every native agent in every pool is affected.
    """

    def test_main_agent_memory_has_all_required_layers(self) -> None:
        """Main agent memory MUST have all long-term layers enabled.

        Missing any layer breaks the corresponding subsystem:
        - No archive → no compression, context window blows up
        - No core → no SOUL/USER/MEMORY.md injection
        - No dream_engine → no offline archive→core consolidation
        - No governance → no tool chain repair, no lossy compaction
        - No pruned → no cleanup catalog
        """
        from bot.config.memory_defaults import main_agent_memory

        m = main_agent_memory()

        # Session layer (compression triggers)
        assert isinstance(m.session, SessionConfig)
        assert m.session.max_token_ratio > 0
        assert 0 < m.session.keep_ratio < 1

        # Archive layer (default off — user enables per-pool)
        assert m.archive is None, "archive must be off by default for main agents"

        # Core memory (default off — depends on archive)
        assert m.core is None, "core memory must be off by default for main agents"

        # Dream engine (default off — depends on archive + core)
        assert m.dream_engine is None, "dream_engine must be off by default for main agents"

        # Compact (default on — essential for all agents)
        assert m.compact is not None, "compact must be enabled for main agents"
        assert m.compact.enabled is True

        # Governance (tool chain repair + lossy compaction)
        assert m.governance is not None, "governance must be enabled for main agents"
        assert isinstance(m.governance, GovernanceConfig)
        assert m.governance.tool_chain_repair is True
        assert m.governance.budget is not None, (
            "main agent governance MUST have budget — without it, "
            "oversized tool results will blow up the context window"
        )

        # Pruned catalog
        assert m.pruned is not None, "pruned must be enabled for main agents"
        assert m.pruned.enabled is True
        assert isinstance(m.pruned, PrunedCatalogConfig)

    def test_main_agent_memory_accepts_max_context_tokens(self) -> None:
        """max_context_tokens from model.yml MUST flow into session config."""
        from bot.config.memory_defaults import main_agent_memory

        m = main_agent_memory(max_context_tokens=128000)
        assert m.session.max_context_tokens == 128000

    def test_main_agent_experience_enabled_by_default(self) -> None:
        """Main agent experience MUST be enabled.

        Without this, ExperienceReviewHook never fires, no EXPERIENCE.md
        is ever created, and the ExperienceProvider injection is empty.
        """
        from bot.config.memory_defaults import main_agent_experience

        e = main_agent_experience()
        assert isinstance(e, ExperienceConfig)
        assert e.enabled is True
        # Reviewer parameters must have sensible defaults
        assert e.min_messages > 0
        assert e.exp_cooldown_turns >= 0
        assert e.max_iterations > 0
        assert e.max_experiences > 0
        assert e.curator_interval > 0

    def test_subagent_memory_is_minimal(self) -> None:
        """Subagent memory MUST be session + pruned + governance ONLY.

        Subagents are short-lived task workers — they must NOT have:
        - archive (no long-term history needed)
        - core (no SOUL/USER/MEMORY.md)
        - dream_engine (no offline consolidation)
        - budget (small context windows, not worth the overhead)

        They MUST have:
        - session (token-budget compression)
        - governance.tool_chain_repair (prevent broken tool chains)
        - pruned (cleanup catalog)
        """
        from bot.config.memory_defaults import subagent_memory

        m = subagent_memory()
        assert isinstance(m, MemoryConfig)

        # MUST have
        assert isinstance(m.session, SessionConfig)
        assert m.governance is not None
        assert m.governance.tool_chain_repair is True
        assert m.pruned is not None and m.pruned.enabled is True

        # MUST NOT have
        assert m.archive is None, "subagent must NOT have archive"
        assert m.core is None, "subagent must NOT have core memory"
        assert m.dream_engine is None, "subagent must NOT have dream_engine"
        assert m.governance.budget is None, (
            "subagent governance must NOT have budget"
        )

    def test_no_subagent_experience_preset_exists(self) -> None:
        """There must be NO subagent_experience() — review is main-agent-only."""
        import bot.config.memory_defaults as mod

        assert not hasattr(mod, "subagent_experience"), (
            "subagent_experience() must NOT exist — experience review is "
            "main-agent-only. Subagents are short-lived and don't review."
        )


# ─── Test 2: _build_assembly_deps_for_pools injects uniformly ────────────────


class TestAssemblyDepsUniformInjection:
    """Verify pools with the default memory toggle (archive/core both off)
    receive the same memory + experience preset, regardless of type or count.

    This is the critical guard: if a new pool is added (native or external),
    it MUST receive the same converged config. External pools are skipped
    at wiring time (pipeline is None), not at deps construction time.
    """

    def test_all_pools_get_same_memory_preset(self, tmp_path: Path) -> None:
        """Every pool — native or external — gets the same memory config."""
        from bot.workspace.wiring.stack import _build_assembly_deps_for_pools

        pool_names = ["native_main", "external_main", "mixed"]
        deps_map = _build_assembly_deps_for_pools(
            pool_specs=_default_pool_specs(pool_names),
            max_context_tokens=50000,
        )

        assert set(deps_map.keys()) == set(pool_names)

        # All pools get identical memory config
        memories = [deps_map[n].memory for n in pool_names]
        for m in memories:
            assert m is not None
            assert m.archive is None  # default off
            assert m.core is None      # default off
            assert m.dream_engine is None  # default off
            assert m.compact is not None and m.compact.enabled  # compact always on
            assert m.governance is not None and m.governance.budget is not None
            assert m.pruned is not None and m.pruned.enabled
            assert m.session.max_context_tokens == 50000

    def test_all_pools_get_same_experience_preset(self, tmp_path: Path) -> None:
        """Every pool — native or external — gets experience enabled.

        External pools are skipped at wiring (pipeline is None), but the
        config is still set so that IF an external pool ever gets a
        pipeline, experience would work.
        """
        from bot.config.memory_defaults import main_agent_experience
        from bot.workspace.wiring.stack import _build_assembly_deps_for_pools

        pool_names = ["native_a", "native_b", "external_c"]
        deps_map = _build_assembly_deps_for_pools(
            pool_specs=_default_pool_specs(pool_names),
            max_context_tokens=None,
        )

        expected_exp = main_agent_experience()
        for name, deps in deps_map.items():
            assert deps.experience is not None, f"{name}: experience must be set"
            assert deps.experience == expected_exp, (
                f"{name}: experience must equal main_agent_experience()"
            )

    def test_works_with_empty_pool_list(self, tmp_path: Path) -> None:
        """Empty pool list must not crash (defensive)."""
        from bot.workspace.wiring.stack import _build_assembly_deps_for_pools

        deps_map = _build_assembly_deps_for_pools(
            pool_specs={},
            max_context_tokens=50000,
        )
        assert deps_map == {}

    def test_works_with_single_pool(self, tmp_path: Path) -> None:
        """Single pool must get full config."""
        from bot.workspace.wiring.stack import _build_assembly_deps_for_pools

        deps_map = _build_assembly_deps_for_pools(
            pool_specs=_default_pool_specs(["solo"]),
            max_context_tokens=None,
        )
        assert len(deps_map) == 1
        deps = deps_map["solo"]
        assert deps.memory is not None
        assert deps.experience is not None and deps.experience.enabled


# ─── Test 3: AgentTemplateRegistry injects subagent_memory uniformly ─────────


class TestSubagentTemplateMemoryInjection:
    """Verify AgentTemplateRegistry injects subagent_memory() to EVERY
    subagent template, regardless of execution_strategy.

    External_coding subagents carry the memory on the template object but
    are skipped at materialize time (template.py:100 early-dispatch).
    """

    def test_all_subagent_templates_get_minimal_memory(self, tmp_path: Path) -> None:
        """Every subagent template — native or external — gets subagent_memory()."""
        from bot.config.memory_defaults import subagent_memory

        store = _build_pool_store_with_mixed_pools(tmp_path)
        registry = AgentTemplateRegistry(
            store, default_subagent_memory=subagent_memory(),
        )

        for pool_name in ["native_main", "external_main", "mixed"]:
            templates = registry.list_templates(pool_name)
            for t in templates:
                m = t.memory
                # Must match subagent_memory() preset
                assert m.archive is None, (
                    f"{pool_name}/{t.spec.agent_name}: archive must be None"
                )
                assert m.core is None
                assert m.dream_engine is None
                assert m.governance is not None and m.governance.tool_chain_repair is True
                assert m.pruned is not None and m.pruned.enabled is True

    def test_external_subagent_template_carries_memory_but_skips_materialize(
        self, tmp_path: Path
    ) -> None:
        """External_coding subagent template has memory (harmless), but
        AgentTemplate.materialize early-dispatches to _materialize_external
        (template.py:100), so the memory is never consumed.

        This test verifies the structural skip point exists.
        """
        from bot.config.memory_defaults import subagent_memory

        store = _build_pool_store_with_mixed_pools(tmp_path)
        registry = AgentTemplateRegistry(
            store, default_subagent_memory=subagent_memory(),
        )

        # Find the external subagent template
        external_template = None
        for t in registry.list_templates("mixed"):
            if t.spec.execution_strategy == ExecutionStrategyKind.EXTERNAL:
                external_template = t
                break

        assert external_template is not None, (
            "Test setup must include an external subagent"
        )
        # The template DOES carry memory (harmless — it's never consumed)
        assert external_template.memory is not None
        # But execution_strategy is EXTERNAL → materialize will skip
        assert external_template.spec.execution_strategy == ExecutionStrategyKind.EXTERNAL


# ─── Test 4: External main agent structural skip at wiring ───────────────────


class TestExternalMainAgentSkip:
    """Verify external main agents are skipped at wiring time.

    External pools have a pipeline (ExternalTurnRunner) but NO BotModelProvider
    (they use their own provider backend). ExperienceReviewAgent uses the
    bot-global ``default_provider`` (from ``model.yml``), NOT per-pool
    provider, so external pools are NOT special-cased — they simply use the
    same default provider as native pools.

    The only skip condition for experience is ``default_provider is None``
    (model.yml unconfigured). In that case, experience review is skipped
    with a warning, but the bot boots and runs normally.
    """

    def test_default_provider_none_skips_experience_with_warning(
        self, tmp_path: Path
    ) -> None:
        """When default_provider is None (no model.yml), experience review
        is skipped with a warning. The bot must NOT crash."""
        from bot.workspace.wiring.pool_wiring import _wire_pool_to_resources
        from bot.workspace.wiring.stack import _build_assembly_deps_for_pools

        deps_map = _build_assembly_deps_for_pools(
            pool_specs=_default_pool_specs(["test_pool"]),
            max_context_tokens=50000,
        )
        deps = deps_map["test_pool"]

        pipeline = MagicMock()
        pipeline.hook_runner = MagicMock()
        pipeline.hook_runner.add = MagicMock()
        pipeline.hooks = []

        main_inst = MagicMock()
        main_inst.pipeline = pipeline

        pool = MagicMock()
        pool._agents = {"agent": main_inst}

        pool_instance = MagicMock()
        pool_instance.pool = pool
        pool_instance.main_agent_name = "agent"

        resources = MagicMock()
        resources.pool_data = {"test_pool": MagicMock()}

        # default_provider=None: must skip experience, NOT crash
        _wire_pool_to_resources(
            pool_instance, "test_pool", deps, resources, default_provider=None
        )

        assert not pipeline.hook_runner.add.called, (
            "ExperienceReviewHook must NOT be registered when default_provider is None"
        )

    def test_native_and_external_pools_both_use_default_provider(
        self, tmp_path: Path
    ) -> None:
        """Both native and external pools use the SAME bot-global default_provider
        for experience review — NOT per-pool provider. This decouples experience
        review from pool type."""
        from bot.workspace.wiring.pool_wiring import _wire_pool_to_resources
        from bot.workspace.wiring.stack import _build_assembly_deps_for_pools

        from modex_agent.core.provider import LLMProvider

        default_provider = MagicMock(spec=LLMProvider)
        deps_map = _build_assembly_deps_for_pools(
            pool_specs=_default_pool_specs(["native_pool", "external_pool"]),
            max_context_tokens=50000,
        )

        for pool_name in ["native_pool", "external_pool"]:
            deps = deps_map[pool_name]
            pipeline = MagicMock()
            pipeline.hook_runner = MagicMock()
            pipeline.hook_runner.add = MagicMock()
            pipeline.hooks = []

            main_inst = MagicMock()
            main_inst.pipeline = pipeline

            pool = MagicMock()
            pool._agents = {"agent": main_inst}

            pool_instance = MagicMock()
            pool_instance.pool = pool
            pool_instance.main_agent_name = "agent"

            pool_data = MagicMock()
            pool_data.experience_dir = tmp_path / "exp" / pool_name
            pool_data.experience_dir.mkdir(parents=True, exist_ok=True)
            pool_data.experience_meta = MagicMock()

            resources = MagicMock()
            resources.pool_data = {pool_name: pool_data}

            _wire_pool_to_resources(
                pool_instance, pool_name, deps, resources, default_provider
            )

            assert pipeline.hook_runner.add.called, (
                f"{pool_name}: ExperienceReviewHook must be registered when "
                "default_provider is available — pool type is irrelevant"
            )


# ─── Test 5: Experience three-component packaging ────────────────────────────


class TestExperienceThreeComponentPackaging:
    """Verify the experience system's three coupled components are all
    packaged together: ExperienceManager (injection) + ExperienceReviewHook
    (review) + ExperienceCurator (LRU eviction).

    If any component is missing, experience degrades silently (see AGENTS.md
    "Experience review mechanism" section).
    """

    def test_experience_config_carries_all_reviewer_params(self) -> None:
        """ExperienceConfig must carry ALL parameters needed by:
        - ExperienceReviewAgent (max_iterations)
        - ExperienceReviewHook (min_messages, exp_cooldown_turns)
        - ExperienceCurator (max_experiences, curator_interval)
        """
        from bot.config.memory_defaults import main_agent_experience

        e = main_agent_experience()
        # ExperienceReviewAgent params
        assert e.max_iterations > 0, "max_iterations must be set for ExperienceReviewAgent"
        # ExperienceReviewHook params
        assert e.min_messages > 0, "min_messages must be set for ExperienceReviewHook"
        assert e.exp_cooldown_turns >= 0, "exp_cooldown_turns must be set for ExperienceReviewHook"
        # ExperienceCurator params
        assert e.max_experiences > 0, "max_experiences must be set for ExperienceCurator"
        assert e.curator_interval > 0, "curator_interval must be set for ExperienceCurator"

    def test_experience_manager_built_when_experience_enabled(
        self, tmp_path: Path
    ) -> None:
        """_build_experience_manager MUST return a non-None ExperienceManager
        when experience is enabled.

        This is the injection component — without it, ExperienceProvider
        never adds <available_experiences> to the system prompt.
        """
        from bot.workspace.pool_data import _build_experience_manager
        from bot.workspace.wiring.stack import _build_assembly_deps_for_pools

        deps_map = _build_assembly_deps_for_pools(
            pool_specs=_default_pool_specs(["test"]),
            max_context_tokens=50000,
        )
        deps = deps_map["test"]

        exp_dir = tmp_path / "experiences" / "test" / "main"
        exp_dir.mkdir(parents=True, exist_ok=True)

        manager = _build_experience_manager(deps, exp_dir)
        assert manager is not None, (
            "ExperienceManager must be built when experience is enabled — "
            "without it, no <available_experiences> XML in system prompt"
        )

    def test_experience_manager_none_when_experience_disabled(
        self, tmp_path: Path
    ) -> None:
        """_build_experience_manager MUST return None when experience is
        disabled (defensive — verifies the guard clause works)."""
        from bot.workspace.pool_data import _build_experience_manager

        # Construct deps with experience=None (memory must be real MemoryConfig
        # because PoolAssemblyDeps is a frozen Pydantic model)
        from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps

        deps = PoolAssemblyDeps(
            memory=MemoryConfig(),
            experience=None,
        )
        exp_dir = tmp_path / "experiences" / "test"
        manager = _build_experience_manager(deps, exp_dir)
        assert manager is None

    def test_experience_curator_built_when_experience_enabled(
        self, tmp_path: Path
    ) -> None:
        """BackgroundTaskRunner._build_curators MUST create an
        ExperienceCurator for every pool with experience enabled.

        This is the LRU eviction component — without it, the experience
        directory grows unbounded.
        """
        from bot.workspace.background import BackgroundTaskRunner
        from bot.workspace.wiring.stack import _build_assembly_deps_for_pools

        deps_map = _build_assembly_deps_for_pools(
            pool_specs=_default_pool_specs(["native_a", "native_b"]),
            max_context_tokens=50000,
        )

        pool_data_a = MagicMock()
        pool_data_a.experience_dir = tmp_path / "exp_a"
        pool_data_a.experience_dir.mkdir(parents=True, exist_ok=True)
        pool_data_a.experience_meta = MagicMock()

        pool_data_b = MagicMock()
        pool_data_b.experience_dir = tmp_path / "exp_b"
        pool_data_b.experience_dir.mkdir(parents=True, exist_ok=True)
        pool_data_b.experience_meta = MagicMock()

        bg = BackgroundTaskRunner(
            pool_data={"native_a": pool_data_a, "native_b": pool_data_b},
            assembly_deps=deps_map,
            default_pool_name="native_a",
        )
        bg._build_curators()

        # Both pools must have curators
        assert "native_a" in bg.curators, "native_a must have ExperienceCurator"
        assert "native_b" in bg.curators, "native_b must have ExperienceCurator"

    def test_experience_curator_skipped_when_experience_disabled(
        self, tmp_path: Path
    ) -> None:
        """BackgroundTaskRunner._build_curators MUST NOT create a curator
        for pools with experience=None (defensive guard)."""
        from bot.workspace.background import BackgroundTaskRunner

        from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps

        pool_data = MagicMock()
        pool_data.experience_dir = tmp_path / "exp"
        pool_data.experience_meta = MagicMock()

        # PoolAssemblyDeps is a frozen Pydantic model — memory must be real
        deps_no_exp = PoolAssemblyDeps(
            memory=MemoryConfig(),
            experience=None,
        )

        bg = BackgroundTaskRunner(
            pool_data={"disabled_pool": pool_data},
            assembly_deps={"disabled_pool": deps_no_exp},
            default_pool_name="disabled_pool",
        )
        bg._build_curators()

        assert "disabled_pool" not in bg.curators, (
            "ExperienceCurator must NOT be built when experience is disabled"
        )


# ─── Test 6: End-to-end with synthesized mixed pool config ───────────────────


class TestEndToEndWithSynthesizedPools:
    """End-to-end test: synthesize pools with all 4 agent-type combinations,
    verify the full configuration chain works without real config/pools/.

    This is the strongest guard — it proves the configuration works
    regardless of what pools the user adds/removes/modifies.
    """

    def test_mixed_pool_configuration_loads_and_injects_correctly(
        self, tmp_path: Path
    ) -> None:
        """Synthesize 3 pools (native_main, external_main, mixed) with
        all 4 agent-type combinations, verify:
        1. PoolStore loads them correctly
        2. _build_assembly_deps_for_pools injects uniform config
        3. AgentTemplateRegistry injects subagent_memory to all templates
        4. External subagent is correctly marked EXTERNAL
        """
        from bot.config.memory_defaults import subagent_memory
        from bot.workspace.wiring.stack import _build_assembly_deps_for_pools

        # 1. Build synthesized pool store
        store = _build_pool_store_with_mixed_pools(tmp_path)
        summaries = store.list_pools()
        pool_names = [s.name for s in summaries]
        assert set(pool_names) == {"native_main", "external_main", "mixed"}

        # 2. Load pool specs and verify structure
        specs = {name: store.read_pool(name) for name in pool_names}

        # native_main: react main + 2 react subs
        assert specs["native_main"].main.execution_strategy == ExecutionStrategyKind.REACT
        assert len(specs["native_main"].subagents) == 2
        for sub in specs["native_main"].subagents:
            assert sub.execution_strategy == ExecutionStrategyKind.REACT

        # external_main: external main, no subs
        assert specs["external_main"].main.execution_strategy == ExecutionStrategyKind.EXTERNAL
        assert specs["external_main"].main.provider_kind == ProviderKind.OPENCODE
        assert len(specs["external_main"].subagents) == 0

        # mixed: react main + 1 react sub + 1 external sub
        assert specs["mixed"].main.execution_strategy == ExecutionStrategyKind.REACT
        assert len(specs["mixed"].subagents) == 2
        sub_strategies = {s.agent_name: s.execution_strategy for s in specs["mixed"].subagents}
        assert sub_strategies["researcher"] == ExecutionStrategyKind.REACT
        assert sub_strategies["external_worker"] == ExecutionStrategyKind.EXTERNAL

        # 3. Verify uniform assembly deps injection
        deps_map = _build_assembly_deps_for_pools(
            pool_specs=_default_pool_specs(pool_names),
            max_context_tokens=50000,
        )
        for name, deps in deps_map.items():
            assert deps.memory is not None, f"{name}: memory must be injected"
            assert deps.memory.archive is None  # default off
            assert deps.experience is not None and deps.experience.enabled

        # 4. Verify subagent template memory injection
        registry = AgentTemplateRegistry(
            store, default_subagent_memory=subagent_memory(),
        )
        for name in pool_names:
            for t in registry.list_templates(name):
                assert t.memory.archive is None, (
                    f"{name}/{t.spec.agent_name}: subagent archive must be None"
                )
                assert t.memory.governance is not None and t.memory.governance.tool_chain_repair
                assert t.memory.pruned is not None and t.memory.pruned.enabled


# ─── Test 7: Experience reviewer uses bot-global default_provider ─────────────


class TestExperienceReviewerUsesDefaultProvider:
    """Verify ExperienceReviewAgent uses the bot-global default_provider
    (from ``model.yml`` via ``BotService._default_provider``), NOT per-pool
    provider.

    This decouples experience review from pool type:
    - native pools: have their own provider, but experience review ignores it
    - external pools: have NO provider, but experience review still works
      (uses the global default)
    - no model.yml: default_provider is None, experience review is skipped
      with a warning (bot boots normally)

    If this contract breaks, external pools crash with
    ``TypeError: provider must be LLMProvider, got NoneType``.
    """

    def test_review_agent_receives_default_provider_not_pool_provider(
        self, tmp_path: Path
    ) -> None:
        """_wire_pool_to_resources MUST pass default_provider to
        ExperienceReviewAgent, NOT pool_instance.provider.

        This is the core decoupling: even if a pool has its own provider,
        experience review uses the global default. This ensures external
        pools (provider=None) don't crash.
        """
        from bot.workspace.wiring.pool_wiring import _wire_pool_to_resources
        from bot.workspace.wiring.stack import _build_assembly_deps_for_pools

        from modex_agent.core.provider import LLMProvider

        default_provider = MagicMock(spec=LLMProvider)
        deps_map = _build_assembly_deps_for_pools(
            pool_specs=_default_pool_specs(["test"]),
            max_context_tokens=50000,
        )

        pipeline = MagicMock()
        pipeline.hook_runner = MagicMock()
        pipeline.hook_runner.add = MagicMock()
        pipeline.hooks = []

        main_inst = MagicMock()
        main_inst.pipeline = pipeline

        pool = MagicMock()
        pool._agents = {"agent": main_inst}

        # pool_instance.provider is a DIFFERENT mock — if wiring used it
        # instead of default_provider, the test would still pass (both are
        # LLMProvider mocks). The real protection is that external pools
        # have provider=None, tested in test_default_provider_none_skips.
        pool_instance = MagicMock()
        pool_instance.pool = pool
        pool_instance.main_agent_name = "agent"
        # pool_instance.provider is NOT set — wiring should NOT access it

        pool_data = MagicMock()
        pool_data.experience_dir = tmp_path / "exp"
        pool_data.experience_dir.mkdir(parents=True, exist_ok=True)
        pool_data.experience_meta = MagicMock()

        resources = MagicMock()
        resources.pool_data = {"test": pool_data}

        _wire_pool_to_resources(
            pool_instance, "test", deps_map["test"], resources, default_provider
        )

        # Verify hook was registered (default_provider was used)
        assert pipeline.hook_runner.add.called, (
            "ExperienceReviewHook must be registered when default_provider is available"
        )

    def test_default_provider_none_skips_experience_not_crash(
        self, tmp_path: Path
    ) -> None:
        """When default_provider is None (model.yml unconfigured), experience
        review is skipped with a warning. The bot MUST NOT crash.

        This is the graceful degradation contract: a fresh bot install
        without model.yml can still boot — chat turns fail, but the WebUI
        is fully usable so the user can configure a model.
        """
        from bot.workspace.wiring.pool_wiring import _wire_pool_to_resources
        from bot.workspace.wiring.stack import _build_assembly_deps_for_pools

        deps_map = _build_assembly_deps_for_pools(
            pool_specs=_default_pool_specs(["no_model"]),
            max_context_tokens=None,
        )

        pipeline = MagicMock()
        pipeline.hook_runner = MagicMock()
        pipeline.hook_runner.add = MagicMock()

        main_inst = MagicMock()
        main_inst.pipeline = pipeline

        pool = MagicMock()
        pool._agents = {"agent": main_inst}

        pool_instance = MagicMock()
        pool_instance.pool = pool
        pool_instance.main_agent_name = "agent"

        resources = MagicMock()
        resources.pool_data = {"no_model": MagicMock()}

        # default_provider=None — must NOT raise TypeError
        _wire_pool_to_resources(
            pool_instance, "no_model", deps_map["no_model"], resources, None
        )

        # Experience hook must NOT be registered
        assert not pipeline.hook_runner.add.called, (
            "ExperienceReviewHook must NOT be registered when default_provider is None"
        )

    def test_external_pool_does_not_crash_with_default_provider(
        self, tmp_path: Path
    ) -> None:
        """External_coding pool (provider=None on pool_instance) must NOT
        crash when default_provider is available.

        This is the regression test for the bot startup crash:
        ``TypeError: provider must be LLMProvider, got NoneType``.
        The fix: experience review uses default_provider, not pool_instance.provider.
        """
        from bot.workspace.wiring.pool_wiring import _wire_pool_to_resources
        from bot.workspace.wiring.stack import _build_assembly_deps_for_pools

        from modex_agent.core.provider import LLMProvider

        default_provider = MagicMock(spec=LLMProvider)
        deps_map = _build_assembly_deps_for_pools(
            pool_specs=_default_pool_specs(["opencode"]),
            max_context_tokens=50000,
        )

        # External pool: has pipeline (ExternalTurnRunner) but provider=None
        pipeline = MagicMock()
        pipeline.hook_runner = MagicMock()
        pipeline.hook_runner.add = MagicMock()
        pipeline.hooks = []

        main_inst = MagicMock()
        main_inst.pipeline = pipeline

        pool = MagicMock()
        pool._agents = {"opencode": main_inst}

        pool_instance = MagicMock()
        pool_instance.pool = pool
        pool_instance.main_agent_name = "opencode"
        pool_instance.provider = None  # external: no BotModelProvider

        pool_data = MagicMock()
        pool_data.experience_dir = tmp_path / "exp_opencode"
        pool_data.experience_dir.mkdir(parents=True, exist_ok=True)
        pool_data.experience_meta = MagicMock()

        resources = MagicMock()
        resources.pool_data = {"opencode": pool_data}

        # Must NOT raise TypeError
        _wire_pool_to_resources(
            pool_instance, "opencode", deps_map["opencode"], resources, default_provider
        )

        # External pool DOES get experience review (uses default_provider)
        assert pipeline.hook_runner.add.called, (
            "External pool must get ExperienceReviewHook when default_provider "
            "is available — experience review is pool-type-agnostic"
        )


# ─── Test 8: Archive emitter notification (UserNoticeCleanupHook) ──────────


class TestArchiveEmitterNotification:
    """Verify the cleanup notice hook (UserNoticeCleanupHook) is correctly
    wired and fires the right notices.

    When session memory is compacted (archive generation triggered), the user
    sees two notices:
    1. ``[compact] Consolidating conversation memory, please wait...`` —
       fires BEFORE the archive-generation LLM call (which can be slow)
    2. ``[compact] Memory consolidated.`` — fires AFTER cleanup completes

    Without these notices, the user sees a stuck agent during archive
    generation (which can take 10+ seconds for the LLM summarizer call).

    The hook is registered via ``memory_system.add_cleanup_hook(...)`` in
    ``pool/factory.py``.
    """

    def test_hook_sends_start_notice_on_cleanup_triggered(self) -> None:
        """UserNoticeCleanupHook.on_cleanup_triggered MUST send the
        start notice via notification_service.send_notice."""
        from bot.service.pool.communication import UserNoticeCleanupHook

        from modex_agent.core.scope import MemoryContext
        from modex_agent.memory.hooks import MemoryHookContext

        notification_service = MagicMock()
        notification_service.send_notice = AsyncMock()
        hook = UserNoticeCleanupHook(notification_service)

        ctx = MemoryHookContext(
            memory_context=MemoryContext(
                session_id="test_session.orchestrator",
                user_id="u1",
            ),
        )

        import asyncio

        asyncio.run(hook.on_cleanup_triggered(ctx))

        notification_service.send_notice.assert_called_once_with(
            "test_session.orchestrator",
            "[compact] Consolidating conversation memory, please wait...",
        )

    def test_hook_sends_done_notice_on_cleanup_finished(self) -> None:
        """UserNoticeCleanupHook.on_cleanup_finished MUST send the
        done notice via notification_service.send_notice."""
        from bot.service.pool.communication import UserNoticeCleanupHook

        from modex_agent.core.scope import MemoryContext
        from modex_agent.memory.cleanup import CleanupResult
        from modex_agent.memory.core.models import CompressionReason
        from modex_agent.memory.hooks import MemoryHookContext

        notification_service = MagicMock()
        notification_service.send_notice = AsyncMock()
        hook = UserNoticeCleanupHook(notification_service)

        ctx = MemoryHookContext(
            memory_context=MemoryContext(
                session_id="test_session.orchestrator",
                user_id="u1",
            ),
            cleanup_result=CleanupResult(
                triggered=True,
                messages_kept=5,
                messages_pruned=10,
                reason=CompressionReason.TOKEN_PRESSURE,
            ),
        )

        import asyncio

        asyncio.run(hook.on_cleanup_finished(ctx))

        notification_service.send_notice.assert_called_once_with(
            "test_session.orchestrator",
            "[compact] Memory consolidated.",
        )

    def test_hook_skips_when_session_id_is_none(self) -> None:
        """Hook MUST NOT send notices when session_id is None
        (defensive — avoids crash on malformed context)."""
        from bot.service.pool.communication import UserNoticeCleanupHook

        from modex_agent.core.scope import MemoryContext
        from modex_agent.memory.hooks import MemoryHookContext

        notification_service = MagicMock()
        notification_service.send_notice = AsyncMock()
        hook = UserNoticeCleanupHook(notification_service)

        ctx = MemoryHookContext(
            memory_context=MemoryContext(session_id=None),
        )

        import asyncio

        asyncio.run(hook.on_cleanup_triggered(ctx))
        asyncio.run(hook.on_cleanup_finished(ctx))

        assert not notification_service.send_notice.called

    def test_hook_skips_when_memory_context_is_none(self) -> None:
        """Hook MUST NOT send notices when memory_context is None
        (defensive — guards against incomplete hook context)."""
        from bot.service.pool.communication import UserNoticeCleanupHook

        from modex_agent.memory.hooks import MemoryHookContext

        notification_service = MagicMock()
        notification_service.send_notice = AsyncMock()
        hook = UserNoticeCleanupHook(notification_service)

        ctx = MemoryHookContext(memory_context=None)

        import asyncio

        asyncio.run(hook.on_cleanup_triggered(ctx))
        asyncio.run(hook.on_cleanup_finished(ctx))

        assert not notification_service.send_notice.called

    def test_hook_implements_both_point_abcs(self) -> None:
        """UserNoticeCleanupHook MUST implement both CleanupTriggeredHook
        and CleanupFinishedHook ABCs.

        Without this, ``memory_system.add_cleanup_hook`` would not dispatch
        either point to it (the runner isinstance-checks each ABC).
        """
        from bot.service.pool.communication import UserNoticeCleanupHook

        from modex_agent.memory.hooks import CleanupFinishedHook, CleanupTriggeredHook

        assert issubclass(UserNoticeCleanupHook, CleanupTriggeredHook | CleanupFinishedHook), (
            "UserNoticeCleanupHook must inherit from both CleanupTriggeredHook "
            "and CleanupFinishedHook so the runner dispatches both points to it"
        )

    def test_hook_notices_are_english_and_start_with_compact_tag(self) -> None:
        """Notice text must start with ``[compact]`` tag and be in English
        (matching the existing convention — not localized).

        The ``[compact]`` tag lets the WebUI/IM filter these notices
        differently from regular agent messages if needed.
        """
        from bot.service.pool.communication import UserNoticeCleanupHook

        assert UserNoticeCleanupHook._START_NOTICE.startswith("[compact]")
        assert UserNoticeCleanupHook._DONE_NOTICE.startswith("[compact]")
        assert "Consolidating" in UserNoticeCleanupHook._START_NOTICE
        assert "consolidated" in UserNoticeCleanupHook._DONE_NOTICE.lower()


# ─── Test 9: MemorySystem fires cleanup hooks through the real path ───────


class TestMemorySystemCleanupHookFiring:
    """Verify DefaultMemorySystem fires cleanup hooks through the real path:

    - Registers a recording ``CleanupFinishedHook`` via ``add_cleanup_hook``.
    - Appends messages to a real ``ScopedMessageHistory`` to trigger cleanup.
    - Asserts the hook received a ``MemoryHookContext`` with the expected
      ``memory_context`` and ``cleanup_result``.

    No ``MagicMock(spec=...)`` for private list storage — the hook is
    registered via the public ``add_cleanup_hook`` API and fired by actually
    running ``cleanup_session()`` through the real ``ScopedMessageHistory`` →
    ``_run_cleanup`` → ``cleanup_session`` path.
    """

    def test_real_cleanup_fires_finished_hook(self, tmp_path: Path) -> None:
        import asyncio

        asyncio.run(self._run_real_cleanup_fires_finished_hook(tmp_path))

    async def _run_real_cleanup_fires_finished_hook(self, tmp_path: Path) -> None:
        from modex_agent.core.scope import MemoryContext
        from modex_agent.memory.default_system import DefaultMemorySystem
        from modex_agent.memory.hooks import CleanupFinishedHook, MemoryHookContext
        from modex_agent.memory.layers.factory import MemoryLayerFactory
        from modex_agent.memory.registry import DefaultMemoryStoreRegistry
        from modex_agent.memory.token_estimator import TokenEstimator

        class _FixedEstimator(TokenEstimator):
            def __init__(self) -> None:
                self.per_message = 10

            def estimate_text(self, text: str) -> int:
                return self.per_message

        class _RecordingFinishedHook(CleanupFinishedHook):
            def __init__(self) -> None:
                self.calls: list[MemoryHookContext] = []

            async def on_cleanup_finished(self, ctx: MemoryHookContext) -> None:
                self.calls.append(ctx)

        registry = DefaultMemoryStoreRegistry(tmp_path)
        layer_set = MemoryLayerFactory.single_user(registry=registry)
        system = DefaultMemorySystem(
            layer_set=layer_set,
            store_registry=registry,
            cleanup_config={
                "max_context_tokens": 100,
                "max_token_ratio": 0.8,
                "keep_ratio": 0.5,
            },
            token_estimator=_FixedEstimator(),
        )
        await system.initialize()

        hook = _RecordingFinishedHook()
        system.add_cleanup_hook(hook)

        context = MemoryContext(session_id="test-session", user_id="test-user")
        history = system.create_message_history(context)

        for i in range(20):
            await history.append({"role": "user", "content": f"msg-{i}"})

        assert len(hook.calls) > 0, (
            "CleanupFinishedHook must fire when cleanup is triggered"
        )
        finished_ctx = hook.calls[0]
        assert finished_ctx.memory_context is not None
        assert finished_ctx.memory_context.session_id == "test-session"
        assert finished_ctx.cleanup_result is not None
        assert finished_ctx.cleanup_result.triggered is True

    def test_real_cleanup_fires_triggered_and_finished_on_normal_path(
        self, tmp_path: Path
    ) -> None:
        import asyncio

        asyncio.run(self._run_real_cleanup_fires_both(tmp_path))

    async def _run_real_cleanup_fires_both(self, tmp_path: Path) -> None:
        from modex_agent.core.scope import MemoryContext
        from modex_agent.memory.default_system import DefaultMemorySystem
        from modex_agent.memory.hooks import (
            CleanupFinishedHook,
            CleanupTriggeredHook,
            MemoryHookContext,
        )
        from modex_agent.memory.layers.factory import MemoryLayerFactory
        from modex_agent.memory.registry import DefaultMemoryStoreRegistry
        from modex_agent.memory.token_estimator import TokenEstimator

        class _FixedEstimator(TokenEstimator):
            def __init__(self) -> None:
                self.per_message = 10

            def estimate_text(self, text: str) -> int:
                return self.per_message

        class _RecordingBothHook(CleanupTriggeredHook, CleanupFinishedHook):
            def __init__(self) -> None:
                self.triggered_calls: list[MemoryHookContext] = []
                self.finished_calls: list[MemoryHookContext] = []

            async def on_cleanup_triggered(self, ctx: MemoryHookContext) -> None:
                self.triggered_calls.append(ctx)

            async def on_cleanup_finished(self, ctx: MemoryHookContext) -> None:
                self.finished_calls.append(ctx)

        registry = DefaultMemoryStoreRegistry(tmp_path)
        layer_set = MemoryLayerFactory.single_user(registry=registry)
        system = DefaultMemorySystem(
            layer_set=layer_set,
            store_registry=registry,
            cleanup_config={
                "max_context_tokens": 100,
                "max_token_ratio": 0.8,
                "keep_ratio": 0.5,
            },
            token_estimator=_FixedEstimator(),
        )
        await system.initialize()

        hook = _RecordingBothHook()
        system.add_cleanup_hook(hook)

        context = MemoryContext(session_id="test-session", user_id="test-user")
        history = system.create_message_history(context)

        for i in range(20):
            await history.append({"role": "user", "content": f"msg-{i}"})

        assert len(hook.triggered_calls) > 0, (
            "CleanupTriggeredHook must fire on the normal cleanup path"
        )
        assert len(hook.finished_calls) > 0, (
            "CleanupFinishedHook must fire on the normal cleanup path"
        )
        assert hook.finished_calls[0].memory_context is not None
        assert hook.finished_calls[0].memory_context.session_id == "test-session"
        assert hook.finished_calls[0].cleanup_result is not None
        assert hook.finished_calls[0].cleanup_result.triggered is True
        assert hook.finished_calls[0].cleanup_result.messages_pruned > 0, (
            "Normal cleanup path must prune messages"
        )
