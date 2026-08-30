"""``build_pool_data``'s ``trace_store`` seam after the `tracing` capability
convergence: the retired BIZ construction block died — the caller-carried
store (the harbor trial's collector-backed store) rides the snapshot into
the capability supply view untouched; the production path passes ``None``
and the ``tracing`` capability's supply builds the pool's store."""

from __future__ import annotations

from pathlib import Path

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


async def test_build_pool_data_carries_injected_trace_store(tmp_path: Path) -> None:
    ctx, root_agent, assembly_deps = _pool_inputs(tmp_path)
    injected_store = OtelSpanTraceStore(tmp_path / "caller-trace")

    pool_data = await build_pool_data(
        ctx,
        "test_pool",
        root_agent,
        None,
        assembly_deps,
        trace_store=injected_store,
    )

    assert pool_data.trace_store is injected_store
    memory_system = pool_data.context_manager.memory_system
    assert memory_system is not None
    await memory_system.close()
    injected_store.close()


async def test_build_pool_data_production_path_leaves_store_none(tmp_path: Path) -> None:
    """The production path builds NO store here — the `tracing`
    capability's supply owns construction (the workspace wiring stamps the
    supply's store onto the snapshot after assembly)."""
    ctx, root_agent, assembly_deps = _pool_inputs(tmp_path)

    pool_data = await build_pool_data(
        ctx,
        "test_pool",
        root_agent,
        None,
        assembly_deps,
    )

    assert pool_data.trace_store is None
    memory_system = pool_data.context_manager.memory_system
    assert memory_system is not None
    await memory_system.close()
