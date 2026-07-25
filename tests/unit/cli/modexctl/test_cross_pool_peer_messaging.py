"""modexctl cross-pool peer messaging — PeerNormal path + poller startup ordering.

Verifies:
- default pool's main agent sends to coder pool's orchestrator via modexctl
- the message lands on coder pool's InboxMQ (cross-pool scope_key isolation)
- the bot's coder pool poller retrieves and dispatches it
- reply contract in the XML instructs the receiver how to reply
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from modex_agent.core.session_registry import InMemorySessionRegistry
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.inbox.types import InboxMessage
from modex_agent.multi_agent.inbox_poller import InboxPoller
from modex_agent.multi_agent.message_type import AgentMessageType
from modex_agent.multi_agent.message_xml import build_peer_agent_message
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters.inbox_mq import SqliteInboxMQ

from modexctl.main import _PoolScopedRecordScope, build_app


def _run_modexctl_send(env: dict[str, str], args: list[str]) -> int:
    """Invoke modexctl send with a fully replaced environment (synchronous)."""
    old_env = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    try:
        result = CliRunner().invoke(build_app(), args)
        return result.exit_code
    finally:
        os.environ.clear()
        os.environ.update(old_env)


async def _run_modexctl_send_async(env: dict[str, str], args: list[str]) -> int:
    """Invoke modexctl send from within an async test.

    CliRunner.invoke is synchronous and modexctl's _ensure_inbox_db calls
    asyncio.run() internally — which fails if pytest-asyncio's event loop
    is already running. Running the CLI in a thread gives it a clean
    event-loop-free context.
    """
    import asyncio

    return await asyncio.to_thread(_run_modexctl_send, env, args)


def _query_inbox(db_path: Path, session_id: str | None = None) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    try:
        if session_id is None:
            rows = conn.execute(
                "SELECT session_id, message_type, payload_json FROM inbox_messages"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT session_id, message_type, payload_json FROM inbox_messages "
                "WHERE session_id = ?",
                (session_id,),
            ).fetchall()
    finally:
        conn.close()
    return [{"session_id": r[0], "message_type": r[1], "payload": json.loads(r[2])} for r in rows]


class TestQ3PeerNormalCrossPool:
    """default pool's main agent → coder pool's orchestrator (cross-pool peer)."""

    def test_peer_message_lands_on_target_pool_inbox(self, tmp_path: Path) -> None:
        inbox_root = tmp_path / "ws" / ".modex" / "inbox"
        env = {
            "MODEX_SESSION_ID": "conv1.default",
            "MODEX_AGENT_NAME": "default",
            "MODEX_INBOX_ROOT": str(inbox_root),
            "MODEX_AGENT_POOL_MAP": "default=default;orchestrator=coder",
            "MODEX_TARGETS": "orchestrator=Code orchestrator",
            "MODEX_COMM_KIND": "normal",
        }
        rc = _run_modexctl_send(env, ["send", "--to", "orchestrator", "--content", "review this"])
        assert rc == 0

        db_path = tmp_path / "ws" / ".modex" / "state.db"
        rows = _query_inbox(db_path)
        assert len(rows) == 1
        row = rows[0]
        # PeerNormal: prefix-reuse → target_sid = conv1.orchestrator
        assert row["session_id"] == "conv1.orchestrator"
        assert row["message_type"] == "agent_message"
        # The XML must carry <reply_contract> instructing how to reply
        content = row["payload"]["content"]
        assert "<agent_message" in content
        assert "<reply_contract>" in content
        # parent_session_id must be None for peer sends (peers are equals)
        assert row["payload"]["metadata"]["parent_session_id"] is None

    def test_peer_message_scope_isolated_from_sender_pool(self, tmp_path: Path) -> None:
        """The message lands on coder pool's scope, NOT default pool's scope."""
        inbox_root = tmp_path / "ws" / ".modex" / "inbox"
        env = {
            "MODEX_SESSION_ID": "conv1.default",
            "MODEX_AGENT_NAME": "default",
            "MODEX_INBOX_ROOT": str(inbox_root),
            "MODEX_AGENT_POOL_MAP": "default=default;orchestrator=coder",
            "MODEX_TARGETS": "orchestrator=",
            "MODEX_COMM_KIND": "normal",
        }
        _run_modexctl_send(env, ["send", "--to", "orchestrator", "--content", "hi"])

        db_path = tmp_path / "ws" / ".modex" / "state.db"
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT owner_scope_key FROM inbox_messages"
            ).fetchone()
        finally:
            conn.close()
        owner = row[0]
        # owner_scope_key must be coder pool's scope, not default's
        assert '"pool": "coder"' in owner or "pool" in owner

    @pytest.mark.asyncio
    async def test_peer_message_retrievable_by_target_pool_poller(self, tmp_path: Path) -> None:
        """End-to-end: modexctl deliver → bot coder pool poller retrieves it.

        CLI invocation is synchronous (no event loop); the async verification
        runs after in a fresh event loop.
        """
        inbox_root = tmp_path / "ws" / ".modex" / "inbox"
        env = {
            "MODEX_SESSION_ID": "conv1.default",
            "MODEX_AGENT_NAME": "default",
            "MODEX_INBOX_ROOT": str(inbox_root),
            "MODEX_AGENT_POOL_MAP": "default=default;orchestrator=coder",
            "MODEX_TARGETS": "orchestrator=",
            "MODEX_COMM_KIND": "normal",
        }
        # Synchronous CLI invocation (modexctl _ensure_inbox_db uses asyncio.run
        # internally — must not be inside a pytest-asyncio event loop).
        rc = await _run_modexctl_send_async(env, ["send", "--to", "orchestrator", "--content", "hello peer"])
        assert rc == 0

        db_path = tmp_path / "ws" / ".modex" / "state.db"
        manager = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
        await manager.open()
        try:
            bot_mq = SqliteInboxMQ(
                db_path=db_path,
                scope=_PoolScopedRecordScope(pool="coder"),
                connection=manager,
            )
            pending = await bot_mq.sessions_with_pending()
            assert "conv1.orchestrator" in pending

            peeked = await bot_mq.peek("conv1.orchestrator")
            assert len(peeked) == 1
            assert "hello peer" in peeked[0].content
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_peer_message_dispatched_to_registered_main_agent(
        self, tmp_path: Path
    ) -> None:
        """Full dispatch: modexctl → poller → main agent pipeline.process_message.

        CLI invocation is synchronous; async dispatch runs after.
        """
        inbox_root = tmp_path / "ws" / ".modex" / "inbox"
        env = {
            "MODEX_SESSION_ID": "conv1.default",
            "MODEX_AGENT_NAME": "default",
            "MODEX_INBOX_ROOT": str(inbox_root),
            "MODEX_AGENT_POOL_MAP": "default=default;orchestrator=coder",
            "MODEX_TARGETS": "orchestrator=",
            "MODEX_COMM_KIND": "normal",
        }
        rc = await _run_modexctl_send_async(env, ["send", "--to", "orchestrator", "--content", "dispatch me"])
        assert rc == 0

        db_path = tmp_path / "ws" / ".modex" / "state.db"
        manager = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
        await manager.open()
        try:
            bot_mq = SqliteInboxMQ(
                db_path=db_path,
                scope=_PoolScopedRecordScope(pool="coder"),
                connection=manager,
            )
            consumer = InboxConsumer(server=bot_mq)
            producer = InboxProducer(server=bot_mq)
            bus = LocalAgentMessageBus(producer=producer, consumer=consumer)

            main_inst = MagicMock()
            main_inst.pipeline = MagicMock()
            main_inst.pipeline.process_message = AsyncMock()
            dispatched: list[str] = []

            class _Pool:
                def __init__(self):
                    self.session_registry = None
                    self._materialize_deps = MagicMock()
                    self._agents = {"orchestrator": main_inst}

                async def sessions_with_pending(self):
                    return await bus.sessions_with_pending()

                async def consume_inbox(self, sid, *, only_types=None):
                    return await bus.consume(sid, limit=10, only_types=only_types)

                async def peek_inbox(self, sid, limit=1):
                    return await bus.peek(sid, limit=limit)

                def get(self, name):
                    return self._agents.get(name)

                def get_template(self, name):
                    return None

                async def materialize_agent(self, sid, template, *, parent_session_id=None):
                    return main_inst

                async def dispatch_envelope(self, sid, instance, envelope):
                    dispatched.append(sid)
                    if instance.pipeline is not None:
                        await instance.pipeline.process_message(envelope)

            pool = _Pool()
            poller = InboxPoller(pool, interval=0.02)
            poller.start()
            await asyncio.sleep(0.3)
            await poller.stop()

            assert len(dispatched) == 1
            assert dispatched[0] == "conv1.orchestrator"
            assert main_inst.pipeline.process_message.called
        finally:
            await manager.close()


class TestQ3SelfSendRejected:
    """modexctl send --to <self> must be rejected (avoid useless round-trip)."""

    def test_self_send_errors(self, tmp_path: Path) -> None:
        inbox_root = tmp_path / "ws" / ".modex" / "inbox"
        env = {
            "MODEX_SESSION_ID": "conv1.orchestrator",
            "MODEX_AGENT_NAME": "orchestrator",
            "MODEX_INBOX_ROOT": str(inbox_root),
            "MODEX_AGENT_POOL_MAP": "orchestrator=coder;coder=coder",
            "MODEX_TARGETS": "coder=",
            "MODEX_COMM_KIND": "normal",
        }
        rc = _run_modexctl_send(env, ["send", "--to", "orchestrator", "--content", "self"])
        assert rc != 0


# ═══════════════════════════════════════════════════════════════════════════════
# Poller must start AFTER main agent registration (no "no template" race)
# ═══════════════════════════════════════════════════════════════════════════════


class TestPollerStartsAfterMainAgentRegistration:
    """pool.start_poller() is called AFTER _register_main_agent() in
    pool_builder.create_pool. If a pending peer message from a previous bot
    run exists in the inbox, the poller must find the registered main agent
    on its first tick — not fall through to get_template and log
    "no template for X; skipping".
    """

    @pytest.mark.asyncio
    async def test_pending_message_dispatched_when_main_registered_before_start(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        db_path = tmp_path / ".modex" / "state.db"
        manager = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
        await manager.open()
        try:
            # Pre-seed: a peer message arrives BEFORE the bot starts
            cli_mq = SqliteInboxMQ(
                db_path=db_path,
                scope=_PoolScopedRecordScope(pool="coder"),
                connection=None,
            )
            target_sid = "conv456.orchestrator"
            message = InboxMessage(
                session_id=target_sid,
                source="default",
                content=build_peer_agent_message(source="default", content="hello"),
                message_type=AgentMessageType.AGENT_MESSAGE.value,
                message_id=uuid4().hex,
                timestamp=datetime.now(UTC),
                metadata={
                    "agent_session_id": target_sid,
                    "session_id": "conv456.default",
                    "invocation_id": "conv456",
                    "parent_session_id": None,
                },
            )
            cli_mq.deliver(target_sid, message)

            bot_mq = SqliteInboxMQ(
                db_path=db_path,
                scope=_PoolScopedRecordScope(pool="coder"),
                connection=manager,
            )
            consumer = InboxConsumer(server=bot_mq)
            producer = InboxProducer(server=bot_mq)
            bus = LocalAgentMessageBus(producer=producer, consumer=consumer)

            main_instance = MagicMock()
            main_instance.pipeline = MagicMock()
            main_instance.pipeline.process_message = AsyncMock()
            dispatched: list[str] = []

            class _Pool:
                def __init__(self):
                    self.session_registry = None
                    self._materialize_deps = MagicMock()
                    self._agents: dict[str, MagicMock] = {}

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
                    return main_instance

                async def dispatch_envelope(self, sid, instance, envelope):
                    dispatched.append(sid)
                    if instance.pipeline is not None:
                        await instance.pipeline.process_message(envelope)

            pool = _Pool()
            poller = InboxPoller(pool, interval=0.02)

            with caplog.at_level(logging.ERROR, logger="modex_agent.multi_agent.inbox_poller"):
                # Fixed ordering: register main agent FIRST, then start poller
                pool._agents["orchestrator"] = main_instance
                poller.start()
                await asyncio.sleep(0.3)
                await poller.stop()

            error_msgs = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
            assert not any("no template for orchestrator" in m for m in error_msgs), (
                f"poller must not log 'no template'; errors: {error_msgs}"
            )
            assert len(dispatched) == 1
            assert dispatched[0] == target_sid
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_old_ordering_causes_no_template_error(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Documents the old bug: starting the poller BEFORE registering the
        main agent causes 'no template for X; skipping' on the first tick."""
        import logging

        db_path = tmp_path / ".modex" / "state.db"
        manager = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
        await manager.open()
        try:
            cli_mq = SqliteInboxMQ(
                db_path=db_path,
                scope=_PoolScopedRecordScope(pool="coder"),
                connection=None,
            )
            target_sid = "conv456.orchestrator"
            message = InboxMessage(
                session_id=target_sid,
                source="default",
                content=build_peer_agent_message(source="default", content="hello"),
                message_type=AgentMessageType.AGENT_MESSAGE.value,
                message_id=uuid4().hex,
                timestamp=datetime.now(UTC),
                metadata={
                    "agent_session_id": target_sid,
                    "session_id": "conv456.default",
                    "invocation_id": "conv456",
                    "parent_session_id": None,
                },
            )
            cli_mq.deliver(target_sid, message)

            bot_mq = SqliteInboxMQ(
                db_path=db_path,
                scope=_PoolScopedRecordScope(pool="coder"),
                connection=manager,
            )
            consumer = InboxConsumer(server=bot_mq)
            producer = InboxProducer(server=bot_mq)
            bus = LocalAgentMessageBus(producer=producer, consumer=consumer)

            main_instance = MagicMock()
            main_instance.pipeline = MagicMock()
            main_instance.pipeline.process_message = AsyncMock()

            class _Pool:
                def __init__(self):
                    self.session_registry = None
                    self._materialize_deps = MagicMock()
                    self._agents: dict[str, MagicMock] = {}

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
                    return main_instance

                async def dispatch_envelope(self, sid, instance, envelope):
                    if instance.pipeline is not None:
                        await instance.pipeline.process_message(envelope)

            pool = _Pool()
            poller = InboxPoller(pool, interval=0.02)

            with caplog.at_level(logging.ERROR, logger="modex_agent.multi_agent.inbox_poller"):
                # OLD ordering: start poller FIRST, then register main agent
                poller.start()
                await asyncio.sleep(0.15)
                pool._agents["orchestrator"] = main_instance
                await asyncio.sleep(0.15)
                await poller.stop()

            error_msgs = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
            assert any("no template for orchestrator" in m for m in error_msgs), (
                "With OLD ordering the poller should log 'no template'. "
                f"Errors: {error_msgs}"
            )
        finally:
            await manager.close()
