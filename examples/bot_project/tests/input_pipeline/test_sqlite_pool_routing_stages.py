"""Regression: ResolvePoolStage + EnvironmentControlStage with SqlitePoolRoutingStore.

Before the fix, ``PoolRoutingStore`` ABC did not define ``.get()`` / ``.set()``
convenience methods — they existed only on ``LocalFilePoolRoutingStore``.
``SqlitePoolRoutingStore`` inherited the ABC without them, so any stage that
called ``ctx.pool_session_store.set(...)`` or ``.get(...)`` raised
``AttributeError`` under the SQLite backend (the bot's default).

This test reproduces that exact path with a real migrated SQLite DB and a real
``SqlitePoolRoutingStore`` — no mocks on the store.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.environment_control import EnvironmentControlStage
from bot.input_pipeline.stages.resolve_pool import ResolvePoolStage

from modex_agent.core.session_id import encode_snowflake
from modex_agent.input_pipeline.envelope import UserInputEnvelope
from modex_agent.input_pipeline.stage import Continue, Terminate
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters.pool_routing_store import SqlitePoolRoutingStore


@pytest.fixture()
def sqlite_pool_store(tmp_path: Path) -> Iterator[SqlitePoolRoutingStore]:
    """Open + migrate a workspace DB, then yield a SqlitePoolRoutingStore."""
    db_path = tmp_path / "workspace.db"
    mgr = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
    asyncio.run(mgr.open())
    asyncio.run(mgr.close())
    store = SqlitePoolRoutingStore(db_path)
    yield store
    store.close()


def _ctx(store: SqlitePoolRoutingStore, *, current_ws: Path | None = None) -> BotInputContext:
    ws = current_ws or Path("/project")

    class _FakeAdapter:
        name = "qq"
        current_ws: Path = ws
        home: Path = Path("/project")

        def save_current_ws(self) -> None:
            pass

        async def _try_intercept_control(self, text: str, session_id: str) -> bool:
            return False

    return BotInputContext(
        default_pool="main",
        available_pools=lambda: {"main", "coding"},
        pool_session_store=store,
        agent_resolver=lambda p: p,
        transcript_store=MagicMock(),
        enqueue_message=MagicMock(),
        command_adapter=_FakeAdapter(),  # type: ignore[arg-type]
    )


class TestResolvePoolStageSqlite:
    """ResolvePoolStage must work with SqlitePoolRoutingStore (regression)."""

    @pytest.mark.asyncio
    async def test_explicit_pool_persists_to_sqlite(
        self, sqlite_pool_store: SqlitePoolRoutingStore
    ) -> None:
        ctx = _ctx(sqlite_pool_store)
        env = UserInputEnvelope(
            external_id="u1", content="hi", channel="websocket", explicit_pool="coding"
        )
        result = await ResolvePoolStage().process(env, ctx)
        assert isinstance(result, Continue)
        assert env.metadata["resolved_pool"] == "coding"
        # The convenience .set() must have persisted to the SQLite store.
        assert sqlite_pool_store.get_pool(encode_snowflake("u1")) == "coding"

    @pytest.mark.asyncio
    async def test_no_explicit_pool_reads_default_from_sqlite(
        self, sqlite_pool_store: SqlitePoolRoutingStore
    ) -> None:
        ctx = _ctx(sqlite_pool_store)
        env = UserInputEnvelope(external_id="u1", content="hi", channel="qq", explicit_pool=None)
        result = await ResolvePoolStage().process(env, ctx)
        assert isinstance(result, Continue)
        # No prior route → falls back to default_pool="main".
        assert env.metadata["resolved_pool"] == "main"
        # .set() must have persisted the default so PoolRouter can read it.
        assert sqlite_pool_store.get_pool(encode_snowflake("u1")) == "main"


class TestEnvironmentControlStageSqlite:
    """EnvironmentControlStage /pool command must work with SqlitePoolRoutingStore."""

    @pytest.mark.asyncio
    async def test_pool_command_persists_to_sqlite(
        self, sqlite_pool_store: SqlitePoolRoutingStore
    ) -> None:
        ctx = _ctx(sqlite_pool_store)
        env = UserInputEnvelope(external_id="u1", content="/coding", channel="qq")
        result = await EnvironmentControlStage(known_pools={"coding"}).process(env, ctx)
        assert isinstance(result, Terminate)
        assert result.reason == "pool_switch"
        assert sqlite_pool_store.get_pool(encode_snowflake("u1")) == "coding"

    @pytest.mark.asyncio
    async def test_non_command_reads_current_pool_from_sqlite(
        self, sqlite_pool_store: SqlitePoolRoutingStore
    ) -> None:
        sqlite_pool_store.set_pool(encode_snowflake("u1"), "coding")
        ctx = _ctx(sqlite_pool_store)
        env = UserInputEnvelope(external_id="u1", content="hello world", channel="qq")
        result = await EnvironmentControlStage().process(env, ctx)
        # Non-command messages pass through; the stage reads .get() on the
        # SQLite store without raising.
        assert isinstance(result, Continue)
