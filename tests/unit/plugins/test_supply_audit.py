from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from typing import Final
from unittest.mock import MagicMock

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
from modex_agent.plugins.assembly.stages.pool_assemble import PoolAssembleStage
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.defaults.capabilities.experience.tool_factory import ExperienceToolFactory
from modex_agent.plugins.defaults.communication import TaskToolFactory
from modex_agent.plugins.defaults.tools import TodoToolFactory
from modex_agent.plugins.loader import ComponentRegistryLoader, PluginDiscoveryConfig
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.scope.compiler import compile_scope
from modex_agent.scope.loader import load_scope_declaration
from modex_agent.scope.spec import PoolSpec
from modex_agent.tools.standard.todo_tool import TodoReadTool
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

_ROOT: Final = Path(__file__).resolve().parents[3]
_BOT_PROJECT: Final = _ROOT / "examples" / "bot_project"
_DECLARATION_PATH: Final = _BOT_PROJECT / "config" / "scopes" / "bot.yml"
_AUDIT_STRATEGY_NAME: Final = "supply_audit"
_DEAD_POOL_RUNTIME_FIELDS: Final = frozenset({"todo_store", "communication"})
_SUPPLY_CAPABILITY_NAMES: Final = frozenset({"experience", "skills", "subagents", "todo"})
_EXPECTED_SUPPLY_KEYS: Final[dict[str, frozenset[str]]] = {
    "default": frozenset({"experience", "skills", "subagents", "todo"}),
    "coder": frozenset({"skills", "subagents", "todo"}),
    "review": frozenset({"experience", "skills", "subagents", "todo"}),
    "opencode": frozenset(),
}

if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))


class _AuditStrategyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _AuditStrategy(ExecutionStrategy):
    @property
    def name(self) -> str:
        return _AUDIT_STRATEGY_NAME

    async def assemble_main(self, ctx: PoolAssemblyContext) -> StrategyAssembly:
        del ctx
        return StrategyAssembly()

    def validate_pool_spec(self, pool: PoolSpec) -> None:
        del pool


async def _production_registry() -> ComponentRegistry:
    registry = ComponentRegistry()
    await ComponentRegistryLoader.load(
        registry,
        PluginDiscoveryConfig(
            bundled_factories=(DefaultPlugin(),),
            project_plugin_paths=(_BOT_PROJECT / "plugins",),
        ),
    )
    registry.register(
        ComponentSlot.EXECUTION_STRATEGY,
        _AUDIT_STRATEGY_NAME,
        SimpleFactory(_AuditStrategy(), _AuditStrategyConfig),
    )
    return registry


def _missing_supply_context() -> AgentContext:
    return AgentContext(
        registry=ComponentRegistry(),
        workspace_ctx=MagicMock(),
        pool_runtime=PoolRuntimeDeps(),
        agent_name="supply-audit",
    )


def test_retired_pool_runtime_supply_fields_stay_dead() -> None:
    field_names = {field.name for field in dataclasses.fields(PoolRuntimeDeps)}
    assert field_names.isdisjoint(_DEAD_POOL_RUNTIME_FIELDS)
    assert set(dir(PoolRuntimeDeps())).isdisjoint(_DEAD_POOL_RUNTIME_FIELDS)

    source_hits: list[str] = []
    for source_root in (_ROOT / "src", _ROOT / "examples"):
        for root, directories, files in source_root.walk():
            # Installed dependencies and runtime data are not project source.
            directories[:] = [
                name for name in directories
                if not name.startswith(".") and name not in {"node_modules", "__pycache__"}
            ]
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = root / name
                for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    if any(f"pool_runtime.{field}" in line for field in _DEAD_POOL_RUNTIME_FIELDS):
                        source_hits.append(f"{path.relative_to(_ROOT)}:{line_number}:{line.strip()}")
    assert source_hits == []


