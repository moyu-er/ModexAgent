"""The experience capability's supply + lifecycle face (plan §10.5, §18.7).

Covers:

- **Construction parity** — the ``<data>/experiences/<pool>/<root-agent>``
  dir layout (the sanitized ``WorkspacePaths`` accessor), the scope-less
  catalog source, the per-file meta store, curator knobs from the
  (conflict-checked) pool config.
- **Config altitude (§10.5.1)** — conflicting pool-level declarations
  fail supply construction with ``ExperienceConfigError`` while
  per-agent review configs stay distinct.
- **Lifecycle** — supply() constructs; pool assembly starts; teardown
  stops; start/stop idempotent; pool shutdown leaves no task.
- **Review-task ownership (§10.5)** — submissions accepted while
  running, rejected during stop, awaited+cancelled on stop.
- **Dark-supply pins** — a pool with ZERO experience-capability agents
  builds NO experience supply; hand-referencing the tool/hook raises.
- **Loud supply reads** — ``require_experience_supply`` raises with the
  repair path on missing pool_runtime / missing key / wrong type.
- **Section parity** — the injection provider renders the pre-migration
  golden bytes (root-normalized) and refreshes on change.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel, ConfigDict

from modex_agent.multi_agent.execution_strategy import (
    ExecutionStrategy,
    PoolAssemblyContext,
    StrategyAssembly,
)
from modex_agent.multi_agent.pool import AgentPool
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
from modex_agent.plugins.defaults.capabilities.experience.config import (
    ExperienceConfigError,
)
from modex_agent.plugins.defaults.capabilities.experience.hook_factory import (
    ExperienceReviewHookFactory,
)
from modex_agent.plugins.defaults.capabilities.experience.tool_factory import (
    ExperienceToolFactory,
)
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


def _make_registry() -> ComponentRegistry:
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


# ─── Construction parity ─────────────────────────────────────────────────────


class TestConstructionParity:
    def test_dir_layout_is_sanitized_workspace_paths_shape(self, tmp_path: Path) -> None:
        supply = ExperienceCapability().supply(_view(tmp_path))

        assert isinstance(supply, ExperienceSupply)
        assert supply.experience_dir == WorkspacePaths(root=tmp_path).experience_dir(
            "test_pool", "main"
        )
        assert supply.experience_dir.exists()

    def test_catalog_source_is_scope_less_over_the_dir(self, tmp_path: Path) -> None:
        supply = ExperienceCapability().supply(_view(tmp_path))

        assert supply.catalog.source.directories == [supply.experience_dir]
        assert supply.catalog.source.scope is None

    def test_curator_knobs_thread_from_pool_config(self, tmp_path: Path) -> None:
        capability = ExperienceCapability()
        view = PoolSupplyView(
            pool_name="test_pool",
            entries=(
                PoolSupplyAgentEntry(
                    agent_name="main",
                    config={"max_experiences": 5, "curator_interval": 7},
                ),
            ),
            root_agent_name="main",
            data_dir=tmp_path,
        )

        supply = capability.supply(view)

        assert supply.catalog.curator.max_experiences == 5
        assert supply.pool_config.curator_interval == 7

    def test_dir_keyed_by_root_agent_not_entry_agent(self, tmp_path: Path) -> None:
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


# ─── Config altitude (§10.5.1) ───────────────────────────────────────────────


class TestConfigAltitude:
    def test_conflicting_pool_config_fails_boot(self, tmp_path: Path) -> None:
        """§5.3 correction: diverging pool-level values are a typed boot
        failure — never a silent first-pick."""
        view = PoolSupplyView(
            pool_name="test_pool",
            entries=(
                PoolSupplyAgentEntry(agent_name="main", config={"max_experiences": 3}),
                PoolSupplyAgentEntry(agent_name="sub", config={"max_experiences": 9}),
            ),
            root_agent_name="main",
            data_dir=tmp_path,
        )

        with pytest.raises(ExperienceConfigError, match="max_experiences"):
            ExperienceCapability().supply(view)

    def test_conflicting_curator_interval_fails_boot(self, tmp_path: Path) -> None:
        view = PoolSupplyView(
            pool_name="test_pool",
            entries=(
                PoolSupplyAgentEntry(agent_name="main", config={"curator_interval": 60}),
                PoolSupplyAgentEntry(agent_name="sub", config={"curator_interval": 3600}),
            ),
            root_agent_name="main",
            data_dir=tmp_path,
        )

        with pytest.raises(ExperienceConfigError, match="curator_interval"):
            ExperienceCapability().supply(view)

    def test_matching_pool_config_with_distinct_review_config_boots(
        self, tmp_path: Path
    ) -> None:
        """Per-agent review knobs stay distinct while pool knobs agree."""
        view = PoolSupplyView(
            pool_name="test_pool",
            entries=(
                PoolSupplyAgentEntry(
                    agent_name="main",
                    config={"max_experiences": 10, "min_messages": 5},
                ),
                PoolSupplyAgentEntry(
                    agent_name="sub",
                    config={"max_experiences": 10, "min_messages": 40},
                ),
            ),
            root_agent_name="main",
            data_dir=tmp_path,
        )

        supply = ExperienceCapability().supply(view)

        assert supply.pool_config.max_experiences == 10
        assert supply.review_config_by_agent["main"].min_messages == 5
        assert supply.review_config_by_agent["sub"].min_messages == 40

    def test_inert_enabled_field_is_gone(self) -> None:
        """The retired ``enabled`` knob is deleted — effectiveness is the
        capability's only enablement."""
        with pytest.raises(Exception, match="Extra inputs"):
            ExperienceCapability().config_model().model_validate({"enabled": True})


