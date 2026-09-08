"""Multi-workspace resource isolation integration test.

After the poll-driven redesign (ADR-0015), the inbox/bus/poller live PER-POOL,
not on the per-workspace ``PoolWorkspaceResources``. What remains
workspace-scoped is the broker (cross-process wakeup) and the on-disk skeleton
(each workspace owns its own ``.modex`` directory tree, including its own
``inbox_dir``). This file asserts those workspace-level invariants.

Per-pool inbox isolation (the old "A's consumer never sees B's message"
property) is covered by ``tests/integration/multi_agent/test_multi_pool_isolation.py``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from bot.workspace.handle import PoolWorkspaceResources

from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.persistence.adapters.file_session_store import LocalFileSessionStore
from modex_agent.tools.overflow.local import LocalFileToolOverflowStore
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.factory import ResourceFactory
from modex_agent.workspace.registry import ScopeRegistry
from modex_agent.workspace.store import GlobalWorkspaceStore

# ── Helpers ─────────────────────────────────────────────────────────────────


def _build_minimal_resources(target: Path) -> PoolWorkspaceResources:
    """Build a minimal PoolWorkspaceResources with a distinct per-workspace broker."""
    ctx = WorkspaceContext.from_target(
        target, data_dir_name=".modex", home=target.parent
    )
    ctx.paths.mkdir_skeleton()
    overflow_store = LocalFileToolOverflowStore(
        workspace=ctx.paths.overflow_dir
    )
    session_index_store = LocalFileSessionStore(root=ctx.paths.session_index_dir)
    broker = InMemoryMessageBroker()
    return PoolWorkspaceResources(
        target=target,
        ctx=ctx,
        overflow_store=overflow_store,
        session_index_store=session_index_store,
        broker=broker,
    )


class _MinimalResourceFactory(ResourceFactory[PoolWorkspaceResources]):
    """Test factory that builds minimal resources without a full BotService."""

    async def materialize(self, ctx: WorkspaceContext) -> PoolWorkspaceResources:
        return _build_minimal_resources(ctx.target)

    async def evict(self, resources: PoolWorkspaceResources) -> None:
        await resources.broker.stop()


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
async def isolated_workspaces(
    tmp_path: Path,
) -> AsyncGenerator[tuple[ScopeRegistry[PoolWorkspaceResources], Path, Path], None]:
    """Yield a registry with two materialized workspaces (A and B), brokers started."""
    home = tmp_path
    ws_a = tmp_path / "workspace_a"
    ws_b = tmp_path / "workspace_b"
    ws_a.mkdir()
    ws_b.mkdir()

    factory = _MinimalResourceFactory()
    store = GlobalWorkspaceStore(home=home, data_dir_name=".modex")
    registry: ScopeRegistry[PoolWorkspaceResources] = ScopeRegistry(
        home=home,
        data_dir_name=".modex",
        factory=factory,
        store=store,
    )

    ctx_a = await registry.get_or_open(ws_a)
    ctx_b = await registry.get_or_open(ws_b)
    resources_a = await registry.materialize(ctx_a)
    resources_b = await registry.materialize(ctx_b)

    await resources_a.broker.start()
    await resources_b.broker.start()

    yield registry, ws_a, ws_b

    await registry.evict_and_release(ws_a)
    await registry.evict_and_release(ws_b)


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_two_workspaces_have_distinct_resources(
    isolated_workspaces: tuple[ScopeRegistry[PoolWorkspaceResources], Path, Path],
) -> None:
    """Each workspace gets its own broker and its own on-disk skeleton."""
    registry, ws_a, ws_b = isolated_workspaces

    ctx_a = await registry.get_or_open(ws_a)
    ctx_b = await registry.get_or_open(ws_b)
    resources_a = await registry.materialize(ctx_a)
    resources_b = await registry.materialize(ctx_b)

    # Distinct brokers (per-workspace wakeup channel)
    assert resources_a.broker is not resources_b.broker

    # Distinct on-disk roots — each workspace owns its own .modex tree, which
    # is where its pools' LocalFileInboxServer will root their distinct inboxes.
    assert resources_a.ctx.paths.inbox_dir != resources_b.ctx.paths.inbox_dir
    assert resources_a.ctx.paths.root != resources_b.ctx.paths.root