async def test_shipped_pool_supply_keys_match_compiled_effective_capabilities(
    tmp_path: Path,
) -> None:
    registry = await _production_registry()
    declaration = load_scope_declaration(_DECLARATION_PATH)
    assert declaration.workspace is not None
    workspace_ctx = WorkspaceContext(
        target=_BOT_PROJECT,
        paths=WorkspacePaths(root=tmp_path / ".modex"),
        is_home=True,
    )
    compilation = compile_scope(
        declaration,
        workspace_ctx=workspace_ctx,
        registry=registry,
    )
    compiled_by_pool = {
        pool.name: tuple(
            agent for agent in compilation.agents if agent.provenance.pool == pool.name
        )
        for pool in declaration.workspace.pools
    }
    assert set(compiled_by_pool) == set(_EXPECTED_SUPPLY_KEYS)

    for pool in declaration.workspace.pools:
        compiled_agents = compiled_by_pool[pool.name]
        expected_from_compilation = {
            capability.name
            for agent in compiled_agents
            for capability in agent.spec.capabilities
            if capability.name in _SUPPLY_CAPABILITY_NAMES
        }
        assert expected_from_compilation == _EXPECTED_SUPPLY_KEYS[pool.name]

        root_spec = next(
            agent.spec
            for agent in compiled_agents
            if agent.provenance.agent == pool.root_agent.name
        )
        pool_handle = MagicMock(spec=AgentPool)
        pool_handle.tree = MagicMock()
        pool_assembly_ctx = PoolAssemblyContext(
            pool_name=pool.name,
            pool_spec=pool,
            project_dir=_BOT_PROJECT,
            data_dir=tmp_path / ".modex",
            broker=MagicMock(),
            inbox_server=MagicMock(),
            agent_bus=MagicMock(),
            output_adapter=MagicMock(),
            safety=MagicMock(),
            retention=MagicMock(),
            registry=MagicMock(),
        )
        infra = SupplyInfra(
            pool_assembly_ctx=pool_assembly_ctx,
            pool=pool_handle,
            pool_specs=tuple(agent.spec for agent in compiled_agents),
        )
        builder = AssemblyBuilder()
        builder.infra = infra
        assembly_ctx = AssemblyContext(
            registry=registry,
            workspace_ctx=workspace_ctx,
            infra=infra,
        )
        stage_spec = root_spec.model_copy(
            update={
                "execution_strategy": _AUDIT_STRATEGY_NAME,
                "interceptors": [],
                "commands": None,
            }
        )
        try:
            await PoolAssembleStage().process(stage_spec, builder, assembly_ctx)
            assert builder.propagated_context is not None
            assert builder.propagated_context.pool_runtime is not None
            assert (
                set(builder.propagated_context.pool_runtime.capability_supply)
                == expected_from_compilation
            )
        finally:
            await builder.cleanup()


async def test_todo_tool_without_todo_capability_raises_with_repair_path() -> None:
    factory = TodoToolFactory(TodoReadTool)

    with pytest.raises(ValueError) as excinfo:
        await factory.create(factory.config_model(), _missing_supply_context())

    message = str(excinfo.value)
    assert "'todo' capability supply" in message
    assert "capabilities: {todo: {}}" in message


async def test_experience_tool_without_experience_capability_raises_with_repair_path() -> None:
    factory = ExperienceToolFactory()

    with pytest.raises(ValueError) as excinfo:
        await factory.create(factory.config_model(), _missing_supply_context())

    message = str(excinfo.value)
    assert "'experience' capability supply" in message
    assert "capabilities: {experience: {}}" in message


async def test_task_tool_without_subagents_capability_raises_with_repair_path() -> None:
    factory = TaskToolFactory()

    with pytest.raises(ValueError) as excinfo:
        await factory.create(factory.config_model(), _missing_supply_context())

    message = str(excinfo.value)
    assert "'subagents' capability supply" in message
    assert "tree predicate" in message
