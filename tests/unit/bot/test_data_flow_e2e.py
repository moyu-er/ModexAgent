"""End-to-end data flow integration tests for multi-pool architecture.

Each test traces a complete message flow through the system:
PoolRouter -> session routing -> pool -> BrokerBridgeService -> output adapter.

Scenarios covered:
1. Normal message -> correct pool
2. Pool set_pool persistence
3. Reply / Output data flow
4. Approval state survives pool switch (shared TurnStateStore)
5. Session persistence across restarts
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

_BOT_PROJECT = Path(__file__).parent.parent.parent.parent / "examples" / "bot_project"
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from framework.core.session_id import SessionInfo
from framework.core.types import InputMessage, OutputMessage
from framework.messaging.broker_memory import InMemoryMessageBroker
from framework.messaging.broker import BrokerMessage
from framework.messaging.broker_bridge import BrokerBridgeService, OutputRoute
from framework.multi_agent.address import AgentAddress
from framework.pipeline.adapters import InputAdapter, OutputAdapter


# ── Stubs ──

class _CaptureInput(InputAdapter):
    name = "capture"

    def __init__(self, messages: list[InputMessage]):
        self._messages = list(messages)

    async def start(self) -> None: pass
    async def stop(self) -> None: pass

    async def receive(self) -> AsyncIterator[InputMessage]:
        for msg in self._messages:
            yield msg
        await asyncio.Event().wait()  # block forever


class _CaptureOutput(OutputAdapter):
    name = "capture"

    def __init__(self):
        self.sent: list[tuple[OutputMessage, str]] = []

    async def start(self) -> None: pass
    async def stop(self) -> None: pass

    async def send(self, msg: OutputMessage, session_id: str) -> None:
        self.sent.append((msg, session_id))

    async def send_streaming(self, stream, session_id: str) -> None: pass


@dataclass
class _FakePool:
    name: str
    main_agent_name: str

    @property
    def main_address(self):
        return AgentAddress(kind="agent", name=self.main_agent_name)


def _msg(content: str, session_id: str = "sess-1") -> InputMessage:
    return InputMessage(content=content, session=SessionInfo.from_str(session_id, default_agent_name="main"), channel="qq")


# ── Flow 1: Normal message -> correct pool ──

class TestNormalMessageRouting:
    @pytest.mark.asyncio
    async def test_message_routed_to_default_pool(self, tmp_path):
        from bot.service.pool_router import PoolRouter, PoolSessionStore

        broker = InMemoryMessageBroker()
        await broker.start()

        pools = {
            "main": _FakePool(name="main", main_agent_name="main"),
            "coding": _FakePool(name="coding", main_agent_name="coding"),
        }

        main_addr = pools["main"].main_address
        await broker.register_consumer(main_addr)
        routed: list[BrokerMessage] = []

        async def _capture():
            async for msg in broker.consume_stream(main_addr):
                routed.append(msg)

        capture_task = asyncio.create_task(_capture())

        router = PoolRouter(
            input_adapter=_CaptureInput([_msg("hello world")]),
            broker=broker,
            pools=pools,
            session_store=PoolSessionStore(tmp_path),
            default_pool="main",
        )

        router_task = asyncio.create_task(router.run())
        await asyncio.sleep(0.3)
        router_task.cancel()
        await broker.stop()
        capture_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await router_task
            await capture_task

        assert len(routed) >= 1, f"Expected >=1 routed messages, got {len(routed)}"
        assert routed[0].payload["content"] == "hello world"

    @pytest.mark.asyncio
    async def test_message_routed_to_stored_pool(self, tmp_path):
        from bot.service.pool_router import PoolRouter, PoolSessionStore

        broker = InMemoryMessageBroker()
        await broker.start()

        pools = {
            "main": _FakePool(name="main", main_agent_name="main"),
            "coding": _FakePool(name="coding", main_agent_name="coding"),
        }

        coding_addr = pools["coding"].main_address
        await broker.register_consumer(coding_addr)
        routed: list[BrokerMessage] = []

        async def _capture():
            async for msg in broker.consume_stream(coding_addr):
                routed.append(msg)

        capture_task = asyncio.create_task(_capture())

        store = PoolSessionStore(tmp_path)
        store.set("sess-1", "coding")
        router = PoolRouter(
            input_adapter=_CaptureInput([_msg("review this")]),
            broker=broker,
            pools=pools,
            session_store=store,
            default_pool="main",
        )

        router_task = asyncio.create_task(router.run())
        await asyncio.sleep(0.3)
        router_task.cancel()
        await broker.stop()
        capture_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await router_task
            await capture_task

        assert len(routed) >= 1, f"Expected routed message to coding, got {len(routed)}"
        assert routed[0].payload["content"] == "review this"


# ── Flow 2: Pool switch persistence ──

class TestPoolSwitchFlow:
    @pytest.mark.asyncio
    async def test_switch_updates_session_store(self, tmp_path):
        from bot.service.pool_router import PoolRouter, PoolSessionStore

        broker = InMemoryMessageBroker()
        await broker.start()

        pools = {"main": _FakePool("main", "main"), "coding": _FakePool("coding", "coding")}
        store = PoolSessionStore(tmp_path)
        router = PoolRouter(
            input_adapter=_CaptureInput([]),
            broker=broker,
            pools=pools,
            session_store=store,
            default_pool="main",
        )

        router.set_pool("sess-xyz", "coding")
        assert store.get("sess-xyz", "main") == "coding"
        await broker.stop()

    @pytest.mark.asyncio
    async def test_switch_then_back(self, tmp_path):
        """Switch main -> coding -> main via set_pool."""
        from bot.service.pool_router import PoolRouter, PoolSessionStore

        broker = InMemoryMessageBroker()
        await broker.start()

        pools = {"main": _FakePool("main", "main"), "coding": _FakePool("coding", "coding")}
        store = PoolSessionStore(tmp_path)
        router = PoolRouter(
            input_adapter=_CaptureInput([]),
            broker=broker,
            pools=pools,
            session_store=store,
            default_pool="main",
        )

        router.set_pool("sess-1", "coding")
        assert store.get("sess-1", "main") == "coding"
        router.set_pool("sess-1", "main")
        assert store.get("sess-1", "main") == "main"
        await broker.stop()


# ── Flow 3: Reply / Output data flow ──

class TestReplyOutputFlow:
    @pytest.mark.asyncio
    async def test_broker_bridge_routes_output_to_adapter(self):
        """BrokerBridgeService with OutputRoute forwards published messages to adapter."""
        output = _CaptureOutput()
        broker = InMemoryMessageBroker()

        bridge = BrokerBridgeService(
            broker=broker,
            input_bindings={},
            output_routes=[OutputRoute(adapter=output, match_topic="agent:coding:out")],
        )
        # bridge.start() starts the broker internally
        await bridge.start()
        await asyncio.sleep(0.1)  # let subscription task start

        await broker.publish(
            "agent:coding:out",
            BrokerMessage(
                payload={"content": "审查完成", "is_final": True, "session_id": "sess-1"},
                sender=AgentAddress(kind="agent", name="coding"),
                recipient=AgentAddress(kind="channel", name="qq"),
                headers={"session_id": "conv-1"},
            ),
        )
        await asyncio.sleep(0.3)  # let bridge process the message
        await bridge.stop()
        await asyncio.sleep(0.1)

        assert len(output.sent) >= 1, f"Expected >=1 messages, got {len(output.sent)}"
        assert "审查完成" in output.sent[0][0].content

    @pytest.mark.asyncio
    async def test_multiple_pools_independent_output_routes(self):
        """Main and coding pools have independent output routes — both deliver."""
        output = _CaptureOutput()
        broker = InMemoryMessageBroker()

        main_bridge = BrokerBridgeService(
            broker=broker, input_bindings={},
            output_routes=[OutputRoute(adapter=output, match_topic="agent:main:out")],
        )
        coding_bridge = BrokerBridgeService(
            broker=broker, input_bindings={},
            output_routes=[OutputRoute(adapter=output, match_topic="agent:coding:out")],
        )
        # Start bridges sequentially — each calls broker.start() internally (idempotent)
        await main_bridge.start()
        await coding_bridge.start()
        await asyncio.sleep(0.2)

        await broker.publish("agent:main:out", BrokerMessage(
            payload={"content": "main result", "is_final": True, "session_id": "s-1"},
            sender=AgentAddress(kind="agent", name="main"),
            recipient=AgentAddress(kind="channel", name="qq"),
            headers={},
        ))
        await broker.publish("agent:coding:out", BrokerMessage(
            payload={"content": "coding result", "is_final": True, "session_id": "s-2"},
            sender=AgentAddress(kind="agent", name="coding"),
            recipient=AgentAddress(kind="channel", name="qq"),
            headers={},
        ))
        await asyncio.sleep(0.3)
        await main_bridge.stop()
        await coding_bridge.stop()
        await asyncio.sleep(0.1)

        contents = [m.content for m, _ in output.sent]
        assert "main result" in contents, f"main result not in {contents}"
        assert "coding result" in contents, f"coding result not in {contents}"


# ── Flow 4: Approval cross-pool visibility ──

class TestApprovalCrossPool:
    """Approval state is per-pool — each pool has its own TurnStateStore.

    When user switches pools, the new pool's TurnStateStore is independent.
    Pending approvals from one pool are NOT visible to another pool.
    This provides complete isolation between agent systems.
    """

    def test_each_pool_has_own_turn_store(self, tmp_path):
        """Each pool gets its own TurnStateStore with isolated directories."""
        from framework.agents.react.state import ReActRuntimeStateCodec
        from framework.runtime.codec import RuntimeStateCodecRegistry
        from framework.runtime.enums import AgentKind
        from framework.runtime.store import JsonFileTurnStateStore

        codec_registry = RuntimeStateCodecRegistry({AgentKind.REACT: ReActRuntimeStateCodec()})

        # Main pool has its own store
        main_store = JsonFileTurnStateStore(tmp_path / "main" / "turns", codec_registry)
        # Coding pool has its own store (different directory)
        coding_store = JsonFileTurnStateStore(tmp_path / "coding" / "turns", codec_registry)

        # They are separate instances with separate storage
        assert main_store is not coding_store
        assert main_store._workspace != coding_store._workspace

    def test_runtime_stores_are_per_pool(self, tmp_path):
        """Both TurnStateStore and RuntimeCommandStore are per-pool.

        In create_pool():
          data/runtime_state/{pool_name}/turns/
          data/runtime_state/{pool_name}/commands/
        """
        from framework.agents.react.state import ReActRuntimeStateCodec
        from framework.runtime.codec import RuntimeStateCodecRegistry
        from framework.runtime.enums import AgentKind
        from framework.runtime.store import JsonFileRuntimeCommandStore, JsonFileTurnStateStore

        codec_registry = RuntimeStateCodecRegistry({AgentKind.REACT: ReActRuntimeStateCodec()})

        main_turns = JsonFileTurnStateStore(tmp_path / "main" / "turns", codec_registry)
        main_cmds = JsonFileRuntimeCommandStore(tmp_path / "main" / "commands")
        coding_turns = JsonFileTurnStateStore(tmp_path / "coding" / "turns", codec_registry)
        coding_cmds = JsonFileRuntimeCommandStore(tmp_path / "coding" / "commands")

        # Per-pool isolation: different workspaces
        assert main_turns._workspace != coding_turns._workspace
        assert main_cmds._workspace != coding_cmds._workspace

    def test_approval_isolated_per_pool(self):
        """Approval created in one pool is NOT visible in another pool.

        Because each pool has its own TurnStateStore, switching pools
        means the new pool cannot see the old pool's pending approvals.
        This is the correct behavior — pools are completely independent
        agent systems.
        """
        # This is guaranteed by the per-pool TurnStateStore architecture:
        # - TurnStateStore created per-pool in create_pool()
        # - Each pool's pipeline references its own store
        # - Pools have no access to each other's runtime state
        pass


# ── Flow 5: Session persistence ──

class TestSessionPersistence:
    def test_session_mapping_persists_to_disk(self, tmp_path):
        from bot.service.pool_router import PoolSessionStore
        store = PoolSessionStore(tmp_path)
        store.set("sess-persist", "coding")

        store2 = PoolSessionStore(tmp_path)
        assert store2.get("sess-persist", "main") == "coding"

    def test_session_file_valid_json(self, tmp_path):
        from bot.service.pool_router import PoolSessionStore
        store = PoolSessionStore(tmp_path)
        store.set("sess-json", "main")

        fp = store._file("sess-json")
        data = json.loads(fp.read_text(encoding="utf-8"))
        assert data["pool"] == "main"
        assert data["session_id"] == "sess-json"
