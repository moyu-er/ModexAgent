"""Tests for Phase 2 ControlChannel — type-routed storage, command_types filtering, TTL, cleanup_session."""

import asyncio
import pytest
from framework.control.channel import InMemoryControlChannel
from framework.control.types import ControlCommand, ControlCommandType, ControlScope


def _cmd(cid: str, ctype: ControlCommandType, sid: str = "s1",
         ttl: float | None = None, agent_id: str | None = None) -> ControlCommand:
    return ControlCommand(
        command_id=cid,
        type=ctype,
        scope=ControlScope(session_id=sid, agent_id=agent_id),
        ttl_seconds=ttl,
    )


class TestTypeRouting:
    """command_types 参数按类型过滤。"""

    async def test_drain_filters_by_command_types(self):
        ch = InMemoryControlChannel()
        await ch.send(_cmd("c1", ControlCommandType.CANCEL_TURN))
        await ch.send(_cmd("c2", ControlCommandType.INJECT_STEER))
        await ch.send(_cmd("c3", ControlCommandType.CANCEL_RUN))

        result = await ch.drain(
            ControlScope(session_id="s1"),
            command_types={ControlCommandType.CANCEL_TURN},
        )
        assert len(result) == 1
        assert result[0].command_id == "c1"

        # c3 should still be there
        remaining = await ch.drain(
            ControlScope(session_id="s1"),
            command_types={ControlCommandType.CANCEL_RUN},
        )
        assert len(remaining) == 1
        assert remaining[0].command_id == "c3"

    async def test_peek_filters_by_command_types(self):
        ch = InMemoryControlChannel()
        await ch.send(_cmd("c1", ControlCommandType.CANCEL_TURN))
        await ch.send(_cmd("c2", ControlCommandType.INJECT_STEER))

        result = await ch.peek(
            ControlScope(session_id="s1"),
            command_types={ControlCommandType.INJECT_STEER},
        )
        assert len(result) == 1
        assert result[0].command_id == "c2"

    async def test_drain_limit_with_command_types_does_not_drop_remaining(self):
        """drain limit 截断时不应该丢失未消费的命令。"""
        ch = InMemoryControlChannel()
        await ch.send(_cmd("c1", ControlCommandType.CANCEL_TURN))
        await ch.send(_cmd("c2", ControlCommandType.CANCEL_TURN))
        await ch.send(_cmd("c3", ControlCommandType.CANCEL_TURN))

        result = await ch.drain(
            ControlScope(session_id="s1"),
            limit=2,
            command_types={ControlCommandType.CANCEL_TURN},
        )
        assert len(result) == 2

        # 第三条应该还在
        remaining = await ch.drain(ControlScope(session_id="s1"), limit=10)
        assert len(remaining) == 1

    async def test_drain_limit_across_types_keeps_unconsumed(self):
        ch = InMemoryControlChannel()
        await ch.send(_cmd("c1", ControlCommandType.INJECT_STEER))
        await ch.send(_cmd("c2", ControlCommandType.CANCEL_TURN))
        await ch.send(_cmd("c3", ControlCommandType.INJECT_STEER))

        result = await ch.drain(
            ControlScope(session_id="s1"),
            limit=1,
            command_types={ControlCommandType.INJECT_STEER, ControlCommandType.CANCEL_TURN},
        )
        assert len(result) == 1

        remaining = await ch.drain(ControlScope(session_id="s1"), limit=10)
        assert len(remaining) == 2


class TestCleanupSession:
    """cleanup_session 释放 session 资源。"""

    async def test_cleanup_removes_all_commands(self):
        ch = InMemoryControlChannel()
        await ch.send(_cmd("c1", ControlCommandType.CANCEL_TURN, sid="s1"))
        await ch.send(_cmd("c2", ControlCommandType.CANCEL_RUN, sid="s1"))
        await ch.send(_cmd("c3", ControlCommandType.CANCEL_TURN, sid="s2"))

        await ch.cleanup_session("s1")

        # s1 cleared
        r1 = await ch.drain(ControlScope(session_id="s1"), limit=10)
        assert len(r1) == 0
        # s2 still has its command
        r2 = await ch.drain(ControlScope(session_id="s2"), limit=10)
        assert len(r2) == 1
        assert r2[0].command_id == "c3"


class TestTTL:
    """全局 TTL 和 per-command TTL。"""

    async def test_global_ttl_expires_commands(self):
        ch = InMemoryControlChannel(ttl_seconds=0.01)
        await ch.send(_cmd("c1", ControlCommandType.CANCEL_TURN))
        await asyncio.sleep(0.02)
        result = await ch.drain(ControlScope(session_id="s1"))
        assert len(result) == 0

    async def test_per_command_ttl_overrides_global(self):
        ch = InMemoryControlChannel(ttl_seconds=60.0)
        await ch.send(_cmd("c1", ControlCommandType.CANCEL_TURN, ttl=0.01))
        await asyncio.sleep(0.02)
        result = await ch.drain(ControlScope(session_id="s1"))
        assert len(result) == 0


class TestSessionIsolation:
    """多 session 隔离。"""

    async def test_different_sessions_isolated(self):
        ch = InMemoryControlChannel()
        await ch.send(_cmd("c1", ControlCommandType.CANCEL_TURN, sid="s1"))
        await ch.send(_cmd("c2", ControlCommandType.CANCEL_TURN, sid="s2"))

        r1 = await ch.drain(ControlScope(session_id="s1"))
        r2 = await ch.drain(ControlScope(session_id="s2"))
        assert len(r1) == 1
        assert r1[0].command_id == "c1"
        assert len(r2) == 1
        assert r2[0].command_id == "c2"

    async def test_concurrent_drain(self):
        ch = InMemoryControlChannel()
        for i in range(20):
            await ch.send(_cmd(f"c{i}", ControlCommandType.CANCEL_TURN, sid="s1"))

        async def drain_some(n: int) -> int:
            cmds = await ch.drain(ControlScope(session_id="s1"), limit=5)
            return len(cmds)

        tasks = [asyncio.create_task(drain_some(i)) for i in range(4)]
        results = await asyncio.gather(*tasks)
        assert sum(results) == 20
