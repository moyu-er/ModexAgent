"""Tests for PoolRouter and pool routing stores.

Pool switching is handled upstream by the input pipeline; PoolRouter only
persists the chosen pool (``set_pool``) and routes incoming messages to it.
"""
from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from inspect import isabstract
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from modex_agent.multi_agent.pool_router import LocalFilePoolRoutingStore

_BOT_PROJECT = Path(__file__).parent.parent.parent.parent / "examples" / "bot_project"
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from modex_agent.core.session_id import SessionInfo  # noqa: E402
from modex_agent.core.types import InputMessage  # noqa: E402
from modex_agent.messaging.broker import AddressKind, BrokerMessage  # noqa: E402
from modex_agent.messaging.broker_bridge import BrokerInputAdapter  # noqa: E402
from modex_agent.messaging.broker_memory import InMemoryMessageBroker  # noqa: E402
from modex_agent.multi_agent.address import AgentAddress  # noqa: E402

# ── Stubs ──

class _StubInput(BrokerInputAdapter):
    def __init__(self, messages: list[InputMessage] | None = None) -> None:
        super().__init__(
            broker=InMemoryMessageBroker(),
            address=AgentAddress(kind=AddressKind.AGENT, name="stub"),
        )
        self._messages = messages or []

    @property
    def name(self) -> str:
        return "stub"

    async def start(self) -> None: pass
    async def stop(self) -> None: pass

    def receive(self) -> AsyncIterator[InputMessage]:
        async def _messages() -> AsyncIterator[InputMessage]:
            for msg in self._messages:
                yield msg

        return _messages()


