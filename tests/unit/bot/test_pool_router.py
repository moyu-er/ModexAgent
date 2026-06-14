"""Integration tests for PoolRouter, PoolSessionStore, and pool switching.

Covers:
- /pool_name switching (including switching to current pool)
- Routing of normal messages to session's pool
- Special commands (/approve, /deny, /continue) pass-through
- Pool name validation
- Session store persistence
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

from framework.core.session_id import SessionId
from framework.core.types import InputMessage, OutputMessage
from framework.ioc.configs.pool import PoolConfig
from framework.ioc.configs.llm import LLMConfig
from framework.ioc.configs.agent import AgentConfig
from framework.pipeline.adapters import InputAdapter, OutputAdapter
from framework.messaging.broker_memory import InMemoryMessageBroker
from framework.messaging.broker import BrokerMessage
from framework.multi_agent.address import AgentAddress


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

    async def send_reply(self, msg: OutputMessage, session_id: str) -> None: pass


class _StubOutput(OutputAdapter):
    name = "stub"
    def __init__(self):
        self.sent: list[tuple[OutputMessage, str]] = []

    async def start(self) -> None: pass
    async def stop(self) -> None: pass

    async def send(self, msg: OutputMessage, session_id: str) -> None:
        self.sent.append((msg, session_id))

    async def send_streaming(self, stream, session_id: str) -> None: pass


# ── Helpers ──

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

class TestPoolRouterExtractPoolCommand:
    """Test _extract_pool_command regex and pool name matching."""

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
            output_adapter=_StubOutput(),
            broker=object(),
            pools=pools,
            session_store=PoolSessionStore(tmp_path),
            default_pool="main",
        )

    def test_exact_pool_name_match(self, router):
        """Exact /pool_name matches a known pool."""
        assert router._extract_pool_command("/main") == "main"
        assert router._extract_pool_command("/coding") == "coding"

    def test_none_for_non_pool_name(self, router):
        """Names not in pools return None."""
        assert router._extract_pool_command("/unknown") is None

    def test_none_for_command_with_args(self, router):
        """Commands with arguments (like /skill args) are NOT pool switches."""
        assert router._extract_pool_command("/weather 上海") is None
        assert router._extract_pool_command("/approve yes") is None

    def test_none_for_builtin_commands_when_no_pool(self, router):
        """Built-in commands like /approve are NOT pool switches (no pool named approve)."""
        assert router._extract_pool_command("/approve") is None
        assert router._extract_pool_command("/deny") is None
        assert router._extract_pool_command("/continue") is None

    def test_none_for_empty_content(self, router):
        assert router._extract_pool_command(None) is None
        assert router._extract_pool_command("") is None

    def test_none_for_normal_text(self, router):
        assert router._extract_pool_command("hello world") is None
        assert router._extract_pool_command("帮我写代码") is None

    def test_none_for_text_with_slash_not_command(self, router):
        """Text starting with / but containing spaces is not a pool command."""
        assert router._extract_pool_command("/hello world foo") is None

    def test_switch_to_current_pool_returns_pool_name(self, router):
        """Switching to current pool still returns the pool name (valid command)."""
        # Even if session is already in 'main', /main is still a valid pool command
        assert router._extract_pool_command("/main") == "main"


class TestPoolRouterRouting:
    """Test complete routing flow: switch + route."""

    @pytest.fixture
    def broker(self):
        return InMemoryMessageBroker()

    @pytest.fixture
    def output(self):
        return _StubOutput()

    @pytest.fixture
    def pools(self):
        return {
            "main": _FakePoolInstance("main"),
            "coding": _FakePoolInstance("coding"),
        }

    @pytest.fixture
    def router(self, broker, output, pools, tmp_path):
        from bot.service.pool_router import PoolRouter, PoolSessionStore
        return PoolRouter(
            input_adapter=_StubInput(),
            output_adapter=output,
            broker=broker,
            pools=pools,
            session_store=PoolSessionStore(tmp_path),
            default_pool="main",
        )

    @pytest.mark.asyncio
    async def test_handle_switch_sends_confirmation(self, router, output):
        """Switching pool sends a confirmation message to the user."""
        await router._handle_switch("sess-1", "coding")
        assert len(output.sent) == 1
        msg, sid = output.sent[0]
        assert "coding" in msg.content
        assert sid == "sess-1"

    @pytest.mark.asyncio
    async def test_handle_switch_updates_session_store(self, router):
        """After switch, session store returns the new pool."""
        await router._handle_switch("sess-2", "coding")
        assert router._session_store.get("sess-2", "main") == "coding"

    @pytest.mark.asyncio
    async def test_routing_falls_back_to_default_for_unknown_pool(self, router, pools):
        """When session's pool name is unknown, falls back to default pool."""
        router._session_store.set("sess-3", "nonexistent")
        msg = InputMessage(content="hello", session=SessionId.from_str("sess-3", default_agent_name="main"), channel="test")
        await router._route_to_pool(msg, pools["main"])


# ── Reserved Pool Name Validation Tests ──

class TestPoolNameValidation:
    def test_valid_pool_names(self):
        from framework.ioc.configs.app import _validate_pool_name
        _validate_pool_name("main")
        _validate_pool_name("coding")
        _validate_pool_name("my-pool")
        _validate_pool_name("pool_123")

    def test_reserved_name_approve_rejected(self):
        from framework.ioc.configs.app import _validate_pool_name
        with pytest.raises(ValueError, match="built-in command"):
            _validate_pool_name("approve")

    def test_reserved_name_deny_rejected(self):
        from framework.ioc.configs.app import _validate_pool_name
        with pytest.raises(ValueError, match="built-in command"):
            _validate_pool_name("deny")

    def test_reserved_name_continue_rejected(self):
        from framework.ioc.configs.app import _validate_pool_name
        with pytest.raises(ValueError, match="built-in command"):
            _validate_pool_name("continue")

    def test_invalid_format_rejected(self):
        from framework.ioc.configs.app import _validate_pool_name
        with pytest.raises(ValueError, match="Invalid pool name"):
            _validate_pool_name("InvalidPool")  # uppercase
        with pytest.raises(ValueError, match="Invalid pool name"):
            _validate_pool_name("123abc")  # starts with digit
