"""Multi-workspace inbox isolation integration test.

Verifies that each workspace has its own inbox_server / inbox_consumer /
agent_bus, so messages posted to workspace A's inbox are consumed only by
workspace A's consumer, even when workspace B is also materialized.
"""

from __future__ import annotations

from pathlib import Path
from typing import AsyncGenerator

import pytest

from bot.workspace.handle import PoolWorkspaceResources
from framework.workspace.context import WorkspaceContext
from framework.workspace.factory import ResourceFactory
from framework.workspace.registry import InMemoryRegistryStore, WorkspaceRegistry
from framework.workspace.routing import WorkspaceResolver
from framework.core.session_store import LocalFileSessionStore
from framework.messaging.broker_memory import InMemoryMessageBroker
from framework.multi_agent.address import AgentAddress
from framework.multi_agent.bus import LocalAgentMessageBus
from framework.multi_agent.envelope import AgentMessageEnvelope
from framework.multi_agent.inbox.consumer import InboxConsumer
from framework.multi_agent.inbox.producer import InboxProducer
from framework.multi_agent.inbox.server_local import LocalFileInboxServer
from framework.tools.overflow.local import LocalFileToolOverflowStore


# ── Module-level constants ───────────────────────────────────────────────────

SESSION_A = "session-A"
SESSION_B = "session-B"
SESSION_BUS = "session-bus"
SESSION_MIGRATE = "session-migrate"


# ── Helpers ─────────────────────────────────────────────────────────────────


def _build_minimal_resources(target: Path) -> PoolWorkspaceResources:
    """Build a minimal PoolWorkspaceResources with distinct per-workspace inbox/broker/bus."""
    ctx = WorkspaceContext.from_target(
        target, data_dir_name=".modex", home=target.parent
    )
    ctx.paths.mkdir_skeleton()
    inbox_server = LocalFileInboxServer(workspace=ctx.paths.inbox_dir)
    overflow_store = LocalFileToolOverflowStore(
        workspace=ctx.paths.overflow_dir, max_chunk_size=10_000
    )
    session_index_store = LocalFileSessionStore(root=ctx.paths.session_index_dir)
    broker = InMemoryMessageBroker()
    inbox_producer = InboxProducer(server=inbox_server)
    inbox_consumer = InboxConsumer(server=inbox_server)
    agent_bus = LocalAgentMessageBus(
        producer=inbox_producer, consumer=inbox_consumer, broker=broker
    )
    return PoolWorkspaceResources(
        target=target,
        ctx=ctx,
        inbox_server=inbox_server,
        overflow_store=overflow_store,
        session_index_store=session_index_store,
        broker=broker,
        inbox_producer=inbox_producer,
        inbox_consumer=inbox_consumer,
        agent_bus=agent_bus,
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
) -> AsyncGenerator[tuple[WorkspaceRegistry[PoolWorkspaceResources], Path, Path], None]:
    """Yield a registry with two materialized workspaces (A and B) and auto-started brokers.

    Cleans up via evict_and_release even if a test fails.
    """
    home = tmp_path
    ws_a = tmp_path / "workspace_a"
    ws_b = tmp_path / "workspace_b"
    ws_a.mkdir()
    ws_b.mkdir()

    factory = _MinimalResourceFactory()
    store = InMemoryRegistryStore()
    registry: WorkspaceRegistry[PoolWorkspaceResources] = WorkspaceRegistry(
        home=home,
        data_dir_name=".modex",
        factory=factory,
        store=store,
    )

    ctx_a = registry.get_or_open(ws_a)
    ctx_b = registry.get_or_open(ws_b)
    resources_a = await registry.materialize(ctx_a)
    resources_b = await registry.materialize(ctx_b)

    # Auto-start brokers so bus signaling works consistently across all tests
    await resources_a.broker.start()
    await resources_b.broker.start()

    yield registry, ws_a, ws_b

    # Cleanup: release resources even if test assertions fail
    await registry.evict_and_release(ws_a)
    await registry.evict_and_release(ws_b)


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_two_workspaces_have_distinct_resources(
    isolated_workspaces: tuple[WorkspaceRegistry[PoolWorkspaceResources], Path, Path],
) -> None:
    """Each workspace gets its own inbox server, broker, and bus."""
    registry, ws_a, ws_b = isolated_workspaces

    ctx_a = registry.get_or_open(ws_a)
    ctx_b = registry.get_or_open(ws_b)
    resources_a = await registry.materialize(ctx_a)
    resources_b = await registry.materialize(ctx_b)

    # Distinct objects
    assert resources_a.inbox_server is not resources_b.inbox_server
    assert resources_a.broker is not resources_b.broker
    assert resources_a.agent_bus is not resources_b.agent_bus
    assert resources_a.inbox_consumer is not resources_b.inbox_consumer
    assert resources_a.inbox_producer is not resources_b.inbox_producer

    # Distinct on-disk roots
    assert resources_a.ctx.paths.inbox_dir != resources_b.ctx.paths.inbox_dir


