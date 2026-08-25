from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from bot.workspace import pool_data as pool_data_module
from bot.workspace.pool_data import build_pool_data

from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.scope.spec import AgentSpec
from modex_agent.trace import OtelSpanTraceStore
from modex_agent.workspace.context import WorkspaceContext


def _pool_inputs(
    tmp_path: Path,
) -> tuple[WorkspaceContext, AgentSpec, PoolAssemblyDeps]:
    target = tmp_path / "workspace"
    target.mkdir()
    return (
        WorkspaceContext.from_target(target, data_dir_name=".modex", home=tmp_path),
        AgentSpec(name="main"),
        PoolAssemblyDeps(memory=MemoryConfig()),
    )


async def test_build_pool_data_uses_injected_trace_store(tmp_path: Path) -> None:
    # Given
    ctx, root_agent, assembly_deps = _pool_inputs(tmp_path)
    injected_store = OtelSpanTraceStore(tmp_path / "caller-trace")

    # When
    with (
        patch.object(pool_data_module, "build_trace_stores") as build_stores,
        patch.object(pool_data_module, "OtelSpanTraceStore") as store_type,
    ):
        pool_data = await build_pool_data(
            ctx,
            "test_pool",
            root_agent,
            None,
            assembly_deps,
            trace_store=injected_store,
        )

    # Then
    assert pool_data.trace_store is injected_store
    build_stores.assert_not_called()
    store_type.assert_not_called()
    memory_system = pool_data.context_manager.memory_system
    assert memory_system is not None
    await memory_system.close()
    injected_store.close()


async def test_build_pool_data_builds_default_trace_store(tmp_path: Path) -> None:
    # Given
    ctx, root_agent, assembly_deps = _pool_inputs(tmp_path)

    # When
    pool_data = await build_pool_data(
        ctx,
        "test_pool",
        root_agent,
        None,
        assembly_deps,
    )

    # Then
    default_store = pool_data.trace_store
    assert isinstance(default_store, OtelSpanTraceStore)
    memory_system = pool_data.context_manager.memory_system
    assert memory_system is not None
    await memory_system.close()
    default_store.close()
