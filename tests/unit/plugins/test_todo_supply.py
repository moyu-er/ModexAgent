"""The todo capability's supply face — the T11 convergence pins (SPEC §8.2).

Covers:

- **Construction parity** — ``TodoCapability.supply()`` reproduces the
  retired BIZ ``build_pool_todo_store``'s behavior: the pool_data
  runtime_dir/todos path, the data_dir-relative fallback, and the
  SQLITE/FILE backend selection.
- **Loud supply reads** — ``require_todo_supply`` raises with the
  capability name + repair path when the pool's ``capability_supply``
  carries no ``todo`` key or a wrong-typed entry; the TOOL/HOOK
  factories share that behavior.
- **Three-carrier death** — ``PoolRuntimeDeps.todo_store``,
  ``AgentMaterializeDeps.todo_store``, ``StrategyAssembly``'s side
  product, and the BIZ ``build_pool_todo_store``/``build_todo_store``
  are gone from the source (grep-clean assertions).
- **Dark-supply pin** — a pool with ZERO todo-capability agents builds
  NO todo supply (the pre-migration always-built store died, SPEC P5);
  hand-referencing a todo component against such a pool raises loudly.
- **Reorientation derivation** — the roster-dispatched factory derives
  ``has_archive`` the way the two dead injection points did (native main
  ← pool memory config; native sub ← always False).
- **WebUI panel** — ``resolve_runtime_stores`` reads the pool's supply
  store (identity parity with the todo tools) in SQLITE mode.
- **Golden split-brain** — the shipped tree's supply_keys facet stays
  equal to the machine-captured pre-migration goldens (todo key present
  exactly where the pre-migration store was always built).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.ioc.configs.app import AppConfig
from modex_agent.ioc.configs.memory import ArchiveConfig, MemoryConfig
from modex_agent.multi_agent.execution_strategy import (
    ExecutionStrategy,
    PoolAssemblyContext,
    StrategyAssembly,
)
from modex_agent.multi_agent.pool import AgentPool
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.persistence.config import PersistenceBackend
from modex_agent.plugins.abc import ComponentSlot, SimpleFactory
from modex_agent.plugins.assembly.builder import AssemblyBuilder
from modex_agent.plugins.assembly.context import (
    AgentContext,
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
)
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.defaults.capabilities.todo import (
    TodoCapability,
    TodoSupply,
    require_todo_supply,
)
from modex_agent.plugins.defaults.hooks import (
    TodoContinuationHookFactory,
    TodoReorientationHookFactory,
)
from modex_agent.plugins.defaults.tools import TodoToolFactory
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.runtime.todo import JsonFileTodoStore, TodoItem, TodoStore
from modex_agent.tools.standard.todo_tool import TodoReadTool
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

_ROOT = Path(__file__).resolve().parents[3]
_BOT_PROJECT = _ROOT / "examples" / "bot_project"
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))


# ─── Harness ─────────────────────────────────────────────────────────────────


def _make_registry() -> ComponentRegistry:
    """DefaultPlugin (the production registration face — the todo
    capability lives there) plus a stub EXECUTION_STRATEGY named "stub"."""
    from pydantic import BaseModel, ConfigDict

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


def _make_pool_assembly_ctx(
    data_dir: Path,
    *,
    runtime_dir: Path | None = None,
    app_config: AppConfig | None = None,
    persistence: Any | None = None,
    assembly_deps: PoolAssemblyDeps | None = None,
) -> PoolAssemblyContext:
    pool_data = None
    if runtime_dir is not None:
        pool_data = MagicMock()
        pool_data.runtime_dir = runtime_dir
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
        app_config=app_config,
        persistence=persistence,
        pool_data=pool_data,
        assembly_deps=assembly_deps,
    )


async def _run_stage(
    pool_assembly_ctx: PoolAssemblyContext,
    specs: tuple[AssemblySpec, ...],
    registry: ComponentRegistry,
) -> PoolRuntimeDeps:
    builder = AssemblyBuilder()
    builder.infra = SupplyInfra(
        pool_assembly_ctx=pool_assembly_ctx,
        pool=MagicMock(spec=AgentPool),
        pool_specs=specs,
    )
    from modex_agent.plugins.assembly.context import AssemblyContext

    ctx = AssemblyContext(
        registry=registry,
        workspace_ctx=MagicMock(),
        infra=builder.infra,
    )
    await PoolAssembleStage().process(specs[0], builder, ctx)
    propagated = builder.propagated_context
    assert propagated is not None and propagated.pool_runtime is not None
    return propagated.pool_runtime


def _todo_entry(agent: str = "main") -> PoolSupplyAgentEntry:
    return PoolSupplyAgentEntry(agent_name=agent, config={})


def _view(**kwargs: Any) -> PoolSupplyView:
    return PoolSupplyView(pool_name="main", entries=(_todo_entry(),), **kwargs)


# ─── Construction parity with the retired BIZ builder ────────────────────────


class TestConstructionParity:
    def test_pool_data_runtime_dir_shape(self, tmp_path: Path) -> None:
        runtime_dir = tmp_path / "ws" / "runtime_state" / "main"

        supply = TodoCapability().supply(_view(runtime_dir=runtime_dir, data_dir=tmp_path))

        assert isinstance(supply, TodoSupply)
        assert isinstance(supply.store, JsonFileTodoStore)
        assert supply.store._base_dir == runtime_dir / "todos"  # noqa: SLF001

    def test_data_dir_fallback_shape_without_pool_data(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"

        supply = TodoCapability().supply(_view(data_dir=data_dir))

        assert isinstance(supply.store, JsonFileTodoStore)
        assert supply.store._base_dir == (  # noqa: SLF001
            data_dir / "runtime_state" / "main" / "todos"
        )

    def test_no_paths_raises_loudly(self) -> None:
        with pytest.raises(ValueError, match="'todo'"):
            TodoCapability().supply(_view())

    def test_sqlite_backend_selects_shared_connection_store(self) -> None:
        from modex_agent.persistence.adapters.todo_store import SqliteTodoStore

        persistence = MagicMock()
        persistence.connection = MagicMock(name="workspace_connection")

        supply = TodoCapability().supply(
            _view(
                data_dir=Path("/tmp/d"),
                persistence=persistence,
                persistence_backend=PersistenceBackend.SQLITE.value,
            )
        )

        assert isinstance(supply.store, SqliteTodoStore)
        # The store rides the workspace's shared connection (identity with
        # the tool-facing store — one construction authority).
        assert supply.store._connection is persistence.connection  # noqa: SLF001

    def test_sqlite_backend_without_manager_falls_back_to_file(self, tmp_path: Path) -> None:
        supply = TodoCapability().supply(
            _view(
                data_dir=tmp_path,
                persistence=None,
                persistence_backend=PersistenceBackend.SQLITE.value,
            )
        )

        assert isinstance(supply.store, JsonFileTodoStore)

    def test_file_backend_ignores_persistence_handle(self, tmp_path: Path) -> None:
        supply = TodoCapability().supply(
            _view(
                data_dir=tmp_path,
                persistence=MagicMock(),
                persistence_backend=PersistenceBackend.FILE.value,
            )
        )

        assert isinstance(supply.store, JsonFileTodoStore)


# ─── Stage aggregation end-to-end (the S phase) ──────────────────────────────


class TestStageAggregation:
    async def test_todo_capability_effective_builds_supply_at_fallback_path(
        self, tmp_path: Path
    ) -> None:
        registry = _make_registry()
        ctx = _make_pool_assembly_ctx(tmp_path)
        spec = _make_spec(
            capabilities=(CompiledCapability(name="todo", config={}, binding=CapabilityBinding()),)
        )

        pool_runtime = await _run_stage(ctx, (spec,), registry)

        supply = pool_runtime.capability_supply.get("todo")
        assert isinstance(supply, TodoSupply)
        assert isinstance(supply.store, JsonFileTodoStore)
        assert supply.store._base_dir == (  # noqa: SLF001
            tmp_path / "runtime_state" / "test_pool" / "todos"
        )

    async def test_view_carries_resource_fields(self, tmp_path: Path) -> None:
        captured: list[PoolSupplyView] = []
        capability = TodoCapability()
        original = capability.supply

        def capturing_supply(view: PoolSupplyView) -> TodoSupply:
            captured.append(view)
            return original(view)

        capability.supply = capturing_supply  # type: ignore[method-assign]
        registry = _make_registry()
        registry._factories[ComponentSlot.CAPABILITY]["todo"] = capability  # noqa: SLF001
        ctx = _make_pool_assembly_ctx(
            tmp_path,
            runtime_dir=tmp_path / "rt",
            persistence=MagicMock(name="mgr"),
        )
        spec = _make_spec(
            capabilities=(CompiledCapability(name="todo", config={}, binding=CapabilityBinding()),)
        )

        await _run_stage(ctx, (spec,), registry)

        (view,) = captured
        assert view.pool_name == "test_pool"
        assert view.data_dir == tmp_path
        assert view.runtime_dir == tmp_path / "rt"
        assert view.persistence is ctx.persistence
        assert view.persistence_backend is None  # no app config on the ctx


# ─── Dark-supply pin (SPEC P5 — the always-built store died) ─────────────────


class _FakeStore(TodoStore):
    async def save(self, session_id: str, todos: list[TodoItem]) -> None:
        return None

    async def get(self, session_id: str) -> list[TodoItem]:
        return []

    async def delete(self, session_id: str) -> None:
        return None


class TestDarkSupplyPin:
    async def test_pool_without_todo_agents_has_no_todo_key(self, tmp_path: Path) -> None:
        registry = _make_registry()
        ctx = _make_pool_assembly_ctx(tmp_path)
        spec = _make_spec()  # no capabilities

        pool_runtime = await _run_stage(ctx, (spec,), registry)

        assert "todo" not in pool_runtime.capability_supply

    async def test_hand_referenced_todo_tool_raises_loudly(self, tmp_path: Path) -> None:
        """A bare roster reference of ``todo_write`` on a pool without the
        todo capability loud-fails at the factory — the dark-supply death."""
        registry = _make_registry()
        ctx = _make_pool_assembly_ctx(tmp_path)
        spec = _make_spec()

        pool_runtime = await _run_stage(ctx, (spec,), registry)

        factory = TodoToolFactory(TodoReadTool)
        chain = AgentContext(
            registry=registry,
            workspace_ctx=MagicMock(),
            pool_runtime=pool_runtime,
            agent_name="probe",
        )
        with pytest.raises(ValueError, match="todo") as excinfo:
            await factory.create(factory.config_model(), chain)
        assert "capabilities: {todo: {}}" in str(excinfo.value)

    async def test_hand_referenced_continuation_hook_raises_loudly(self, tmp_path: Path) -> None:
        registry = _make_registry()
        ctx = _make_pool_assembly_ctx(tmp_path)
        spec = _make_spec()

        pool_runtime = await _run_stage(ctx, (spec,), registry)

        chain = AgentContext(
            registry=registry,
            workspace_ctx=MagicMock(),
            pool_runtime=pool_runtime,
            agent_name="probe",
        )
        with pytest.raises(ValueError, match="todo"):
            await TodoContinuationHookFactory().create(MagicMock(), chain)

    async def test_hand_referenced_reorientation_hook_raises_loudly(self, tmp_path: Path) -> None:
        registry = _make_registry()
        ctx = _make_pool_assembly_ctx(tmp_path)
        spec = _make_spec()

        pool_runtime = await _run_stage(ctx, (spec,), registry)

        chain = AgentContext(
            registry=registry,
            workspace_ctx=MagicMock(),
            pool_runtime=pool_runtime,
            agent_name="probe",
        )
        with pytest.raises(ValueError, match="todo"):
            await TodoReorientationHookFactory().create(MagicMock(), chain)


# ─── Loud supply reads ───────────────────────────────────────────────────────


class _WrongSupply(CapabilitySupply):
    pass


class TestLoudSupplyReads:
    def test_missing_pool_runtime_raises(self) -> None:
        with pytest.raises(ValueError, match="todo"):
            require_todo_supply(None)

    def test_missing_key_names_repair_path(self) -> None:
        with pytest.raises(ValueError, match=r"capabilities: \{todo: \{\}\}"):
            require_todo_supply(PoolRuntimeDeps())

    def test_wrong_type_names_expected_and_actual(self) -> None:
        pool_runtime = PoolRuntimeDeps(capability_supply={"todo": _WrongSupply()})

        with pytest.raises(ValueError, match=r"TodoSupply.*_WrongSupply|_WrongSupply.*TodoSupply"):
            require_todo_supply(pool_runtime)

    def test_concrete_supply_round_trips(self) -> None:
        supply = TodoSupply(store=_FakeStore())
        pool_runtime = PoolRuntimeDeps(capability_supply={"todo": supply})

        assert require_todo_supply(pool_runtime) is supply


# ─── Reorientation has_archive derivation (the injection deaths' values) ─────


def _archive_enabled_cfg() -> PoolAssemblyDeps:
    return PoolAssemblyDeps(memory=MemoryConfig(archive=ArchiveConfig(enabled=True)))


def _reorientation_ctx(
    pool_assembly_ctx: PoolAssemblyContext,
    *,
    agent_type: str,
) -> AgentContext:
    from modex_agent.plugins.abc import AgentType

    spec = MagicMock()
    spec.agent_type = AgentType(agent_type)
    return AgentContext(
        registry=MagicMock(),
        workspace_ctx=MagicMock(),
        pool_runtime=PoolRuntimeDeps(
            pool_assembly_ctx=pool_assembly_ctx,
            capability_supply={"todo": TodoSupply(store=_FakeStore())},
        ),
        agent_name="probe",
        spec=spec,
    )


class TestReorientationHasArchive:
    async def test_native_main_derives_from_pool_memory_config(self, tmp_path: Path) -> None:
        ctx = _reorientation_ctx(
            _make_pool_assembly_ctx(tmp_path, assembly_deps=_archive_enabled_cfg()),
            agent_type="native_main",
        )

        hook = await TodoReorientationHookFactory().create(MagicMock(), ctx)

        assert hook._has_archive is True  # noqa: SLF001

    async def test_native_main_without_archive_is_false(self, tmp_path: Path) -> None:
        ctx = _reorientation_ctx(
            _make_pool_assembly_ctx(
                tmp_path, assembly_deps=PoolAssemblyDeps(memory=MemoryConfig())
            ),
            agent_type="native_main",
        )

        hook = await TodoReorientationHookFactory().create(MagicMock(), ctx)

        assert hook._has_archive is False  # noqa: SLF001

    async def test_native_sub_is_always_false_even_with_pool_archive(self, tmp_path: Path) -> None:
        """The retired subagent injection hardcoded ``has_archive=False``
        (session-only memory never archives) — the factory keeps that."""
        ctx = _reorientation_ctx(
            _make_pool_assembly_ctx(tmp_path, assembly_deps=_archive_enabled_cfg()),
            agent_type="native_sub",
        )

        hook = await TodoReorientationHookFactory().create(MagicMock(), ctx)

        assert hook._has_archive is False  # noqa: SLF001

    async def test_hook_carries_the_supply_store(self, tmp_path: Path) -> None:
        store = _FakeStore()
        ctx = AgentContext(
            registry=MagicMock(),
            workspace_ctx=MagicMock(),
            pool_runtime=PoolRuntimeDeps(capability_supply={"todo": TodoSupply(store=store)}),
            agent_name="probe",
        )

        hook = await TodoReorientationHookFactory().create(MagicMock(), ctx)

        assert hook._todo_store is store  # noqa: SLF001


# ─── Three-carrier death (grep-clean assertions) ─────────────────────────────


class TestCarrierDeath:
    def _source(self, relative: str) -> str:
        return (_ROOT / relative).read_text(encoding="utf-8")

    def test_pool_runtime_deps_field_gone(self) -> None:
        assert "todo_store" not in self._source("src/modex_agent/plugins/assembly/context.py")

    def test_agent_materialize_deps_field_gone(self) -> None:
        assert "todo_store" not in self._source("src/modex_agent/multi_agent/materialize_deps.py")

    def test_strategy_assembly_side_product_gone(self) -> None:
        source = self._source("src/modex_agent/multi_agent/execution_strategy.py")
        assert "todo_store" not in source
        assert "JsonFileTodoStore" not in source

    def test_biz_builders_gone(self) -> None:
        source = self._source("examples/bot_project/bot/service/builders.py")
        assert "build_pool_todo_store" not in source
        assert "build_todo_store" not in source

    def test_react_strategy_construction_gone(self) -> None:
        assert "build_pool_todo_store" not in self._source(
            "examples/bot_project/bot/service/react_strategy.py"
        )

    def test_wiring_param_gone(self) -> None:
        # The wiring module itself died with the W6 position-default
        # convergence — the todo-continuation hook rides the roster.
        assert not (_ROOT / "src/modex_agent/hook/wiring.py").exists()

    def test_old_declaration_face_fails_loud_at_load(self, tmp_path: Path) -> None:
        """The retired supplement declaration key is a LOADER rejection:
        the field is gone from the frozen extra-forbid model, so any
        value under it surfaces as an unknown-field pydantic
        ``ValidationError`` at boot."""
        from pydantic import ValidationError

        from modex_agent.scope import load_scope_declaration

        yml = tmp_path / "old-face.yml"
        yml.write_text(
            "pool:\n  name: p\n  agents:\n    root:\n      tool_supplements: [todo]\n",
            encoding="utf-8",
        )
        with pytest.raises(ValidationError, match="tool_supplements"):
            load_scope_declaration(yml)


# ─── WebUI panel — identity parity with the tools' store ─────────────────────


class _FakeResources:
    def __init__(self, pool: str, supply: TodoSupply | None) -> None:
        pool_data = MagicMock()
        pool_data.turn_store = MagicMock(name="turn_store")
        self.pool_data: dict[str, Any] = {pool: pool_data}
        instance = MagicMock()
        deps = MagicMock()
        deps.capability_supply = {"todo": supply} if supply is not None else {}
        instance.pool.materialize_deps = deps
        self.pools: dict[str, Any] = {pool: instance}


def _workspace_stack(resources: _FakeResources) -> Any:
    stack = MagicMock()
    stack.registry.get_or_open = AsyncMock(return_value=MagicMock())
    stack.registry.materialize = AsyncMock(return_value=resources)
    return stack


class TestWebuiPanelRead:
    async def test_file_backend_returns_empty_stores(self) -> None:
        from bot.webui.workspace_providers import resolve_runtime_stores

        app_config = AppConfig.model_validate(
            {"persistence": {"backend": PersistenceBackend.FILE.value}}
        )

        stores = await resolve_runtime_stores(
            _workspace_stack(MagicMock()), app_config, Path("/ws"), "main"
        )

        assert stores.todo_store is None
        assert stores.turn_store is None

    async def test_sqlite_reads_the_pool_supply_store_identity(self) -> None:
        from bot.webui.workspace_providers import resolve_runtime_stores

        supply = TodoSupply(store=_FakeStore())
        resources = _FakeResources("main", supply)

        stores = await resolve_runtime_stores(
            _workspace_stack(resources), AppConfig(), Path("/ws"), "main"
        )

        # Identity parity: the panel reads the SAME store instance the
        # todo tools write through (the pre-migration WebUI built a
        # second store over the same storage).
        assert stores.todo_store is supply.store
        assert stores.turn_store is resources.pool_data["main"].turn_store

    async def test_sqlite_pool_without_todo_capability_gets_no_store(self) -> None:
        from bot.webui.workspace_providers import resolve_runtime_stores

        resources = _FakeResources("main", None)

        stores = await resolve_runtime_stores(
            _workspace_stack(resources), AppConfig(), Path("/ws"), "main"
        )

        assert stores.todo_store is None


# ─── Golden split-brain — supply_keys facet (the always-built → iff-effective) ─


class TestGoldenSupplyKeys:
    async def test_shipped_tree_supply_keys_match_pre_migration_goldens(self) -> None:
        from tests.unit.scope.goldens.assertor import GoldenFile
        from tests.unit.scope.goldens.capture import (
            GoldenPackage,
            capture_package_facets,
        )

        actual = await capture_package_facets(GoldenPackage.TODO)
        golden_dir = _ROOT / "tests" / "unit" / "scope" / "goldens" / "todo"

        assert sorted(actual) == ["coder", "default", "opencode", "review"]
        for pool, document in actual.items():
            golden = GoldenFile.model_validate_json(
                (golden_dir / f"{pool}.json").read_text(encoding="utf-8")
            ).root
            for agent, facets in document.root.items():
                expected = golden[agent].supply_keys
                if pool == "opencode":
                    # The subagents wave (T15) switched the capture's
                    # subagents supply-key projection to compile-product
                    # authority: the external opencode pool compiles no
                    # capabilities, so the key drops even though the runtime
                    # communication construction is still unconditional in
                    # this wave (it dies with the subagents supply wave).
                    expected = tuple(k for k in expected if k != "subagents")
                # The pre-migration capture recorded the ALWAYS-built todo
                # store for native pools; the migrated tree builds the supply
                # iff the capability is effective. The shipped tree's todo
                # agents all declare the capability, so the facets must be
                # exactly equal — no exemption.
                assert facets.supply_keys == expected, (pool, agent)