@pytest.mark.asyncio
async def test_message_to_workspace_a_stays_in_a(
    isolated_workspaces: tuple[WorkspaceRegistry[PoolWorkspaceResources], Path, Path],
) -> None:
    """Post a message to workspace A's inbox; B's consumer sees nothing."""
    registry, ws_a, ws_b = isolated_workspaces

    ctx_a = registry.get_or_open(ws_a)
    ctx_b = registry.get_or_open(ws_b)
    resources_a = await registry.materialize(ctx_a)
    resources_b = await registry.materialize(ctx_b)

    # Build an inter-agent envelope and post it into A's inbox via A's producer
    envelope = AgentMessageEnvelope(
        payload={"content": "hello from A"},
        source=AgentAddress(kind="agent", name="main"),
        target=AgentAddress(kind="agent", name="sub"),
        session_id=SESSION_A,
        agent_session_id=SESSION_A,
    )
    await resources_a.inbox_producer.send(SESSION_A, envelope)

    # A's consumer should see the message
    a_messages = await resources_a.inbox_consumer.consume(SESSION_A, limit=10)
    assert len(a_messages) == 1
    assert a_messages[0].content == "hello from A"

    # B's consumer should see nothing (different inbox server)
    b_messages = await resources_b.inbox_consumer.consume(SESSION_A, limit=10)
    assert len(b_messages) == 0


@pytest.mark.asyncio
async def test_message_to_workspace_b_stays_in_b(
    isolated_workspaces: tuple[WorkspaceRegistry[PoolWorkspaceResources], Path, Path],
) -> None:
    """Post a message to workspace B's inbox; A's consumer sees nothing."""
    registry, ws_a, ws_b = isolated_workspaces

    ctx_a = registry.get_or_open(ws_a)
    ctx_b = registry.get_or_open(ws_b)
    resources_a = await registry.materialize(ctx_a)
    resources_b = await registry.materialize(ctx_b)

    envelope = AgentMessageEnvelope(
        payload={"content": "hello from B"},
        source=AgentAddress(kind="agent", name="main"),
        target=AgentAddress(kind="agent", name="sub"),
        session_id=SESSION_B,
        agent_session_id=SESSION_B,
    )
    await resources_b.inbox_producer.send(SESSION_B, envelope)

    # B's consumer should see the message
    b_messages = await resources_b.inbox_consumer.consume(SESSION_B, limit=10)
    assert len(b_messages) == 1
    assert b_messages[0].content == "hello from B"

    # A's consumer should see nothing
    a_messages = await resources_a.inbox_consumer.consume(SESSION_B, limit=10)
    assert len(a_messages) == 0


