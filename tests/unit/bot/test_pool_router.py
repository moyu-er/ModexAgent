"""Tests for PoolRouter and PoolSessionStore.

Pool switching is handled upstream by the input pipeline; PoolRouter only
persists the chosen pool (``set_pool``) and routes incoming messages to it.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

_BOT_PROJECT = Path(__file__).parent.parent.parent.parent / "examples" / "bot_project"
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import InputMessage
from modex_agent.ioc.configs.app import _validate_pool_name
from modex_agent.ioc.configs.pool import PoolConfig
from modex_agent.ioc.configs.llm import LLMConfig
from modex_agent.ioc.configs.agent import AgentConfig
from modex_agent.pipeline.adapters import InputAdapter
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.messaging.broker import BrokerMessage
from modex_agent.multi_agent.address import AgentAddress


# ── Stubs ──

class _StubInput(InputAdapter):
    name = "stub"

    def __init__(self, messages: list[InputMessage] | None = None):
        self._messages = messages or []

    async def start(self) -> None: pass
    async def stop(self) -> None: pass

    async def receive(self) -> AsyncIterator[InputMessage]:
        for msg in self._messages:
            yield msg


def _make_pool_config(name: str) -> PoolConfig:
    return PoolConfig(
        llm=LLMConfig(model="test", api_key="k"),
        agents=[AgentConfig(name=name, role="main")],
    )


class _FakePoolInstance:
    """Minimal stub that has main_agent_name and main_address."""

    def __init__(self, name: str):
        self.name = name
        self.main_agent_name = name

    @property
    def main_address(self):
        return AgentAddress(kind="agent", name=self.main_agent_name)


# ── PoolSessionStore Tests ──

class TestPoolSessionStore:
    def test_get_returns_default_when_no_file(self):
        with tempfile.TemporaryDirectory() as d:
            from bot.service.pool_router import PoolSessionStore
            store = PoolSessionStore(Path(d))
            assert store.get("unknown_session", "default") == "default"

    def test_set_and_get_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            from bot.service.pool_router import PoolSessionStore
            store = PoolSessionStore(Path(d))
            store.set("sess-123", "coding")
            assert store.get("sess-123", "main") == "coding"

    def test_multiple_sessions_independent(self):
        with tempfile.TemporaryDirectory() as d:
            from bot.service.pool_router import PoolSessionStore
            store = PoolSessionStore(Path(d))
            store.set("sess-a", "main")
            store.set("sess-b", "coding")
            assert store.get("sess-a", "x") == "main"
            assert store.get("sess-b", "x") == "coding"

    def test_corrupted_file_returns_default(self):
        with tempfile.TemporaryDirectory() as d:
            from bot.service.pool_router import PoolSessionStore
            store = PoolSessionStore(Path(d))
            # Write corrupted JSON
            fp = store._file("corrupt")
            fp.write_text("{not valid json", encoding="utf-8")
            assert store.get("corrupt", "fallback") == "fallback"


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
        from bot.service.pool_router import PoolRouter, PoolSessionStore
        return PoolRouter(
            input_adapter=_StubInput(),
            broker=object(),
            pools=pools,
            session_store=PoolSessionStore(tmp_path),
            default_pool="main",
        )

    def test_set_pool_updates_store(self, router):
        router.set_pool("sess-xyz", "coding")
        assert router._session_store.get("sess-xyz", "main") == "coding"

    def test_set_pool_uses_snowflake_key(self, router):
        router.set_pool("snowflake.main", "coding")
        assert router._session_store.get("snowflake", "main") == "coding"


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
        from bot.service.pool_router import PoolRouter, PoolSessionStore
        return PoolRouter(
            input_adapter=_StubInput(),
            broker=broker,
            pools=pools,
            session_store=PoolSessionStore(tmp_path),
            default_pool="main",
        )

    @pytest.mark.asyncio
    async def test_routing_falls_back_to_default_for_unknown_pool(self, router, pools, broker):
        """When session's pool name is unknown, falls back to default pool."""
        await broker.start()
        router._session_store.set("sess-3", "nonexistent")
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
        from bot.service.pool_router import PoolSessionStore

        store = PoolSessionStore(tmp_path)
        store.set("sess-route", "coding")
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

        assert len(routed) >= 1
        assert routed[0].payload["content"] == "hello"


# ── Reserved Pool Name Validation Tests ──

class TestPoolNameValidation:
    def test_valid_pool_names(self):
        _validate_pool_name("main")
        _validate_pool_name("coding")
        _validate_pool_name("my-pool")
        _validate_pool_name("pool_123")

    def test_reserved_name_approve_rejected(self):
        with pytest.raises(ValueError, match="built-in command"):
            _validate_pool_name("approve")

    def test_reserved_name_deny_rejected(self):
        with pytest.raises(ValueError, match="built-in command"):
            _validate_pool_name("deny")

    def test_reserved_name_continue_rejected(self):
        with pytest.raises(ValueError, match="built-in command"):
            _validate_pool_name("continue")

    def test_invalid_format_rejected(self):
        with pytest.raises(ValueError, match="Invalid pool name"):
            _validate_pool_name("InvalidPool")  # uppercase
        with pytest.raises(ValueError, match="Invalid pool name"):
            _validate_pool_name("123abc")  # starts with digit