# ─── Lifecycle: supply() constructs; assembly starts; teardown stops ─────────


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
        """With interval 0 the curator runs immediately and LRU evicts the
        least-recently-used excess experience."""
        from modex_agent.plugins.defaults.capabilities.experience.metadata import (
            ExperienceMetaRecord,
        )

        capability = ExperienceCapability()
        view = PoolSupplyView(
            pool_name="test_pool",
            entries=(
                PoolSupplyAgentEntry(
                    agent_name="main",
                    config={"max_experiences": 2, "curator_interval": 0},
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


# ─── Review-task ownership (§10.5 — the retired hook-owned set died) ─────────


class TestReviewTaskOwnership:
    async def test_submission_accepted_while_running(self, tmp_path: Path) -> None:
        supply = ExperienceCapability().supply(_view(tmp_path))
        await supply.start()

        started = asyncio.Event()

        async def review() -> None:
            started.set()
            await asyncio.sleep(0.2)

        task = supply.submit_review(
            agent_name="main", review_factory=review, invocation_id="iv-1"
        )
        assert task is not None
        await asyncio.wait_for(started.wait(), timeout=1)
        assert supply.review_in_flight("main")
        await supply.stop()
        assert not supply.review_in_flight("main")

    async def test_submission_rejected_during_stop(self, tmp_path: Path) -> None:
        supply = ExperienceCapability().supply(_view(tmp_path))
        await supply.start()
        await supply.stop()

        async def review() -> None:  # pragma: no cover — must never run
            raise AssertionError("rejected submission ran")

        built = False

        def review_factory():
            nonlocal built
            built = True
            return review()

        assert (
            supply.submit_review(
                agent_name="main", review_factory=review_factory, invocation_id="x"
            )
            is None
        )
        assert not built

    async def test_stop_cancels_and_awaits_pending_review(self, tmp_path: Path) -> None:
        supply = ExperienceCapability().supply(_view(tmp_path))
        await supply.start()
        cancelled = asyncio.Event()

        async def review() -> None:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = supply.submit_review(
            agent_name="main", review_factory=review, invocation_id="iv-2"
        )
        assert task is not None
        await asyncio.sleep(0)

        await supply.stop()

        assert cancelled.is_set()
        assert task.cancelled() or task.done()

    async def test_failed_review_is_isolated(self, tmp_path: Path) -> None:
        """A crashing review task is logged and swallowed — the supply (and
        the completed foreground turn) survive."""
        supply = ExperienceCapability().supply(_view(tmp_path))
        await supply.start()

        async def review() -> None:
            raise RuntimeError("review exploded")

        task = supply.submit_review(
            agent_name="main", review_factory=review, invocation_id="iv-3"
        )
        assert task is not None
        await asyncio.wait_for(task, timeout=1)
        await supply.stop()  # still stoppable

    async def test_second_submission_for_agent_is_rejected_atomically(
        self, tmp_path: Path
    ) -> None:
        supply = ExperienceCapability().supply(_view(tmp_path))
        await supply.start()
        release = asyncio.Event()
        second_built = False

        async def first_review() -> None:
            await release.wait()

        async def second_review() -> None:
            raise AssertionError("duplicate review ran")

        def second_factory():
            nonlocal second_built
            second_built = True
            return second_review()

        first = supply.submit_review(
            agent_name="main", review_factory=first_review, invocation_id="first"
        )
        assert first is not None
        assert (
            supply.submit_review(
                agent_name="main",
                review_factory=second_factory,
                invocation_id="second",
            )
            is None
        )
        assert not second_built

        release.set()
        await first
        await supply.stop()


# ─── Dark-supply pin ─────────────────────────────────────────────────────────


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


# ─── Loud supply reads ───────────────────────────────────────────────────────


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
        capability = ExperienceCapability()
        ctx = AgentContext(
            registry=MagicMock(),
            workspace_ctx=MagicMock(),
            pool_runtime=PoolRuntimeDeps(),
            agent_name="main",
        )

        with pytest.raises(ValueError, match="experience"):
            await capability.assemble(
                CapabilityBinding(active_sections=(_INJECTION_SECTION,)), ctx
            )

    async def test_version_is_content_hash_and_refreshes_on_change(self, tmp_path: Path) -> None:
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


# ─── Anchor geometry ─────────────────────────────────────────────────────────


class TestAnchorPosition:
    async def test_experience_section_renders_at_capability_anchor(self, tmp_path: Path) -> None:
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
