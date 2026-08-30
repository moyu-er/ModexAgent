"""The experience capability's supply + section face — the T14 pins (SPEC §8.3).

Covers:

- **Construction parity** — ``ExperienceCapability.supply()`` reproduces
  the retired BIZ constructions (``build_pool_data``'s experience layer +
  ``BackgroundTaskRunner._build_curators``): the
  ``<data>/experiences/<pool>/<root-agent>`` dir layout (the sanitized
  ``WorkspacePaths`` accessor), the scope-less manager source, the
  per-file meta store, and the curator knobs threaded from the FIRST
  entry's validated config (the retired single-config-per-pool
  semantics).
- **D4 curator lifecycle** — supply() constructs; pool assembly starts;
  pool teardown stops. The stage-run test pins all three: the supply's
  task exists after Stage 3, dies on the builder's cleanup-on-failure,
  and dies on ``AgentPool.shutdown_all`` (no orphaned runners).
- **Section byte parity** — the injection provider's content equals the
  machine-captured pre-migration golden
  (``tests/unit/memory/goldens/experience_section_pre_migration.txt``,
  captured on this wave's parent commit through the retired
  ``_experience_manager`` channel) with the fixture root normalized.
- **Dark-supply pins** — a pool with ZERO experience-capability agents
  builds NO experience supply (the retired always-built dir/manager
  died, SPEC P5); hand-referencing the experience tool or the review
  hook against such a pool raises loudly.
- **Loud supply reads** — ``require_experience_supply`` raises with the
  capability name + repair path on missing pool_runtime / missing key /
  wrong type.
- **Typed-field + BIZ-construction deaths** — grep-clean assertions for
  the retired carriers (``_experience_manager``, the two
  ``experience_review_provider`` typed fields, the BIZ
  manager/dir/curator builders, ``PoolAssemblyDeps.experience``).
- **The review provider rides the supply** — the deployment default LLM
  provider threads ``SupplyInfra.default_llm_provider`` → the supply
  view → ``ExperienceSupply.review_provider`` (the B8 ledger decision).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel, ConfigDict

from modex_agent.core.experience import (
    FileExperienceSource,
)
from modex_agent.multi_agent.execution_strategy import (
    ExecutionStrategy,
    PoolAssemblyContext,
    StrategyAssembly,
)
from modex_agent.multi_agent.pool import AgentPool
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.plugins.abc import ComponentSlot, SimpleFactory
from modex_agent.plugins.assembly.builder import AssemblyBuilder
from modex_agent.plugins.assembly.context import (
    AgentContext,
    AssemblyContext,
    PoolRuntimeDeps,
    SupplyInfra,
)
from modex_agent.plugins.assembly.spec import AssemblySpec, MemoryOverrides
from modex_agent.plugins.assembly.stages.pool_assemble import PoolAssembleStage
from modex_agent.plugins.capability import (
    CapabilityBinding,
    CapabilitySupply,
    CompiledCapability,
    PoolSupplyAgentEntry,
    PoolSupplyView,
    PromptSectionSpec,
)
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.defaults.capabilities.experience import (
    ExperienceCapability,
    ExperienceSupply,
    require_experience_supply,
)
from modex_agent.plugins.defaults.hooks import ExperienceReviewHookFactory
from modex_agent.plugins.defaults.tools import ExperienceToolFactory
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

_ROOT = Path(__file__).resolve().parents[3]
_BOT_PROJECT = _ROOT / "examples" / "bot_project"
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

_GOLDEN_FILE = (
    _ROOT / "tests" / "unit" / "memory" / "goldens" / "experience_section_pre_migration.txt"
)

_INJECTION_SECTION = PromptSectionSpec(section_id="experience.injection", order=50)


# ─── Harness (the todo supply suite's shapes) ────────────────────────────────


def _make_registry() -> ComponentRegistry:
    """DefaultPlugin (the production registration face — the experience
    capability lives there) plus a stub EXECUTION_STRATEGY named "stub"."""

    class _StubConfig(BaseModel):
        model_config = ConfigDict(frozen=True, extra="forbid")

    class _StubExecutionStrategy(ExecutionStrategy):
        @property
        def name(self) -> str:
            return "stub"

        async def assemble_main(self, ctx: PoolAssemblyContext) -> StrategyAssembly:
            return StrategyAssembly()  # pragma: no cover — the stage awaits the mock

        def validate_pool_spec(self, pool: Any) -> None:
            return None

    registry = ComponentRegistry()
    with PluginRegistrationContext(registry) as registration:
        DefaultPlugin().register(registration)
    registry.register(
        ComponentSlot.EXECUTION_STRATEGY,
        "stub",
        SimpleFactory(_StubExecutionStrategy(), _StubConfig),
    )
    return registry


def _make_spec(
    agent_name: str = "main",
    *,
    capabilities: tuple[CompiledCapability, ...] = (),
) -> AssemblySpec:
    from modex_agent.plugins.abc import AgentType

    workspace_root = Path(__file__).parent / "_ws_probe"
    return AssemblySpec(
        agent_type=AgentType.native_main,
        agent_name=agent_name,
        pool_name="test_pool",
        tools=[],
        hooks=[],
        llm_provider="default",
        system_prompt_provider="file_prompt",
        system_prompt_config={},
        memory_overrides=MemoryOverrides(),
        execution_strategy="stub",
        capabilities=capabilities,
        workspace_ctx=WorkspaceContext(
            target=workspace_root,
            paths=WorkspacePaths(root=workspace_root),
            is_home=False,
        ),
    )


def _make_pool_assembly_ctx(data_dir: Path) -> PoolAssemblyContext:
    return PoolAssemblyContext(
        pool_name="test_pool",
        pool_spec=MagicMock(),
        project_dir=data_dir,
        data_dir=data_dir,
        broker=MagicMock(),
        inbox_server=MagicMock(),
        agent_bus=MagicMock(),
        output_adapter=MagicMock(),
        safety=MagicMock(),
        retention=MagicMock(),
        registry=MagicMock(),
    )


async def _run_stage(
    pool_assembly_ctx: PoolAssemblyContext,
    specs: tuple[AssemblySpec, ...],
    registry: ComponentRegistry,
    *,
    pool: AgentPool | None = None,
    builder: AssemblyBuilder | None = None,
) -> tuple[PoolRuntimeDeps, AssemblyBuilder]:
    builder = builder if builder is not None else AssemblyBuilder()
    if pool is None:
        pool = MagicMock(spec=AgentPool)
    builder.infra = SupplyInfra(
        pool_assembly_ctx=pool_assembly_ctx,
        pool=pool,  # type: ignore[arg-type]
        pool_specs=specs,
    )
    ctx = AssemblyContext(
        registry=registry,
        workspace_ctx=MagicMock(),
        infra=builder.infra,
    )
    await PoolAssembleStage().process(specs[0], builder, ctx)
    propagated = builder.propagated_context
    assert propagated is not None and propagated.pool_runtime is not None
    return propagated.pool_runtime, builder


def _experience_compiled(config: dict[str, Any] | None = None) -> CompiledCapability:
    return CompiledCapability(
        name="experience",
        config=config or {},
        binding=CapabilityBinding(active_sections=(_INJECTION_SECTION,)),
    )


def _view(data_dir: Path, **kwargs: Any) -> PoolSupplyView:
    return PoolSupplyView(
        pool_name="test_pool",
        entries=(PoolSupplyAgentEntry(agent_name="main", config={}),),
        root_agent_name="main",
        data_dir=data_dir,
        **kwargs,
    )


# ─── Construction parity with the retired BIZ builders ───────────────────────


class TestConstructionParity:
    def test_dir_layout_is_sanitized_workspace_paths_shape(self, tmp_path: Path) -> None:
        supply = ExperienceCapability().supply(_view(tmp_path))

        assert isinstance(supply, ExperienceSupply)
        assert supply.experience_dir == WorkspacePaths(root=tmp_path).experience_dir(
            "test_pool", "main"
        )
        assert supply.experience_dir.exists()

    def test_manager_source_is_the_retired_scope_less_shape(self, tmp_path: Path) -> None:
        supply = ExperienceCapability().supply(_view(tmp_path))

        source = supply.manager._source  # noqa: SLF001
        assert isinstance(source, FileExperienceSource)
        assert source.directories == [supply.experience_dir]
        assert source.scope is None

    def test_curator_knobs_thread_from_first_entry_config(self, tmp_path: Path) -> None:
        capability = ExperienceCapability()
        view = PoolSupplyView(
            pool_name="test_pool",
            entries=(
                PoolSupplyAgentEntry(
                    agent_name="main",
                    config={
                        "max_experiences": 5,
                        "curator_interval": 7,
                    },
                ),
            ),
            root_agent_name="main",
            data_dir=tmp_path,
        )

        supply = capability.supply(view)

        assert supply.curator._max_experiences == 5  # noqa: SLF001
        assert supply.curator_interval == 7

    def test_first_entry_wins_on_diverging_configs(self, tmp_path: Path) -> None:
        """OQ1 arbitration: root-first order — the retired pool had ONE
        config (the root's); diverging per-agent configs resolve to the
        first entry's."""
        capability = ExperienceCapability()
        view = PoolSupplyView(
            pool_name="test_pool",
            entries=(
                PoolSupplyAgentEntry(agent_name="main", config={"max_experiences": 3}),
                PoolSupplyAgentEntry(agent_name="sub", config={"max_experiences": 9}),
            ),
            root_agent_name="main",
            data_dir=tmp_path,
        )

        supply = capability.supply(view)

        assert supply.curator._max_experiences == 3  # noqa: SLF001

    def test_dir_keyed_by_root_agent_not_entry_agent(self, tmp_path: Path) -> None:
        """A subagent-only declaration keys the dir by the pool's ROOT
        agent — the retired construction keyed the main agent
        unconditionally."""
        view = PoolSupplyView(
            pool_name="test_pool",
            entries=(PoolSupplyAgentEntry(agent_name="sub", config={}),),
            root_agent_name="main",
            data_dir=tmp_path,
        )

        supply = ExperienceCapability().supply(view)

        assert supply.experience_dir.name == "main"

    def test_review_provider_rides_the_supply(self, tmp_path: Path) -> None:
        provider = MagicMock(name="default_llm")

        supply = ExperienceCapability().supply(_view(tmp_path, default_llm_provider=provider))

        assert supply.review_provider is provider

    def test_no_data_dir_raises_loudly(self) -> None:
        with pytest.raises(ValueError, match="'experience'"):
            ExperienceCapability().supply(
                PoolSupplyView(
                    pool_name="p",
                    entries=(PoolSupplyAgentEntry(agent_name="main", config={}),),
                    root_agent_name="main",
                )
            )

    def test_no_root_agent_name_raises_loudly(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="root agent"):
            ExperienceCapability().supply(
                PoolSupplyView(
                    pool_name="p",
                    entries=(PoolSupplyAgentEntry(agent_name="main", config={}),),
                    data_dir=tmp_path,
                )
            )


# ─── D4 lifecycle: supply() constructs; pool assembly starts; teardown stops ─


class TestCuratorLifecycle:
    async def test_stage_starts_the_curator_loop(self, tmp_path: Path) -> None:
        registry = _make_registry()
        ctx = _make_pool_assembly_ctx(tmp_path)
        spec = _make_spec(capabilities=(_experience_compiled(),))

        pool_runtime, _ = await _run_stage(ctx, (spec,), registry)

        supply = pool_runtime.capability_supply.get("experience")
        assert isinstance(supply, ExperienceSupply)
        assert supply.task is not None and not supply.task.done()
        await supply.stop()

    async def test_builder_cleanup_on_failure_stops_the_loop(self, tmp_path: Path) -> None:
        """A later-stage failure runs the builder's registered cleanups —
        the supply stop is among them (cleanup-on-failure road)."""
        registry = _make_registry()
        ctx = _make_pool_assembly_ctx(tmp_path)
        spec = _make_spec(capabilities=(_experience_compiled(),))
        builder = AssemblyBuilder()

        pool_runtime, _ = await _run_stage(ctx, (spec,), registry, builder=builder)
        supply = pool_runtime.capability_supply["experience"]
        assert isinstance(supply, ExperienceSupply)
        assert supply.task is not None

        await builder.cleanup()

        assert supply.task is None

    async def test_pool_shutdown_all_stops_the_loop(self, tmp_path: Path) -> None:
        """The pool's teardown machinery (``AgentPool.shutdown_all``) stops
        the attached supply workers — the shutdown road. Tears the pool
        down and asserts the runner stopped: no orphaned background tasks."""
        registry = _make_registry()
        ctx = _make_pool_assembly_ctx(tmp_path)
        spec = _make_spec(capabilities=(_experience_compiled(),))
        pool = AgentPool(broker=MagicMock(), agent_factory=MagicMock())

        pool_runtime, _ = await _run_stage(ctx, (spec,), registry, pool=pool)
        supply = pool_runtime.capability_supply["experience"]
        assert isinstance(supply, ExperienceSupply)
        assert supply.task is not None

        await pool.shutdown_all()

        assert supply.task is None

    async def test_curator_loop_cycles_and_evicts(self, tmp_path: Path) -> None:
        """The retired background runner's regression: the loop still
        CYCLES — with interval 0 the curator runs immediately and LRU
        evicts the least-recently-used excess experience (meta records
        seed the count, exactly like the review hook's post-write
        bookkeeping does)."""
        from modex_agent.core.experience.meta import ExperienceMetaRecord

        capability = ExperienceCapability()
        view = PoolSupplyView(
            pool_name="test_pool",
            entries=(
                PoolSupplyAgentEntry(
                    agent_name="main",
                    config={
                        "max_experiences": 2,
                        "curator_interval": 0,
                    },
                ),
            ),
            root_agent_name="main",
            data_dir=tmp_path,
        )
        supply = capability.supply(view)
        exp_dir = supply.experience_dir
        for name in ("exp-a", "exp-b", "exp-c"):
            entry = exp_dir / name
            entry.mkdir(parents=True)
            (entry / "EXPERIENCE.md").write_text(
                f"---\nname: {name}\ndescription: d\nscenario: s\n---\n# {name}\n",
                encoding="utf-8",
            )
        # exp-a never used (no last_used_at) → LRU evicts it first.
        supply.meta_store.set("exp-a", ExperienceMetaRecord())
        supply.meta_store.set("exp-b", ExperienceMetaRecord(use_count=5))
        supply.meta_store.set("exp-c", ExperienceMetaRecord(use_count=5))

        await supply.start()
        assert supply.task is not None
        # Interval 0 → the first cycle runs within a scheduler turn.
        for _ in range(50):
            await asyncio.sleep(0.01)
            if not (exp_dir / "exp-a").exists():
                break
        await supply.stop()

        remaining = sorted(p.name for p in exp_dir.iterdir() if p.is_dir())
        assert "exp-a" not in remaining, f"LRU eviction did not run: {remaining}"

    async def test_start_and_stop_are_idempotent(self, tmp_path: Path) -> None:
        supply = ExperienceCapability().supply(_view(tmp_path))
        await supply.start()
        task_first = supply.task
        await supply.start()
        assert supply.task is task_first  # no double start
        await supply.stop()
        await supply.stop()
        assert supply.task is None


# ─── Stage aggregation end-to-end (the S phase) ─────────────────────────────


class TestStageAggregation:
    async def test_view_carries_root_agent_and_default_provider(self, tmp_path: Path) -> None:
        captured: list[PoolSupplyView] = []
        capability = ExperienceCapability()
        original = capability.supply

        def capturing_supply(view: PoolSupplyView) -> ExperienceSupply:
            captured.append(view)
            return original(view)

        capability.supply = capturing_supply  # type: ignore[method-assign]
        registry = _make_registry()
        registry._factories[ComponentSlot.CAPABILITY]["experience"] = capability  # noqa: SLF001
        ctx = _make_pool_assembly_ctx(tmp_path)
        spec = _make_spec(capabilities=(_experience_compiled(),))

        pool_runtime, _ = await _run_stage(ctx, (spec,), registry)
        supply = pool_runtime.capability_supply.get("experience")
        assert isinstance(supply, ExperienceSupply)
        await supply.stop()

        (view,) = captured
        assert view.pool_name == "test_pool"
        assert view.root_agent_name == "main"
        assert view.data_dir == tmp_path
        assert view.default_llm_provider is None  # no provider on this infra


# ─── Dark-supply pin (SPEC P5 — the always-built dir/manager died) ───────────


class TestDarkSupplyPin:
    async def test_pool_without_experience_agents_has_no_experience_key(
        self, tmp_path: Path
    ) -> None:
        registry = _make_registry()
        ctx = _make_pool_assembly_ctx(tmp_path)
        spec = _make_spec()  # no capabilities

        pool_runtime, _ = await _run_stage(ctx, (spec,), registry)

        assert "experience" not in pool_runtime.capability_supply

    async def test_hand_referenced_experience_tool_raises_loudly(self, tmp_path: Path) -> None:
        """A bare roster reference of ``experience`` on a pool without the
        capability loud-fails at the factory — the dark-supply death (the
        bare-tool degraded mode, SPEC §5.3)."""
        registry = _make_registry()
        ctx = _make_pool_assembly_ctx(tmp_path)
        spec = _make_spec()

        pool_runtime, _ = await _run_stage(ctx, (spec,), registry)

        chain = AgentContext(
            registry=registry,
            workspace_ctx=MagicMock(),
            pool_runtime=pool_runtime,
            agent_name="probe",
        )
        with pytest.raises(ValueError, match="experience") as excinfo:
            await ExperienceToolFactory().create(ExperienceToolFactory.config_model(), chain)
        assert "capabilities: {experience: {}}" in str(excinfo.value)

    async def test_hand_referenced_review_hook_raises_loudly(self, tmp_path: Path) -> None:
        registry = _make_registry()
        ctx = _make_pool_assembly_ctx(tmp_path)
        spec = _make_spec()

        pool_runtime, _ = await _run_stage(ctx, (spec,), registry)

        chain = AgentContext(
            registry=registry,
            workspace_ctx=MagicMock(),
            pool_runtime=pool_runtime,
            agent_name="probe",
        )
        with pytest.raises(ValueError, match="experience"):
            await ExperienceReviewHookFactory().create(MagicMock(), chain)


# ─── Loud supply reads ────────────────────────────────────────────────────────


class _WrongSupply(CapabilitySupply):
    pass


class TestLoudSupplyReads:
    def test_missing_pool_runtime_raises(self) -> None:
        with pytest.raises(ValueError, match="experience"):
            require_experience_supply(None)

    def test_missing_key_names_repair_path(self) -> None:
        with pytest.raises(ValueError, match=r"capabilities: \{experience: \{\}\}"):
            require_experience_supply(PoolRuntimeDeps())

    def test_wrong_type_names_expected_and_actual(self) -> None:
        pool_runtime = PoolRuntimeDeps(capability_supply={"experience": _WrongSupply()})

        with pytest.raises(
            ValueError, match=r"ExperienceSupply.*_WrongSupply|_WrongSupply.*ExperienceSupply"
        ):
            require_experience_supply(pool_runtime)

    def test_concrete_supply_round_trips(self, tmp_path: Path) -> None:
        supply = ExperienceCapability().supply(_view(tmp_path))
        pool_runtime = PoolRuntimeDeps(capability_supply={"experience": supply})

        assert require_experience_supply(pool_runtime) is supply


# ─── Section byte parity (the pre-migration golden) ──────────────────────────


class TestSectionByteParity:
    async def test_channel_section_bytes_match_pre_migration_golden(self, tmp_path: Path) -> None:
        """The acceptance bar: the capability channel's section content is
        byte-equal to the retired experience special case's output
        (captured on this wave's parent commit; the fixture root is
        normalized — the rendered ``directory=""`` attributes embed
        absolute paths). The view's pool/root names match the capture
        fixture's layout (``experiences/pool/main``)."""
        from tests.unit.memory.goldens.capture_experience_injection import (
            _write_fixtures,
        )

        _write_fixtures(tmp_path)
        capability = ExperienceCapability()
        supply = capability.supply(
            PoolSupplyView(
                pool_name="pool",
                entries=(PoolSupplyAgentEntry(agent_name="main", config={}),),
                root_agent_name="main",
                data_dir=tmp_path,
            )
        )
        wiring = await capability.assemble(
            CapabilityBinding(active_sections=(_INJECTION_SECTION,)),
            AgentContext(
                registry=MagicMock(),
                workspace_ctx=MagicMock(),
                pool_runtime=PoolRuntimeDeps(capability_supply={"experience": supply}),
                agent_name="main",
            ),
        )
        assert len(wiring.prompt_providers) == 1

        content = await wiring.prompt_providers[0].get_or_refresh()
        normalized = content.replace(str(tmp_path.resolve()), "<ROOT>")
        # The golden was captured on a Windows host (backslash separators);
        # Path rendering follows the running OS, so compare on the
        # platform-neutral separator.
        normalized = normalized.replace("\\", "/")
        golden = _GOLDEN_FILE.read_text(encoding="utf-8").replace("\\", "/")

        assert normalized == golden

    async def test_assemble_wiring_shape(self, tmp_path: Path) -> None:
        capability = ExperienceCapability()
        supply = capability.supply(_view(tmp_path))
        ctx = AgentContext(
            registry=MagicMock(),
            workspace_ctx=MagicMock(),
            pool_runtime=PoolRuntimeDeps(capability_supply={"experience": supply}),
            agent_name="main",
        )

        active = await capability.assemble(
            CapabilityBinding(active_sections=(_INJECTION_SECTION,)), ctx
        )
        inactive = await capability.assemble(CapabilityBinding(active_sections=()), ctx)

        assert len(active.prompt_providers) == 1
        assert inactive.prompt_providers == ()
        assert active.artifacts == {}

    async def test_active_section_without_supply_raises_loudly(self) -> None:
        """A binding with the active section but no pool supply is a broken
        invariant (effectiveness implies the pool-level supply) — loud."""
        capability = ExperienceCapability()
        ctx = AgentContext(
            registry=MagicMock(),
            workspace_ctx=MagicMock(),
            pool_runtime=PoolRuntimeDeps(),
            agent_name="main",
        )

        with pytest.raises(ValueError, match="experience"):
            await capability.assemble(CapabilityBinding(active_sections=(_INJECTION_SECTION,)), ctx)

    async def test_version_is_content_hash_and_refreshes_on_change(self, tmp_path: Path) -> None:
        """Manager-driven section contract (SPEC §7.3 / E10): version is
        the content hash — stable while the experiences are unchanged,
        changed (exactly one refresh) when they change."""
        exp_dir = tmp_path / "experiences" / "test_pool" / "main"
        exp_dir.mkdir(parents=True)
        (exp_dir / "exp-one").mkdir()
        (exp_dir / "exp-one" / "EXPERIENCE.md").write_text(
            "---\nname: exp-one\ndescription: one\nscenario: s\n---\n# one\n",
            encoding="utf-8",
        )
        supply = ExperienceCapability().supply(_view(tmp_path))
        wiring = await ExperienceCapability().assemble(
            CapabilityBinding(active_sections=(_INJECTION_SECTION,)),
            AgentContext(
                registry=MagicMock(),
                workspace_ctx=MagicMock(),
                pool_runtime=PoolRuntimeDeps(capability_supply={"experience": supply}),
                agent_name="main",
            ),
        )
        provider = wiring.prompt_providers[0]

        first = await provider.get_or_refresh()
        version_first = provider.last_version
        unchanged = await provider.get_or_refresh()
        assert unchanged == first
        assert provider.last_version == version_first

        (exp_dir / "exp-two").mkdir()
        (exp_dir / "exp-two" / "EXPERIENCE.md").write_text(
            "---\nname: exp-two\ndescription: two\nscenario: s\n---\n# two\n",
            encoding="utf-8",
        )
        changed = await provider.get_or_refresh()
        assert changed != first
        assert provider.last_version != version_first
        assert "exp-two" in changed


# ─── Anchor geometry — the documented position delta ─────────────────────────


class TestAnchorPosition:
    async def test_experience_section_renders_at_capability_anchor(self, tmp_path: Path) -> None:
        """The retired channel rendered the section at load() position 8
        (after provider blocks/prefetch); the channel renders it at the
        capability anchor (fork → capability block → core memory / AgentComm)
        — the DESIGNED position delta (SPEC §7.3 N4; content byte-equal is
        the acceptance bar, pinned above)."""
        from modex_agent.memory.hooks import MemoryHookRunner
        from modex_agent.memory.system import MemorySystemContextManager

        mock_system = MagicMock()
        mock_system.ensure_within_budget = AsyncMock()
        mock_system.retrieve_core_memory = AsyncMock(
            return_value=MagicMock(soul="", user="", memory="")
        )
        mock_system.get_core_memory_directory = AsyncMock(return_value=None)
        mock_system.get_storage_path = AsyncMock(return_value=None)
        mock_system.get_providers = MagicMock(return_value=[])
        mock_system.prefetch_memories = AsyncMock(return_value=None)
        mock_system.get_history = AsyncMock(return_value=[])
        mock_system.create_message_history = MagicMock(return_value=MagicMock())
        mock_system.hook_runner = MemoryHookRunner()
        mock_system.pruned_manager = None

        exp_dir = tmp_path / "experiences" / "test_pool" / "main"
        exp_dir.mkdir(parents=True)
        (exp_dir / "exp-anchor").mkdir()
        (exp_dir / "exp-anchor" / "EXPERIENCE.md").write_text(
            "---\nname: exp-anchor\ndescription: anchor\nscenario: s\n---\n# anchor\n",
            encoding="utf-8",
        )
        supply = ExperienceCapability().supply(_view(tmp_path))
        wiring = await ExperienceCapability().assemble(
            CapabilityBinding(active_sections=(_INJECTION_SECTION,)),
            AgentContext(
                registry=MagicMock(),
                workspace_ctx=MagicMock(),
                pool_runtime=PoolRuntimeDeps(capability_supply={"experience": supply}),
                agent_name="main",
            ),
        )
        mgr = MemorySystemContextManager(
            memory_system=mock_system,
            base_system_prompt="BASE-MARKER",
        )
        mgr.set_capability_sections(wiring.prompt_providers)

        state = await mgr.load("s1", tool_manager=MagicMock())
        assert state.system_prompt_pipeline is not None
        prompt = await state.system_prompt_pipeline.get_or_refresh()

        assert "## Experiences" in prompt
        assert prompt.index("BASE-MARKER") < prompt.index("## Experiences")


# ─── Typed-field + BIZ-construction deaths (grep-clean assertions) ───────────

# Runtime/generated state (never source): the bot's `.modex` SQLite state,
# runtime-populated `experiences`/`subworkspace`, logs, caches, and any
# virtualenv (a stale editable install under `examples/bot_project/.venv`
# shadows the live tree and would fake a violation).
_SKIPPED_DIRS = {
    "__pycache__",
    "node_modules",
    "dist",
    "target",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "site-packages",
    ".modex",
    "experiences",
    "subworkspace",
    "logs",
}


def _iter_py_files(*bases: Path) -> Any:
    for base in bases:
        for path in base.rglob("*.py"):
            if not path.is_file():
                continue
            if any(part in _SKIPPED_DIRS for part in path.parts):
                continue
            yield path


def _py_tree_contains(needle: str, *bases: Path) -> list[str]:
    marker = needle.encode("utf-8")
    # Sorted: rglob iteration order is filesystem-dependent (macOS and Linux
    # disagree), and the death-fact assertions compare against fixed lists.
    return sorted(str(path) for path in _iter_py_files(*bases) if marker in path.read_bytes())


#: The two legitimate ``ExperienceManager`` mentions: the class's own
#: docstring usage example and the capability supply (the one constructor).
_CORE_EXPERIENCE_MANAGER = _ROOT / "src" / "modex_agent" / "core" / "experience" / "manager.py"
_CAPABILITY_EXPERIENCE = (
    _ROOT / "src" / "modex_agent" / "plugins" / "defaults" / "capabilities" / "experience.py"
)


class TestDeathFacts:
    def test_experience_manager_special_case_gone_from_system(self) -> None:
        source = (_ROOT / "src" / "modex_agent" / "memory" / "system.py").read_text(
            encoding="utf-8"
        )
        assert "_experience_manager" not in source
        assert "experience_manager" not in source

    def test_experience_manager_construction_sites_gone(self) -> None:
        """The plan's (f) sweep: no production site constructs an
        ``ExperienceManager`` outside the capability supply (the
        ``core/experience`` package's own docstring examples and tests
        are not wiring)."""
        assert _py_tree_contains(
            "ExperienceManager(",
            _ROOT / "src" / "modex_agent",
            _BOT_PROJECT / "bot",
        ) == sorted([str(_CORE_EXPERIENCE_MANAGER), str(_CAPABILITY_EXPERIENCE)])

    def test_curator_construction_site_gone_from_biz(self) -> None:
        assert _py_tree_contains(
            "ExperienceCurator(",
            _ROOT / "src" / "modex_agent",
            _BOT_PROJECT / "bot",
        ) == [str(_CAPABILITY_EXPERIENCE)]

    def test_biz_experience_builders_gone(self) -> None:
        assert (
            _py_tree_contains("_build_experience_manager", _ROOT / "src", _ROOT / "examples") == []
        )
        assert _py_tree_contains("_build_curators", _ROOT / "src", _ROOT / "examples") == []

    def test_experience_meta_carrier_gone_from_pool_data(self) -> None:
        source = (_BOT_PROJECT / "bot" / "workspace" / "pool_data.py").read_text(encoding="utf-8")
        assert "experience_meta" not in source
        assert "experience_dir" not in source

    def test_both_review_provider_typed_fields_gone(self) -> None:
        source = (_ROOT / "src" / "modex_agent" / "plugins" / "assembly" / "context.py").read_text(
            encoding="utf-8"
        )
        # The only remaining mention documents the retirement in the
        # replacement field's docstring.
        assert source.count("experience_review_provider") == 1

    def test_pool_assembly_deps_experience_field_gone(self) -> None:
        assert "experience" not in PoolAssemblyDeps.model_fields

    def test_stack_derivation_gone(self) -> None:
        source = (_BOT_PROJECT / "bot" / "workspace" / "wiring" / "stack.py").read_text(
            encoding="utf-8"
        )
        assert "ExperienceConfig" not in source

    def test_context_manager_param_gone_from_source_tree(self) -> None:
        assert _py_tree_contains("experience_manager=", _ROOT / "src", _ROOT / "examples") == []