class _FakePoolInstance:
    """Minimal stub that has main_agent_name, main_address, and a recording
    ``pool.submit_input`` (the poll-driven routing entry point)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.main_agent_name = name
        self.submitted: list = []
        record = self.submitted

        class _Inner:
            async def submit_input(self, sid, msg):  # noqa: ANN001
                record.append((sid, msg))

        self.pool = _Inner()

    @property
    def main_address(self):
        return AgentAddress(kind=AddressKind.AGENT, name=self.main_agent_name)


# ── PoolRoutingStore Tests ──

class TestPoolRoutingStore:
    def test_abstract_store_cannot_be_instantiated(self) -> None:
        from modex_agent.multi_agent.pool_router import PoolRoutingStore

        assert isabstract(PoolRoutingStore)


class TestLocalFilePoolRoutingStore:
    @pytest.fixture
    def store(self, tmp_path: Path) -> LocalFilePoolRoutingStore:
        from modex_agent.multi_agent.pool_router import LocalFilePoolRoutingStore

        return LocalFilePoolRoutingStore(tmp_path)

    def test_missing_prefix_returns_none(
        self, store: LocalFilePoolRoutingStore
    ) -> None:
        assert store.get_pool("unknown-session") is None

    def test_set_pool_roundtrips(self, store: LocalFilePoolRoutingStore) -> None:
        store.set_pool("sess-123", "coding")

        assert store.get_pool("sess-123") == "coding"

    def test_delete_pool_removes_route(
        self, store: LocalFilePoolRoutingStore
    ) -> None:
        store.set_pool("sess-123", "coding")

        store.delete_pool("sess-123")

        assert store.get_pool("sess-123") is None

    def test_list_prefixes_returns_sorted_stored_prefixes(
        self, store: LocalFilePoolRoutingStore
    ) -> None:
        store.set_pool("sess-b", "coding")
        store.set_pool("sess-a", "main")

        assert store.list_prefixes() == ["sess-a", "sess-b"]

    def test_corrupted_route_raises_validation_error(
        self, store: LocalFilePoolRoutingStore
    ) -> None:
        from pydantic import ValidationError

        store._file("corrupt").write_text("{not valid json", encoding="utf-8")

        with pytest.raises(ValidationError):
            store.get_pool("corrupt")

    def test_delete_pool_routes_removes_only_matching(
        self, store: LocalFilePoolRoutingStore
    ) -> None:
        store.set_pool("sess-a", "pool_a")
        store.set_pool("sess-b", "pool_b")
        store.set_pool("sess-c", "pool_a")

        deleted = store.delete_pool_routes("pool_a")

        assert deleted == 2
        assert store.get_pool("sess-a") is None
        assert store.get_pool("sess-c") is None
        assert store.get_pool("sess-b") == "pool_b"
        assert store.list_prefixes() == ["sess-b"]

    def test_delete_pool_routes_no_match_returns_zero(
        self, store: LocalFilePoolRoutingStore
    ) -> None:
        store.set_pool("sess-1", "pool_a")
        deleted = store.delete_pool_routes("nonexistent")
        assert deleted == 0
        assert store.get_pool("sess-1") == "pool_a"

    def test_delete_pool_routes_skips_corrupt_files(
        self, store: LocalFilePoolRoutingStore
    ) -> None:
        """Corrupt JSON files are skipped, not raised — delete_pool_routes
        must not abort the cascade on a single bad record."""
        store.set_pool("sess-good", "pool_a")
        # Write a corrupt file that also matches the pool name (would be
        # deleted if parseable, but parse fails so it's skipped).
        store._file("sess-corrupt").write_text(
            "{not valid json", encoding="utf-8"
        )

        deleted = store.delete_pool_routes("pool_a")

        assert deleted == 1
        assert store.get_pool("sess-good") is None
        # Corrupt file is left in place (not deleted by delete_pool_routes).
        assert store._file("sess-corrupt").exists()


# ── PoolRouter Tests ──

class TestPoolRouterSetPool:
    """Pool switching is persisted via ``set_pool``."""

    @pytest.fixture
    def pools(self):
        return {
            "main": _FakePoolInstance("main"),
            "coding": _FakePoolInstance("coding"),
        }

    @pytest.fixture
    def router(self, pools, tmp_path):
        from modex_agent.multi_agent.pool_router import (
            LocalFilePoolRoutingStore,
            PoolRouter,
        )
        return PoolRouter(
            input_adapter=_StubInput(),
            broker=InMemoryMessageBroker(),
            pools=pools,
            session_store=LocalFilePoolRoutingStore(tmp_path),
            default_pool="main",
        )

    def test_set_pool_updates_store(self, router):
        router.set_pool("sess-xyz", "coding")
        assert router._session_store.get_pool("sess-xyz") == "coding"

    def test_set_pool_uses_snowflake_key(self, router):
        router.set_pool("snowflake.main", "coding")
        assert router._session_store.get_pool("snowflake") == "coding"


class TestPoolRouterRouting:
    """Test message routing through PoolRouter.run() and _route_to_pool()."""

    @pytest.fixture
    def broker(self):
        return InMemoryMessageBroker()

    @pytest.fixture
    def pools(self):
        return {
            "main": _FakePoolInstance("main"),
            "coding": _FakePoolInstance("coding"),
        }

    @pytest.fixture
    def router(self, broker, pools, tmp_path):
        from modex_agent.multi_agent.pool_router import (
            LocalFilePoolRoutingStore,
            PoolRouter,
        )
        return PoolRouter(
            input_adapter=_StubInput(),
            broker=broker,
            pools=pools,
            session_store=LocalFilePoolRoutingStore(tmp_path),
            default_pool="main",
        )

    @pytest.mark.asyncio
    async def test_routing_falls_back_to_default_for_unknown_pool(self, router, pools, broker):
        """When session's pool name is unknown, falls back to default pool."""
        await broker.start()
        router._session_store.set_pool("sess-3", "nonexistent")
        msg = InputMessage(
            content="hello",
            session=SessionInfo.from_str("sess-3", default_agent_name="main"),
            channel="test",
        )
        await router._route_to_pool(msg, pools["main"])
        await broker.stop()

    @pytest.mark.asyncio
    async def test_run_routes_to_stored_pool(self, router, pools, broker, tmp_path):
        """Messages are routed to the pool stored for their snowflake."""
        from modex_agent.multi_agent.pool_router import LocalFilePoolRoutingStore

        store = LocalFilePoolRoutingStore(tmp_path)
        store.set_pool("sess-route", "coding")
        router_with_store = type(router)(
            input_adapter=_StubInput([
                InputMessage(
                    content="hello",
                    session=SessionInfo.from_str("sess-route", default_agent_name="main"),
                    channel="test",
                ),
            ]),
            broker=broker,
            pools=pools,
            session_store=store,
            default_pool="main",
        )

        await broker.start()
        coding_addr = pools["coding"].main_address
        await broker.register_consumer(coding_addr)
        routed: list[BrokerMessage] = []

        async def _capture():
            async for msg in broker.consume_stream(coding_addr):
                routed.append(msg)

        import asyncio
        capture_task = asyncio.create_task(_capture())
        router_task = asyncio.create_task(router_with_store.run())
        await asyncio.sleep(0.3)
        router_task.cancel()
        await broker.stop()
        capture_task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await router_task
            await capture_task

        assert pools["coding"].submitted, "coding pool received no submission"
        assert pools["coding"].submitted[0][1].content == "hello"
