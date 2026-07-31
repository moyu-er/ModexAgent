"""Regression tests for "no template for orchestrator; skipping" bug.

Two root causes, two fixes, two test classes:

1. InboxPoller orphan drain — a pending message whose agent_name is not
   served by this pool (no instance, no template) must be consumed once
   with a warning, not re-logged on every tick forever.

2. PoolRouter self-healing re-route — when a message's session carries an
   agent_name that the routed pool does not serve, the router finds the
   pool that does serve it and re-routes + self-heals the routing store.
   This prevents new orphans from being created.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.core.scope import RecordScope
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import InputMessage
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.inbox.types import InboxMessage
from modex_agent.multi_agent.inbox_poller import InboxPoller
from modex_agent.multi_agent.pool_instance import PoolInstance
from modex_agent.multi_agent.pool_router import LocalFilePoolRoutingStore, PoolRouter
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters.inbox_mq import SqliteInboxMQ


class _DefaultPoolScope(RecordScope):
    pool: str | None = None


# ═══════════════════════════════════════════════════════════════════════════════
# Fix 2: InboxPoller drains orphan messages instead of infinite skip
# ═══════════════════════════════════════════════════════════════════════════════


class TestInboxPollerHandlesOrphanWithoutDataLoss:
    """An orphan message (agent not served by this pool) must NOT be discarded.

    ADR-0015: "no silent drop" — a message is either pending, folded, or
    consumed by an agent turn. Draining without dispatching violates this.

    Correct behavior: log ERROR **once** per orphan session (not every tick),
    skip dispatching, leave the message **pending** (no data loss). A
    ``_orphan_logged`` set tracks already-reported sessions to prevent log
    spam. The router's write-time validation prevents new orphans; this is
    the residual safety net for pre-existing stale messages.
    """

    @pytest.mark.asyncio
    async def test_orphan_message_stays_pending_no_data_loss(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        db_path = tmp_path / ".modex" / "state.db"
        manager = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
        await manager.open()
        try:
            cli_mq = SqliteInboxMQ(
                db_path=db_path,
                scope=_DefaultPoolScope(pool="default"),
                connection=None,
            )
            stale_sid = "e3834e2a8ee0.orchestrator"
            message = InboxMessage(
                session_id=stale_sid,
                source="unknown",
                content="user said hello",
                message_type="external_input",
                message_id=uuid4().hex,
                timestamp=datetime.now(UTC),
                metadata={
                    "session_id": "e3834e2a8ee0",
                    "agent_session_id": stale_sid,
                    "source_kind": "channel",
                    "source_name": "unknown",
                },
            )
            cli_mq.deliver(stale_sid, message)

            bot_mq = SqliteInboxMQ(
                db_path=db_path,
                scope=_DefaultPoolScope(pool="default"),
                connection=manager,
            )
            consumer = InboxConsumer(server=bot_mq)
            producer = InboxProducer(server=bot_mq)
            bus = LocalAgentMessageBus(producer=producer, consumer=consumer)

            default_inst = MagicMock()
            default_inst.pipeline = MagicMock()
            default_inst.pipeline.process_message = AsyncMock()

            class _DefaultPool:
                def __init__(self):
                    self.session_registry = None
                    self._materialize_deps = MagicMock()
                    self._agents = {"default": default_inst}

                async def sessions_with_pending(self):
                    return await bus.sessions_with_pending()

                async def peek_inbox(self, sid, limit=1):
                    return await bus.peek(sid, limit=limit)

                async def consume_inbox(self, sid, *, only_types=None):
                    return await bus.consume(sid, limit=10, only_types=only_types)

                def get(self, name):
                    return self._agents.get(name)

                def get_template(self, name):
                    return None

                async def materialize_agent(self, sid, template, *, parent_session_id=None):
                    return default_inst

                async def dispatch_envelope(self, sid, instance, envelope):
                    if instance.pipeline is not None:
                        await instance.pipeline.process_message(envelope)

            pool = _DefaultPool()
            poller = InboxPoller(pool, interval=0.02)

            with caplog.at_level(logging.WARNING, logger="modex_agent.multi_agent.inbox_poller"):
                poller.start()
                await asyncio.sleep(0.3)
                await poller.stop()

            # ERROR logged (the "no template" message)
            error_count = sum(
                1 for r in caplog.records if "no template for orchestrator" in r.message
            )
            assert error_count == 1, f"error should be logged once; got {error_count}"

            # NO drain warning — message is NOT consumed
            drained = any("drained" in r.message and "orphan" in r.message for r in caplog.records)
            assert not drained, "orphan must NOT be drained (no silent drop)"

            # Message MUST still be pending (no data loss)
            pending = await bot_mq.sessions_with_pending()
            assert stale_sid in pending, (
                f"orphan message must stay pending (no data loss); pending: {pending}"
            )

            # default agent's pipeline must NOT have been called (wrong agent)
            assert not default_inst.pipeline.process_message.called
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_no_repeated_error_on_subsequent_ticks(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """After the first ERROR, subsequent ticks must NOT re-log — the
        session is in ``_orphan_logged`` and silently skipped."""
        db_path = tmp_path / ".modex" / "state.db"
        manager = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
        await manager.open()
        try:
            cli_mq = SqliteInboxMQ(
                db_path=db_path,
                scope=_DefaultPoolScope(pool="default"),
                connection=None,
            )
            stale_sid = "abc123.orchestrator"
            message = InboxMessage(
                session_id=stale_sid,
                source="unknown",
                content="hello",
                message_type="external_input",
                message_id=uuid4().hex,
                timestamp=datetime.now(UTC),
                metadata={"session_id": "abc123", "agent_session_id": stale_sid},
            )
            cli_mq.deliver(stale_sid, message)

            bot_mq = SqliteInboxMQ(
                db_path=db_path,
                scope=_DefaultPoolScope(pool="default"),
                connection=manager,
            )
            consumer = InboxConsumer(server=bot_mq)
            producer = InboxProducer(server=bot_mq)
            bus = LocalAgentMessageBus(producer=producer, consumer=consumer)

            class _Pool:
                def __init__(self):
                    self.session_registry = None
                    self._materialize_deps = MagicMock()
                    self._agents = {"default": MagicMock(pipeline=MagicMock())}

                async def sessions_with_pending(self):
                    return await bus.sessions_with_pending()

                async def peek_inbox(self, sid, limit=1):
                    return await bus.peek(sid, limit=limit)

                async def consume_inbox(self, sid, *, only_types=None):
                    return await bus.consume(sid, limit=10, only_types=only_types)

                def get(self, name):
                    return self._agents.get(name)

                def get_template(self, name):
                    return None

                async def materialize_agent(self, sid, template, *, parent_session_id=None):
                    return self._agents["default"]

                async def dispatch_envelope(self, sid, instance, envelope):
                    pass

            poller = InboxPoller(_Pool(), interval=0.02)
            with caplog.at_level(logging.ERROR, logger="modex_agent.multi_agent.inbox_poller"):
                poller.start()
                await asyncio.sleep(0.5)
                await poller.stop()

            error_count = sum(1 for r in caplog.records if "no template" in r.message)
            assert error_count == 1, (
                f"error must appear exactly once (not every tick); got {error_count}"
            )
        finally:
            await manager.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Fix 1: PoolRouter self-heals stale pool routing
# ═══════════════════════════════════════════════════════════════════════════════


class TestPoolRouterSelfHealsStaleRouting:
    """When a message's session carries an agent_name the routed pool doesn't
    serve, the router finds the owning pool and re-routes + self-heals."""

    @pytest.mark.asyncio
    async def test_message_for_coder_agent_rerouted_from_default(self, tmp_path: Path) -> None:
        """A message with session_id='conv1.orchestrator' (agent_name=orchestrator)
        routed to default pool → re-routed to coder pool (which serves orchestrator).

        The routing store is NOT self-healed — per ADR-0019 the store is the
        routing authority, maintained by the pool-switch write path. The router
        only corrects the per-message routing decision; fixing the store is the
        write site's responsibility.
        """
        routing_store = LocalFilePoolRoutingStore(tmp_path)
        routing_store.set_pool("conv1", "default")

        coder_pool = MagicMock()
        coder_pool.serves_agent = MagicMock(side_effect=lambda n: n == "orchestrator")

        default_pool = MagicMock()
        default_pool.serves_agent = MagicMock(side_effect=lambda n: n == "default")

        pools = {
            "default": PoolInstance(
                name="default",
                media=MagicMock(),
                subagent_count=0,
                pool=default_pool,
                broker_bridge=MagicMock(),
                tool_manager=MagicMock(),
                skill_manager=None,
                mcp_manager=None,
                terminal_manager=None,
                main_agent_name="default",
                main_execution_strategy=ExecutionStrategyKind.REACT,
                provider=MagicMock(),
                notification_service=MagicMock(),
                communication_service=MagicMock(),
                agent_bus=MagicMock(),
                target_store=MagicMock(),
            ),
            "coder": PoolInstance(
                name="coder",
                media=MagicMock(),
                subagent_count=0,
                pool=coder_pool,
                broker_bridge=MagicMock(),
                tool_manager=MagicMock(),
                skill_manager=None,
                mcp_manager=None,
                terminal_manager=None,
                main_agent_name="orchestrator",
                main_execution_strategy=ExecutionStrategyKind.REACT,
                provider=MagicMock(),
                notification_service=MagicMock(),
                communication_service=MagicMock(),
                agent_bus=MagicMock(),
                target_store=MagicMock(),
            ),
        }

        router = PoolRouter(
            input_adapter=MagicMock(),
            broker=MagicMock(),
            pools=pools,
            session_store=routing_store,
            default_pool="default",
        )

        routed_to: list[str] = []

        async def _fake_route_to_pool(msg, pool):
            routed_to.append(pool.name)

        router._route_to_pool = _fake_route_to_pool  # type: ignore[method-assign]
        msg = InputMessage(
            content="hello",
            session=SessionInfo(session_id="conv1.orchestrator", agent_name="orchestrator"),
            sender_id="user",
            chat_id="c",
        )
        await router.route_message(msg)

        assert routed_to == ["coder"], f"must re-route to coder; got {routed_to}"
        assert routing_store.get_pool("conv1") == "default", (
            "routing store must NOT be self-healed (authority stays at write site)"
        )

    @pytest.mark.asyncio
    async def test_message_for_default_agent_stays_on_default(self, tmp_path: Path) -> None:
        """No mismatch → no re-routing, no self-healing."""
        routing_store = LocalFilePoolRoutingStore(tmp_path)
        routing_store.set_pool("conv1", "default")

        default_pool = MagicMock()
        default_pool.serves_agent = MagicMock(side_effect=lambda n: n == "default")

        pools = {
            "default": PoolInstance(
                name="default",
                media=MagicMock(),
                subagent_count=0,
                pool=default_pool,
                broker_bridge=MagicMock(),
                tool_manager=MagicMock(),
                skill_manager=None,
                mcp_manager=None,
                terminal_manager=None,
                main_agent_name="default",
                main_execution_strategy=ExecutionStrategyKind.REACT,
                provider=MagicMock(),
                notification_service=MagicMock(),
                communication_service=MagicMock(),
                agent_bus=MagicMock(),
                target_store=MagicMock(),
            ),
        }

        router = PoolRouter(
            input_adapter=MagicMock(),
            broker=MagicMock(),
            pools=pools,
            session_store=routing_store,
            default_pool="default",
        )

        routed_to: list[str] = []

        async def _fake_route_to_pool(msg, pool):
            routed_to.append(pool.name)

        router._route_to_pool = _fake_route_to_pool  # type: ignore[method-assign]
        msg = InputMessage(
            content="hello",
            session=SessionInfo(session_id="conv1.default", agent_name="default"),
            sender_id="user",
            chat_id="c",
        )
        await router.route_message(msg)

        assert routed_to == ["default"]
        assert routing_store.get_pool("conv1") == "default"

    @pytest.mark.asyncio
    async def test_bare_prefix_session_trusts_routing_store(self, tmp_path: Path) -> None:
        """A session with no agent_name (bare prefix → agent_name='')
        routes to the stored pool without re-routing — empty agent_name
        matches no pool's agents so the reconciler leaves it on the
        stored pool."""
        routing_store = LocalFilePoolRoutingStore(tmp_path)
        routing_store.set_pool("conv1", "default")

        default_pool = MagicMock()
        default_pool.serves_agent = MagicMock(return_value=False)

        pools = {
            "default": PoolInstance(
                name="default",
                media=MagicMock(),
                subagent_count=0,
                pool=default_pool,
                broker_bridge=MagicMock(),
                tool_manager=MagicMock(),
                skill_manager=None,
                mcp_manager=None,
                terminal_manager=None,
                main_agent_name="default",
                main_execution_strategy=ExecutionStrategyKind.REACT,
                provider=MagicMock(),
                notification_service=MagicMock(),
                communication_service=MagicMock(),
                agent_bus=MagicMock(),
                target_store=MagicMock(),
            ),
        }

        router = PoolRouter(
            input_adapter=MagicMock(),
            broker=MagicMock(),
            pools=pools,
            session_store=routing_store,
            default_pool="default",
        )

        routed_to: list[str] = []

        async def _fake_route_to_pool(msg, pool):
            routed_to.append(pool.name)

        router._route_to_pool = _fake_route_to_pool  # type: ignore[method-assign]
        msg = InputMessage(
            content="hello",
            session=SessionInfo(session_id="conv1", agent_name=""),
            sender_id="user",
            chat_id="c",
        )
        await router.route_message(msg)

        assert routed_to == ["default"]


async def _route_to_pool(self, msg, pool):  # helper for the patched method above
    pass
