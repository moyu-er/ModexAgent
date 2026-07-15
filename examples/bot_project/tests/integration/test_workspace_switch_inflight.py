"""Workspace registry isolation integration test.

Verifies that workspace resources are snapshotted at resolution time and that
registering a new workspace via ``registry.get_or_open`` does not affect
in-flight turns or previously resolved resources.

This is the key property of the multi-live design: many workspaces coexist,
and a turn's resources are snapshotted at resolution time.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from bot.workspace.handle import PoolWorkspaceResources

from modex_agent.core.session_store import LocalFileSessionStore
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.tools.overflow.local import LocalFileToolOverflowStore
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.factory import ResourceFactory
from modex_agent.workspace.registry import WorkspaceRegistry
from modex_agent.workspace.routing import WorkspaceResolver
from modex_agent.workspace.store import GlobalWorkspaceStore

# ── Module-level constants ───────────────────────────────────────────────────


# ── Helpers ─────────────────────────────────────────────────────────────────


def _build_minimal_resources(target: Path) -> PoolWorkspaceResources:
    """Build a minimal PoolWorkspaceResources with a distinct per-workspace broker."""
    ctx = WorkspaceContext.from_target(
        target, data_dir_name=".modex", home=target.parent
    )
    ctx.paths.mkdir_skeleton()
    overflow_store = LocalFileToolOverflowStore(
        workspace=ctx.paths.overflow_dir, max_chunk_size=10_000
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
async def inflight_setup(
    tmp_path: Path,
) -> AsyncGenerator[
    tuple[WorkspaceResolver[PoolWorkspaceResources], WorkspaceRegistry[PoolWorkspaceResources], Path, Path],
    None,
]:
    """Yield a resolver + registry with two workspaces (A and B) ready for in-flight tests.

    Cleans up via evict_and_release even if a test fails.
    """
    home = tmp_path
    ws_a = tmp_path / "workspace_a"
    ws_b = tmp_path / "workspace_b"
    ws_a.mkdir()
    ws_b.mkdir()

    factory = _MinimalResourceFactory()
    store = GlobalWorkspaceStore(home=home, data_dir_name=".modex")
    registry: WorkspaceRegistry[PoolWorkspaceResources] = WorkspaceRegistry(
        home=home,
        data_dir_name=".modex",
        factory=factory,
        store=store,
    )

    resolver = WorkspaceResolver(registry=registry)

    yield resolver, registry, ws_a, ws_b

    # Cleanup: release resources even if test assertions fail
    await registry.evict_and_release(ws_a)
    await registry.evict_and_release(ws_b)


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inflight_turn_holds_original_resources(
    inflight_setup: tuple[
        WorkspaceResolver[PoolWorkspaceResources],
        WorkspaceRegistry[PoolWorkspaceResources],
        Path,
        Path,
    ],
) -> None:
    """A turn started in workspace A keeps writing to A even after /cd to B."""
    resolver, registry, ws_a, ws_b = inflight_setup

    # Phase 1: Turn starts in workspace A — resolve ws_a
    ctx_a, resources_a = await resolver.resolve(ws_a)
    assert ctx_a.target == ws_a.resolve()
    await resources_a.broker.start()

    # Phase 2: Mid-turn, the next message carries ws_b (simulates /cd having
    # moved the conversation). Register ws_b in the registry.
    await registry.get_or_open(ws_b)

    # Phase 3: Assert the in-flight turn's resources still point to workspace A
    # The key property: R_A is unchanged, still rooted in A
    assert resources_a.ctx.target == ws_a.resolve()
    assert resources_a.ctx.paths.root == ws_a / ".modex"

    # Phase 4: Next resolution now resolves ws_b
    ctx_b, resources_b = await resolver.resolve(ws_b)
    assert ctx_b.target == ws_b.resolve()
    await resources_b.broker.start()

    # Assert distinct contexts and resources
    assert ctx_b.target != ctx_a.target
    assert resources_b is not resources_a
    assert resources_b.ctx.paths.root == ws_b / ".modex"

    # Phase 5: No exception / busy-check was raised during switch
    # The switch is just a pointer mutation — no blocking, no teardown
    # (If we got here without exception, the test passes this criterion)


@pytest.mark.asyncio
async def test_inflight_resources_object_identity_preserved(
    inflight_setup: tuple[
        WorkspaceResolver[PoolWorkspaceResources],
        WorkspaceRegistry[PoolWorkspaceResources],
        Path,
        Path,
    ],
) -> None:
    """The original R object reference is preserved after switch; re-resolution returns a different R."""
    resolver, registry, ws_a, ws_b = inflight_setup

    # Start turn in A
    ctx_a, resources_a = await resolver.resolve(ws_a)
    await resources_a.broker.start()

    # Capture the original reference for identity checks
    original_ref = resources_a
    original_data_root = resources_a.ctx.paths.root

    # Next message carries ws_b (switch happened)
    await registry.get_or_open(ws_b)

    # Original reference unchanged
    assert resources_a is original_ref
    assert resources_a.ctx.paths.root == original_data_root

    # Re-resolve gets a new/different R
    ctx_b, resources_b = await resolver.resolve(ws_b)
    await resources_b.broker.start()

    assert resources_b is not original_ref
    assert resources_b.ctx.paths.root != original_data_root


@pytest.mark.asyncio
async def test_switch_does_not_raise_or_block(
    inflight_setup: tuple[
        WorkspaceResolver[PoolWorkspaceResources],
        WorkspaceRegistry[PoolWorkspaceResources],
        Path,
        Path,
    ],
) -> None:
    """Switching a session pointer is a pure mutation — no exception, no busy-check."""
    resolver, registry, ws_a, ws_b = inflight_setup

    # Start in A
    ctx_a, resources_a = await resolver.resolve(ws_a)
    await resources_a.broker.start()

    # Switch should not raise
    try:
        await registry.get_or_open(ws_b)
    except Exception as exc:
        pytest.fail(f"Switch raised an unexpected exception: {exc}")

    # Re-resolve should also not raise
    try:
        ctx_b, resources_b = await resolver.resolve(ws_b)
        await resources_b.broker.start()
    except Exception as exc:
        pytest.fail(f"Re-resolution after switch raised an unexpected exception: {exc}")

    # Assert the switch actually happened
    assert ctx_b.target == ws_b.resolve()
