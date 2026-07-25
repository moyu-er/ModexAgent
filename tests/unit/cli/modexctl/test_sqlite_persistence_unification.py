"""SQLite persistence unification — bot_project and modexctl share state.db.

Verifies:
- PersistenceConfig default backend is SQLITE (so bot_project gets SQLite out of box)
- modexctl uses SqliteInboxMQ.deliver (sync stdlib sqlite3, same state.db)
- Bot side uses SqliteInboxMQ (async, ConnectionManager, same state.db)
- Both produce identical scope_key via content-based stamping (ADR-0028)
  so cross-process SQL WHERE clauses match
- BotRecordScope(pool=X).canonical() == _PoolScopedRecordScope(pool=X).canonical()
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.core.agent import AgentCommKind
from modex_agent.core.scope import RecordScope
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters.inbox_mq import SqliteInboxMQ
from modex_agent.persistence.config import PersistenceBackend, PersistenceConfig

from modexctl.main import _PoolScopedRecordScope


class _BotRecordScope(RecordScope):
    """Mirrors examples/bot_project/bot/scope.py BotRecordScope.

    Registered via __init_subclass__ content-based stamping (ADR-0028 §3):
    two subclasses with the same extra-field signature (``pool``) produce
    identical canonical JSON regardless of class name or module.
    """

    pool: str | None = None


class TestQ1PersistenceBackend:
    def test_default_backend_is_sqlite(self) -> None:
        assert PersistenceConfig().backend is PersistenceBackend.SQLITE

    def test_persistence_config_is_frozen(self) -> None:
        cfg = PersistenceConfig()
        with pytest.raises(Exception):
            cfg.backend = PersistenceBackend.FILE  # type: ignore[misc]

    def test_file_backend_opt_in_via_string(self) -> None:
        cfg = PersistenceConfig.model_validate({"backend": "file"})
        assert cfg.backend is PersistenceBackend.FILE

    def test_sqlite_backend_via_string(self) -> None:
        cfg = PersistenceConfig.model_validate({"backend": "sqlite"})
        assert cfg.backend is PersistenceBackend.SQLITE


class TestQ1ScopeCanonicalMatch:
    """Cross-process scope_key matching — ADR-0028 content-based stamping."""

    @pytest.mark.parametrize("pool", ["default", "coder", "opencode", "main"])
    def test_owner_scope_keys_match_cross_process(self, pool: str) -> None:
        """BotRecordScope (bot side) and _PoolScopedRecordScope (modexctl CLI)
        must produce IDENTICAL canonical JSON for the same pool name.

        This is the SQL WHERE clause invariant: modexctl writes with
        owner_scope_key=X; the bot's InboxMQ reads with owner_scope_key=X.
        If they diverge, the bot's sessions_with_pending() never sees the
        CLI-delivered message.
        """
        bot_canonical = _BotRecordScope(pool=pool).canonical()
        cli_canonical = _PoolScopedRecordScope(pool=pool).canonical()
        assert bot_canonical == cli_canonical, (
            f"scope keys must match for pool={pool!r};\n"
            f"bot: {bot_canonical}\ncli: {cli_canonical}"
        )

    @pytest.mark.parametrize("pool", ["default", "coder"])
    def test_session_scoped_keys_match(self, pool: str) -> None:
        """Per-session scope_key (owner+session_id) must also match."""
        sid = "conv123.orchestrator"
        bot_session_key = _BotRecordScope(pool=pool, session_id=sid).canonical()
        cli_session_key = _PoolScopedRecordScope(pool=pool, session_id=sid).canonical()
        assert bot_session_key == cli_session_key

    def test_scope_keys_differ_across_pools(self) -> None:
        """Different pools must produce different scope_keys (isolation)."""
        assert _PoolScopedRecordScope(pool="coder").canonical() != (
            _PoolScopedRecordScope(pool="default").canonical()
        )


class TestQ1SqliteInboxMQSharedStateDb:
    """modexctl deliver (stdlib sqlite3) and bot consume (aiosqlite) share
    the same state.db file and the same schema."""

    @pytest.mark.asyncio
    async def test_cli_deliver_bot_consume_round_trip(self, tmp_path: Path) -> None:
        """A message written by modexctl's SqliteInboxMQ.deliver (sync, stdlib
        sqlite3, connection=None) must be readable by the bot's
        SqliteInboxMQ.peek (async, ConnectionManager)."""
        from datetime import UTC, datetime
        from uuid import uuid4

        from modex_agent.multi_agent.inbox.types import InboxMessage

        db_path = tmp_path / ".modex" / "state.db"
        manager = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
        await manager.open()
        try:
            # CLI side: connection=None forces stdlib sqlite3 path
            cli_mq = SqliteInboxMQ(
                db_path=db_path,
                scope=_PoolScopedRecordScope(pool="coder"),
                connection=None,
            )
            session_id = "conv1.coder"
            message = InboxMessage(
                session_id=session_id,
                source="default",
                content="<agent_message>hello</agent_message>",
                message_type="agent_message",
                message_id=uuid4().hex,
                timestamp=datetime.now(UTC),
                metadata={"session_id": "conv1.default", "parent_session_id": None},
            )
            assert cli_mq.deliver(session_id, message), "CLI deliver must succeed"

            # Bot side: same db, same scope, ConnectionManager
            bot_mq = SqliteInboxMQ(
                db_path=db_path,
                scope=_PoolScopedRecordScope(pool="coder"),
                connection=manager,
            )
            pending = await bot_mq.sessions_with_pending()
            assert session_id in pending

            peeked = await bot_mq.peek(session_id)
            assert len(peeked) == 1
            assert peeked[0].content == "<agent_message>hello</agent_message>"
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_pool_isolation_via_scope_key(self, tmp_path: Path) -> None:
        """A message written to pool='coder' must NOT be visible to a
        SqliteInboxMQ scoped for pool='default' — even on the same db."""
        from datetime import UTC, datetime
        from uuid import uuid4

        from modex_agent.multi_agent.inbox.types import InboxMessage

        db_path = tmp_path / ".modex" / "state.db"
        manager = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
        await manager.open()
        try:
            # Write to coder pool
            cli_coder = SqliteInboxMQ(
                db_path=db_path,
                scope=_PoolScopedRecordScope(pool="coder"),
                connection=None,
            )
            sid = "conv1.orchestrator"
            cli_coder.deliver(
                sid,
                InboxMessage(
                    session_id=sid,
                    source="x",
                    content="c",
                    message_type="agent_message",
                    message_id=uuid4().hex,
                    timestamp=datetime.now(UTC),
                    metadata={},
                ),
            )

            # Read from default pool — must be empty
            bot_default = SqliteInboxMQ(
                db_path=db_path,
                scope=_PoolScopedRecordScope(pool="default"),
                connection=manager,
            )
            pending = await bot_default.sessions_with_pending()
            assert sid not in pending, (
                f"pool='default' must not see coder's messages; got {pending}"
            )
        finally:
            await manager.close()
