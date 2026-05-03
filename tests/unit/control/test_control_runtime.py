"""Tests for ControlRuntime and ControlPhase."""
import pytest
import asyncio
from framework.control.runtime import ControlRuntime, ControlPhase
from framework.control.channel import InMemoryControlChannel
from framework.control.store import InMemoryControlStore
from framework.control.types import ControlCommand, ControlCommandType, ControlScope
from framework.interceptor.handler import CommandHandlerRegistry


class TestControlPhase:
    def test_all_phases_defined(self):
        assert ControlPhase.BEFORE_TURN == "before_turn"
        assert ControlPhase.BEFORE_ITERATION == "before_iteration"
        assert ControlPhase.BEFORE_LLM == "before_llm"
        assert ControlPhase.BEFORE_TOOL_BATCH == "before_tool_batch"
        assert ControlPhase.BEFORE_TOOL_CALL == "before_tool_call"


class TestControlRuntime:
    def test_construction(self):
        channel = InMemoryControlChannel()
        store = InMemoryControlStore()
        registry = CommandHandlerRegistry()
        cr = ControlRuntime(channel=channel, store=store, registry=registry)
        assert cr.channel is channel
        assert cr.store is store
        assert cr.max_commands == 3

    def test_drain_no_commands_does_not_raise(self):
        channel = InMemoryControlChannel()
        store = InMemoryControlStore()
        registry = CommandHandlerRegistry()
        cr = ControlRuntime(channel=channel, store=store, registry=registry)

        from framework.core.agent import AgentContext
        from framework.core.tool_manager import InMemoryToolManager
        from framework.memory.history import ListMessageHistory

        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
        )

        async def _test():
            await cr.drain(ctx, phase=ControlPhase.BEFORE_TURN)
        asyncio.run(_test())


class TestInMemoryControlStore:
    def test_append_and_claim(self):
        store = InMemoryControlStore()
        scope = ControlScope(session_id="s1")
        cmd = ControlCommand(
            command_id="c1",
            type=ControlCommandType.CANCEL_TURN,
            scope=scope,
        )
        async def _test():
            await store.append_command(scope, cmd)
            claimed = await store.claim_commands(scope, limit=5)
            assert len(claimed) == 1
            assert claimed[0].command_id == "c1"
        asyncio.run(_test())

    def test_claim_filters_by_command_type(self):
        store = InMemoryControlStore()
        scope = ControlScope(session_id="s1")
        cmd1 = ControlCommand(command_id="c1", type=ControlCommandType.CANCEL_TURN, scope=scope)
        cmd2 = ControlCommand(command_id="c2", type=ControlCommandType.INJECT_USER_MESSAGE, scope=scope)
        async def _test():
            await store.append_command(scope, cmd1)
            await store.append_command(scope, cmd2)
            claimed = await store.claim_commands(
                scope, limit=5,
                command_types={ControlCommandType.CANCEL_TURN},
            )
            assert len(claimed) == 1
            assert claimed[0].type == ControlCommandType.CANCEL_TURN
        asyncio.run(_test())