@pytest.mark.asyncio
async def test_agent_bus_isolation_across_workspaces(
    isolated_workspaces: tuple[WorkspaceRegistry[PoolWorkspaceResources], Path, Path],
) -> None:
    """Use the agent_bus (producer+consumer) to verify full-stack isolation."""
    registry, ws_a, ws_b = isolated_workspaces

    ctx_a = registry.get_or_open(ws_a)
    ctx_b = registry.get_or_open(ws_b)
    resources_a = await registry.materialize(ctx_a)
    resources_b = await registry.materialize(ctx_b)

    envelope_a = AgentMessageEnvelope(
        payload={"content": "bus message A"},
        source=AgentAddress(kind="agent", name="main"),
        target=AgentAddress(kind="agent", name="sub"),
        session_id=SESSION_BUS,
        agent_session_id=SESSION_BUS,
    )
    await resources_a.agent_bus.send(SESSION_BUS, envelope_a)

    envelope_b = AgentMessageEnvelope(
        payload={"content": "bus message B"},
        source=AgentAddress(kind="agent", name="main"),
        target=AgentAddress(kind="agent", name="sub"),
        session_id=SESSION_BUS,
        agent_session_id=SESSION_BUS,
    )
    await resources_b.agent_bus.send(SESSION_BUS, envelope_b)

    # A's bus consume should only return A's message
    a_envelopes = await resources_a.agent_bus.consume(SESSION_BUS, limit=10, block=False)
    assert len(a_envelopes) == 1
    # Use .get() for safe dict access instead of raw subscript
    assert a_envelopes[0].payload.get("content") == "bus message A"

    # B's bus consume should only return B's message
    b_envelopes = await resources_b.agent_bus.consume(SESSION_BUS, limit=10, block=False)
    assert len(b_envelopes) == 1
    assert b_envelopes[0].payload.get("content") == "bus message B"


@pytest.mark.asyncio
async def test_session_migration_resumes_pending_messages(
    tmp_path: Path,
) -> None:
    """Session A moves from workspace A to workspace B and back;
    pending messages in workspace A resume when A returns.
    """
    home = tmp_path
    ws_a = tmp_path / "workspace_a"
    ws_b = tmp_path / "workspace_b"
    ws_a.mkdir()
    ws_b.mkdir()

    factory = _MinimalResourceFactory()
    store = InMemoryRegistryStore()
    registry: WorkspaceRegistry[PoolWorkspaceResources] = WorkspaceRegistry(
        home=home,
        data_dir_name=".modex",
        factory=factory,
        store=store,
    )

    # Resolver routes by workspace Path (no session map in resolution).
    resolver = WorkspaceResolver(registry=registry)

    # Phase 1: Session starts in workspace A
    ctx_a, resources_a = await resolver.resolve(ws_a)
    assert ctx_a.target == ws_a.resolve()
    await resources_a.broker.start()

    # Post a message to workspace A's inbox for this session
    # (do NOT consume it here — it should remain pending)
    envelope_a = AgentMessageEnvelope(
        payload={"content": "message in A"},
        source=AgentAddress(kind="agent", name="main"),
        target=AgentAddress(kind="agent", name="sub"),
        session_id=SESSION_MIGRATE,
        agent_session_id=SESSION_MIGRATE,
    )
    await resources_a.inbox_producer.send(SESSION_MIGRATE, envelope_a)

    # Phase 2: session is now routed to workspace B (message carries ws_b).
    ctx_b, resources_b = await resolver.resolve(ws_b)
    assert ctx_b.target == ws_b.resolve()
    await resources_b.broker.start()

    # Post a message to workspace B's inbox
    envelope_b = AgentMessageEnvelope(
        payload={"content": "message in B"},
        source=AgentAddress(kind="agent", name="main"),
        target=AgentAddress(kind="agent", name="sub"),
        session_id=SESSION_MIGRATE,
        agent_session_id=SESSION_MIGRATE,
    )
    await resources_b.inbox_producer.send(SESSION_MIGRATE, envelope_b)

    b_messages = await resources_b.inbox_consumer.consume(SESSION_MIGRATE, limit=10)
    assert len(b_messages) == 1
    assert b_messages[0].content == "message in B"

    # Phase 3: session routed back to workspace A (message carries ws_a again).
    ctx_a2, resources_a2 = await resolver.resolve(ws_a)
    assert ctx_a2.target == ws_a.resolve()

    # The key assertion: A's inbox still holds the original pending message
    a_messages2 = await resources_a2.inbox_consumer.consume(SESSION_MIGRATE, limit=10)
    assert len(a_messages2) == 1
    assert a_messages2[0].content == "message in A"

    # Phase 4: Verify workspace B's message is still isolated
    # (B's consumer should not see A's message)
    b_messages2 = await resources_b.inbox_consumer.consume(SESSION_MIGRATE, limit=10)
    # B already consumed its message in Phase 2, so this should be 0
    assert len(b_messages2) == 0

    # Cleanup
    await registry.evict_and_release(ws_a)
    await registry.evict_and_release(ws_b)
