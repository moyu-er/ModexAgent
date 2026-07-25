"""parent_session_id propagation chain — env var → contextvar → modexctl → inbox → poller.

Verifies the FULL propagation chain:

1. ExternalEnvBuilder.build_modex_vars emits MODEX_PARENT_SESSION_ID ONLY
   when comm_kind=SUBAGENT (and parent_session_id is non-None).
2. NativeEnvInjectionHook reads ctx.session.parent_session_id and passes
   it through the ExternalEnvSpec template → build_modex_vars → contextvar.
3. SubprocessExecutor reads _modex_env contextvar → build_full_env → child
   process env contains MODEX_PARENT_SESSION_ID.
4. modexctl _send ParentReply path reads MODEX_PARENT_SESSION_ID and uses
   it verbatim as target_sid.
5. modexctl _send SubagentDispatch path writes parent_session_id into the
   InboxMessage metadata (so the bot poller can register the child with
   the correct parent).
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from modex_agent.agents.external_coding.env_builder import ExternalEnvBuilder
from modex_agent.agents.external_coding.types import ExternalEnvSpec
from modex_agent.core.agent import AgentCommKind, AgentContext
from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.session_registry import InMemorySessionRegistry
from modex_agent.hook.builtin.env_injection import NativeEnvInjectionHook
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.inbox.types import InboxMessage
from modex_agent.multi_agent.inbox_poller import InboxPoller
from modex_agent.multi_agent.message_type import AgentMessageType
from modex_agent.multi_agent.message_xml import build_dispatch_xml, build_peer_agent_message
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters.inbox_mq import SqliteInboxMQ
from modex_agent.runtime.env_context import _current_session_id, _modex_env
from modex_agent.tools.terminal.env import build_full_env

from modexctl.main import _PoolScopedRecordScope


def _make_env_spec(
    *,
    comm_kind: AgentCommKind = AgentCommKind.NORMAL,
    parent_session_id: str | None = None,
    agent_name: str = "orchestrator",
    session_id: str = "conv123.orchestrator",
) -> ExternalEnvSpec:
    return ExternalEnvSpec(
        workspace_root=Path("/tmp/ws"),
        inbox_root=Path("/tmp/ws/.modex/inbox"),
        workdir=Path("/tmp/ws"),
        session_id=session_id,
        agent_name=agent_name,
        provider_session_id="",
        agent_pool_map={agent_name: "coder"},
        targets=[],
        modexctl_bin_dir=Path("/tmp/bin"),
        comm_kind=comm_kind,
        parent_session_id=parent_session_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Step 1+2: env_builder emits MODEX_PARENT_SESSION_ID only for SUBAGENT
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnvBuilderParentSessionId:
    def test_subagent_kind_emits_parent_session_id(self) -> None:
        spec = _make_env_spec(
            comm_kind=AgentCommKind.SUBAGENT,
            parent_session_id="conv456.orchestrator",
            agent_name="coder",
            session_id="inv1.coder",
        )
        vars_dict = ExternalEnvBuilder.build_modex_vars(spec)
        assert vars_dict.get("MODEX_PARENT_SESSION_ID") == "conv456.orchestrator"
        assert vars_dict["MODEX_COMM_KIND"] == "subagent"

    def test_normal_kind_omits_parent_session_id(self) -> None:
        """NORMAL (main agent peer send) must NOT emit MODEX_PARENT_SESSION_ID.

        modexctl's PeerNormal path uses ADR-0019 prefix-reuse to derive the
        target_sid — it should not be influenced by parent_session_id.
        """
        spec = _make_env_spec(
            comm_kind=AgentCommKind.NORMAL,
            parent_session_id="should-not-appear",  # even if set, NORMAL ignores
            agent_name="orchestrator",
            session_id="conv123.orchestrator",
        )
        vars_dict = ExternalEnvBuilder.build_modex_vars(spec)
        assert "MODEX_PARENT_SESSION_ID" not in vars_dict
        assert vars_dict["MODEX_COMM_KIND"] == "normal"

    def test_subagent_kind_without_parent_session_id_omits_key(self) -> None:
        spec = _make_env_spec(
            comm_kind=AgentCommKind.SUBAGENT,
            parent_session_id=None,
            agent_name="coder",
            session_id="inv1.coder",
        )
        vars_dict = ExternalEnvBuilder.build_modex_vars(spec)
        assert "MODEX_PARENT_SESSION_ID" not in vars_dict


# ═══════════════════════════════════════════════════════════════════════════════
# Step 2: NativeEnvInjectionHook propagates parent_session_id via contextvar
# ═══════════════════════════════════════════════════════════════════════════════


class TestNativeEnvInjectionHookParentPropagation:
    """NativeEnvInjectionHook reads ctx.session.parent_session_id and threads
    it through ExternalEnvSpec → build_modex_vars → _modex_env contextvar."""

    @pytest.mark.asyncio
    async def test_subagent_session_propagates_parent_to_contextvar(self) -> None:
        """A subagent's AgentContext carries session.parent_session_id.
        The hook must propagate it into the _modex_env contextvar as
        MODEX_PARENT_SESSION_ID so SubprocessExecutor injects it into
        child process env.
        """
        # Reset contextvar
        _modex_env.set(None)
        _current_session_id.set(None)

        template = _make_env_spec(
            comm_kind=AgentCommKind.SUBAGENT,
            parent_session_id=None,  # template's parent; ctx overrides
            agent_name="coder",
            session_id="__pending__.coder",
        )
        hook = NativeEnvInjectionHook(env_spec_template=template)

        # Simulate a subagent AgentContext with a real parent link
        ctx = MagicMock(spec=AgentContext)
        ctx.session = SessionInfo(
            session_id="inv1.coder",
            agent_name="coder",
            parent_session_id="conv456.orchestrator",
        )
        ctx.comm_kind = AgentCommKind.SUBAGENT

        await hook.before_turn(ctx)

        modex_env = _modex_env.get()
        assert modex_env is not None
        assert modex_env.get("MODEX_PARENT_SESSION_ID") == "conv456.orchestrator"
        assert modex_env.get("MODEX_COMM_KIND") == "subagent"
        assert modex_env.get("MODEX_SESSION_ID") == "inv1.coder"
        assert _current_session_id.get() == "inv1.coder"

    @pytest.mark.asyncio
    async def test_normal_session_omits_parent_in_contextvar(self) -> None:
        _modex_env.set(None)
        _current_session_id.set(None)

        template = _make_env_spec(
            comm_kind=AgentCommKind.NORMAL,
            agent_name="orchestrator",
            session_id="__pending__.orchestrator",
        )
        hook = NativeEnvInjectionHook(env_spec_template=template)

        ctx = MagicMock(spec=AgentContext)
        ctx.session = SessionInfo(
            session_id="conv123.orchestrator",
            agent_name="orchestrator",
            parent_session_id=None,  # main agents have no parent
        )
        ctx.comm_kind = AgentCommKind.NORMAL

        await hook.before_turn(ctx)

        modex_env = _modex_env.get()
        assert modex_env is not None
        assert "MODEX_PARENT_SESSION_ID" not in modex_env
        assert modex_env["MODEX_COMM_KIND"] == "normal"


# ═══════════════════════════════════════════════════════════════════════════════
# Step 3: SubprocessExecutor reads _modex_env → child env contains the var
# ═══════════════════════════════════════════════════════════════════════════════


class TestSubprocessEnvContainsParentSessionId:
    """build_full_env(overrides=_modex_env.get()) produces the child process
    env. If _modex_env contains MODEX_PARENT_SESSION_ID, the child sees it."""

    @pytest.mark.asyncio
    async def test_subagent_env_reaches_subprocess(self) -> None:
        _modex_env.set(None)
        template = _make_env_spec(
            comm_kind=AgentCommKind.SUBAGENT,
            agent_name="coder",
            session_id="__pending__.coder",
        )
        hook = NativeEnvInjectionHook(env_spec_template=template)
        ctx = MagicMock(spec=AgentContext)
        ctx.session = SessionInfo(
            session_id="inv1.coder",
            agent_name="coder",
            parent_session_id="conv456.orchestrator",
        )
        ctx.comm_kind = AgentCommKind.SUBAGENT
        await hook.before_turn(ctx)

        # SubprocessExecutor does: build_full_env(overrides=_modex_env.get())
        child_env = build_full_env(overrides=_modex_env.get())
        assert child_env.get("MODEX_PARENT_SESSION_ID") == "conv456.orchestrator"
        assert child_env.get("MODEX_COMM_KIND") == "subagent"
        assert child_env.get("MODEX_SESSION_ID") == "inv1.coder"


# ═══════════════════════════════════════════════════════════════════════════════
# Step 4+5: modexctl _send reads MODEX_PARENT_SESSION_ID and writes it into
# the InboxMessage metadata; the bot poller registers the child with the parent
# ═══════════════════════════════════════════════════════════════════════════════


class TestModexctlParentReplyPath:
    """modexctl _send with comm_kind=subagent: ParentReply path.

    The subagent's reply goes to MODEX_PARENT_SESSION_ID verbatim.

    NOTE: these tests are synchronous (no @pytest.mark.asyncio) because
    CliRunner.invoke runs the CLI synchronously. modexctl's _ensure_inbox_db
    calls asyncio.run() internally, which would fail if a pytest-asyncio
    event loop were already running.
    """

    def test_subagent_reply_lands_on_parent_session(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from modexctl.main import build_app

        inbox_root = tmp_path / "ws" / ".modex" / "inbox"
        runner = CliRunner()

        # Set up env as a subagent would see it
        env = {
            "MODEX_SESSION_ID": "inv1.coder",
            "MODEX_AGENT_NAME": "coder",
            "MODEX_INBOX_ROOT": str(inbox_root),
            "MODEX_AGENT_POOL_MAP": "coder=coder;orchestrator=coder",
            "MODEX_TARGETS": "orchestrator=",
            "MODEX_COMM_KIND": "subagent",
            "MODEX_PARENT_SESSION_ID": "conv456.orchestrator",
        }
        old_env = dict(os.environ)
        os.environ.clear()
        os.environ.update(env)
        try:
            result = runner.invoke(
                build_app(),
                ["send", "--to", "orchestrator", "--content", "task done"],
            )
        finally:
            os.environ.clear()
            os.environ.update(old_env)

        assert result.exit_code == 0

        # The message must land on the parent session (conv456.orchestrator)
        db_path = tmp_path / "ws" / ".modex" / "state.db"
        import sqlite3
        import json

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT session_id, payload_json FROM inbox_messages",
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == "conv456.orchestrator"
        payload = json.loads(row[1])
        # ParentReply metadata carries the subagent's session as session_id
        assert payload["metadata"]["session_id"] == "inv1.coder"


class TestModexctlSubagentDispatchWritesParentMetadata:
    """modexctl _send same-pool SubagentDispatch path writes parent_session_id
    into the InboxMessage metadata so the bot poller can register the child
    session with the correct parent."""

    def test_subagent_dispatch_metadata_carries_parent(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from modexctl.main import build_app

        inbox_root = tmp_path / "ws" / ".modex" / "inbox"
        runner = CliRunner()

        env = {
            "MODEX_SESSION_ID": "conv1.orchestrator",
            "MODEX_AGENT_NAME": "orchestrator",
            "MODEX_INBOX_ROOT": str(inbox_root),
            "MODEX_AGENT_POOL_MAP": "orchestrator=coder;coder=coder",
            "MODEX_TARGETS": "coder=Code subagent",
            "MODEX_COMM_KIND": "normal",
        }
        old_env = dict(os.environ)
        os.environ.clear()
        os.environ.update(env)
        try:
            result = runner.invoke(
                build_app(),
                ["send", "--to", "coder", "--content", "do this"],
            )
        finally:
            os.environ.clear()
            os.environ.update(old_env)

        assert result.exit_code == 0

        db_path = tmp_path / "ws" / ".modex" / "state.db"
        import sqlite3
        import json

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT session_id, message_type, payload_json FROM inbox_messages",
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        session_id, msg_type, payload_json = row
        assert msg_type == "task_request"
        payload = json.loads(payload_json)
        # parent_session_id must be the sender's session (the main agent)
        assert payload["metadata"]["parent_session_id"] == "conv1.orchestrator"
        # target session is minted as {invocation_id}.coder
        assert session_id.endswith(".coder")
        assert session_id != "conv1.coder"  # NOT prefix-reuse; fresh invocation_id


# ═══════════════════════════════════════════════════════════════════════════════
# parent_session_id must survive the SQLite cross-process round-trip
# ═══════════════════════════════════════════════════════════════════════════════


class TestParentSessionIdSqliteRoundTrip:
    """modexctl SubagentDispatch → SQLite deliver → bot peek → envelope.parent_session_id.

    The bot's AgentMessageBus._reconstruct extracts parent_session_id from
    InboxMessage.metadata. This test verifies the metadata survives the
    CLI (stdlib sqlite3) → bot (aiosqlite) round-trip.
    """

    @pytest.mark.asyncio
    async def test_parent_session_id_preserved_through_sqlite_round_trip(
        self, tmp_path: Path
    ) -> None:
        from modex_agent.multi_agent.bus import LocalAgentMessageBus
        from modex_agent.multi_agent.message_xml import build_dispatch_xml

        db_path = tmp_path / ".modex" / "state.db"
        manager = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
        await manager.open()
        try:
            cli_mq = SqliteInboxMQ(
                db_path=db_path,
                scope=_PoolScopedRecordScope(pool="coder"),
                connection=None,
            )
            main_session = "conv123.orchestrator"
            invocation_id = uuid4().hex[:8]
            target_sid = f"{invocation_id}.coder"
            xml_content = build_dispatch_xml(
                source="orchestrator",
                invocation_id=invocation_id,
                content="do this task",
                target_execution_strategy=ExecutionStrategyKind.REACT,
            )
            message = InboxMessage(
                session_id=target_sid,
                source="orchestrator",
                content=xml_content,
                message_type=AgentMessageType.TASK_REQUEST.value,
                message_id=uuid4().hex,
                timestamp=datetime.now(UTC),
                metadata={
                    "agent_session_id": target_sid,
                    "session_id": main_session,
                    "invocation_id": invocation_id,
                    "parent_session_id": main_session,
                },
            )
            assert cli_mq.deliver(target_sid, message), "deliver must succeed"

            bot_mq = SqliteInboxMQ(
                db_path=db_path,
                scope=_PoolScopedRecordScope(pool="coder"),
                connection=manager,
            )
            peeked = await bot_mq.peek(target_sid)
            assert len(peeked) == 1
            assert peeked[0].metadata.get("parent_session_id") == main_session

            envelope = LocalAgentMessageBus._reconstruct(peeked[0], target_sid)
            assert envelope.parent_session_id == main_session
        finally:
            await manager.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Poller must register the session with parent in ONE step (no parentless window)
# ═══════════════════════════════════════════════════════════════════════════════


class TestPollerRegistersParentInOneStep:
    """_materialize_then_turn peeks the inbox BEFORE registering so the session
    is registered with the correct parent_session_id in one atomic step.

    The old code registered without parent first, then re-registered with
    parent — a race where the WebUI could read a parentless session between
    the two steps, making a subagent appear as a main agent.
    """

    @pytest.mark.asyncio
    async def test_first_registration_includes_parent(self, tmp_path: Path) -> None:
        from modex_agent.core.session_registry import InMemorySessionRegistry
        from modex_agent.multi_agent.bus import LocalAgentMessageBus
        from modex_agent.multi_agent.inbox.consumer import InboxConsumer
        from modex_agent.multi_agent.inbox.producer import InboxProducer
        from modex_agent.multi_agent.inbox_poller import InboxPoller
        from modex_agent.multi_agent.message_xml import build_dispatch_xml

        db_path = tmp_path / ".modex" / "state.db"
        manager = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
        await manager.open()
        try:
            cli_mq = SqliteInboxMQ(
                db_path=db_path,
                scope=_PoolScopedRecordScope(pool="coder"),
                connection=None,
            )
            main_session = "conv123.orchestrator"
            invocation_id = uuid4().hex[:8]
            target_sid = f"{invocation_id}.coder"
            message = InboxMessage(
                session_id=target_sid,
                source="orchestrator",
                content=build_dispatch_xml(
                    source="orchestrator",
                    invocation_id=invocation_id,
                    content="do this task",
                    target_execution_strategy=ExecutionStrategyKind.REACT,
                ),
                message_type=AgentMessageType.TASK_REQUEST.value,
                message_id=uuid4().hex,
                timestamp=datetime.now(UTC),
                metadata={
                    "agent_session_id": target_sid,
                    "session_id": main_session,
                    "invocation_id": invocation_id,
                    "parent_session_id": main_session,
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
            registry = InMemorySessionRegistry()

            register_calls: list[SessionInfo] = []
            original_register = registry.register

            async def _tracking_register(session: SessionInfo) -> None:
                register_calls.append(session.model_copy())
                await original_register(session)

            registry.register = _tracking_register  # type: ignore[method-assign]

            materialized = {"done": False}

            class _MockTemplate:
                async def materialize(self, parent, inv, deps):
                    inst = MagicMock()
                    inst.pipeline = MagicMock()
                    inst.pipeline.process_message = AsyncMock()
                    materialized["done"] = True
                    return inst

            class _Pool:
                def __init__(self):
                    self.session_registry = registry
                    self._materialize_deps = MagicMock()

                async def sessions_with_pending(self):
                    return await bus.sessions_with_pending()

                async def peek_inbox(self, sid, limit=1):
                    return await bus.peek(sid, limit=limit)

                async def consume_inbox(self, sid, *, only_types=None):
                    return await bus.consume(sid, limit=10, only_types=only_types)

                def get(self, name):
                    return None

                def get_template(self, name):
                    return _MockTemplate() if name == "coder" else None

                async def materialize_agent(self, sid, template, *, parent_session_id=None):
                    parent = SessionInfo.from_str(parent_session_id) if parent_session_id else None
                    inv = sid.split(".")[0]
                    return await template.materialize(parent, inv, self._materialize_deps)

                async def dispatch_envelope(self, sid, instance, envelope):
                    if instance.pipeline is not None:
                        await instance.pipeline.process_message(envelope)

            pool = _Pool()
            poller = InboxPoller(pool, interval=0.02)
            poller.start()
            await asyncio.sleep(0.3)
            await poller.stop()

            assert materialized["done"]
            target_regs = [r for r in register_calls if r.session_id == target_sid]
            assert len(target_regs) >= 1
            assert target_regs[0].parent_session_id == main_session, (
                f"FIRST registration must include parent; "
                f"got {target_regs[0].parent_session_id!r}"
            )
        finally:
            await manager.close()
