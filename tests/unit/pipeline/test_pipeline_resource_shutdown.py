from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_group import ToolGroupResource
from modex_agent.messaging.models import InputMessage
from modex_agent.multi_agent.descriptor import AgentInstance
from modex_agent.pipeline.pipeline import AgentPipeline
from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry


async def test_pipeline_stop_cancels_and_drains_turn_before_agent_close() -> None:
    order: list[str] = []
    registry = TurnSessionRegistry()
    registry.set_session_lock("session")

    async def turn() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            order.append("turn_drained")

    task = asyncio.create_task(turn())
    registry.register_task("session", task)
    await asyncio.sleep(0)
    agent = MagicMock()
    agent.stop = AsyncMock(side_effect=lambda: order.append("agent_closed"))
    runner = MagicMock()
    runner.hook_runner = None
    runner.cleanup_session = AsyncMock()
    pipeline = AgentPipeline(
        agent=agent,
        turn_runner=runner,
        input_adapter=MagicMock(),
        output_adapter=MagicMock(),
        registry=registry,
    )

    assert await pipeline.stop() is True

    assert task.done()
    assert order == ["turn_drained", "agent_closed"]


async def test_pipeline_stop_does_not_close_agent_while_current_turn_is_active() -> None:
    registry = TurnSessionRegistry()
    current = asyncio.current_task()
    assert current is not None
    registry.register_task("session", current)
    agent = MagicMock()
    agent.stop = AsyncMock()
    runner = MagicMock()
    runner.hook_runner = None
    pipeline = AgentPipeline(
        agent=agent,
        turn_runner=runner,
        input_adapter=MagicMock(),
        output_adapter=MagicMock(),
        registry=registry,
    )

    assert await pipeline.stop() is False
    agent.stop.assert_not_awaited()
    registry.unregister_turn("session")


async def test_instance_closes_resource_once_after_real_pipeline_drain() -> None:
    order: list[str] = []
    registry = TurnSessionRegistry()

    async def turn() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            order.append("turn_drained")

    class Resource(ToolGroupResource):
        async def aclose(self) -> None:
            order.append("resource_closed")

    task = asyncio.create_task(turn())
    registry.register_task("session", task)
    await asyncio.sleep(0)
    agent = MagicMock()
    agent.stop = AsyncMock(side_effect=lambda: order.append("agent_closed"))
    runner = MagicMock()
    runner.hook_runner = None
    pipeline = AgentPipeline(
        agent=agent,
        turn_runner=runner,
        input_adapter=MagicMock(),
        output_adapter=MagicMock(),
        registry=registry,
    )
    instance = AgentInstance(
        descriptor=MagicMock(),
        context_manager=MagicMock(),
        pipeline=pipeline,
        resources=(Resource(),),
    )

    assert await instance.stop() is True
    assert await instance.stop() is True
    assert order == ["turn_drained", "agent_closed", "resource_closed"]


async def test_stop_closes_admission_before_draining_unregistered_request() -> None:
    registry = TurnSessionRegistry()
    started = asyncio.Event()

    async def process_locked(*args: object, **kwargs: object) -> None:
        started.set()
        await asyncio.Event().wait()

    runner = MagicMock()
    runner.hook_runner = None
    runner.process_locked = AsyncMock(side_effect=process_locked)
    runner.cleanup_session = AsyncMock()
    agent = MagicMock()
    agent.stop = AsyncMock()
    pipeline = AgentPipeline(
        agent=agent,
        turn_runner=runner,
        input_adapter=MagicMock(),
        output_adapter=MagicMock(),
        registry=registry,
    )
    request = asyncio.create_task(
        pipeline.process_message_outcome(
            InputMessage(content="run", session=SessionInfo.from_str("s1.main"))
        )
    )
    await started.wait()

    try:
        assert await pipeline.stop() is True
        assert request.done()
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)


async def test_process_message_is_rejected_after_pipeline_stop() -> None:
    registry = TurnSessionRegistry()
    runner = MagicMock()
    runner.hook_runner = None
    runner.process_locked = AsyncMock(return_value=None)
    agent = MagicMock()
    agent.stop = AsyncMock()
    pipeline = AgentPipeline(
        agent=agent,
        turn_runner=runner,
        input_adapter=MagicMock(),
        output_adapter=MagicMock(),
        registry=registry,
    )
    assert await pipeline.stop() is True

    with pytest.raises(RuntimeError, match="closed"):
        await pipeline.process_message_outcome(
            InputMessage(content="late", session=SessionInfo.from_str("s1.main"))
        )
    runner.process_locked.assert_not_awaited()


async def test_post_drain_session_failure_does_not_skip_later_cleanup() -> None:
    events: list[str] = []
    registry = TurnSessionRegistry()
    registry.set_session_lock("session")

    class HookRunner:
        async def aclose(self) -> None:
            events.append("hook_closed")

    class Resource(ToolGroupResource):
        async def aclose(self) -> None:
            events.append("resource_closed")

    async def cleanup_session(session_id: str) -> None:
        events.append("session_cleanup")
        raise RuntimeError("session cleanup failed")

    runner = MagicMock()
    runner.hook_runner = HookRunner()
    runner.cleanup_session = AsyncMock(side_effect=cleanup_session)
    agent = MagicMock()
    agent.stop = AsyncMock(side_effect=lambda: events.append("agent_closed"))
    pipeline = AgentPipeline(
        agent=agent,
        turn_runner=runner,
        input_adapter=MagicMock(),
        output_adapter=MagicMock(),
        registry=registry,
    )
    instance = AgentInstance(
        descriptor=MagicMock(),
        context_manager=MagicMock(),
        pipeline=pipeline,
        resources=(Resource(),),
    )

    with pytest.raises(RuntimeError, match="session cleanup failed"):
        await instance.stop()

    assert events == [
        "session_cleanup",
        "hook_closed",
        "agent_closed",
        "resource_closed",
    ]
    assert instance.drain_confirmed is True
    assert instance.resources == ()


async def test_pipeline_error_wins_when_resource_cleanup_also_fails() -> None:
    events: list[str] = []
    registry = TurnSessionRegistry()

    class HookRunner:
        async def aclose(self) -> None:
            events.append("hook_failed")
            raise RuntimeError("hook cleanup failed")

    class Resource(ToolGroupResource):
        async def aclose(self) -> None:
            events.append("resource_failed")
            raise RuntimeError("resource cleanup failed")

    runner = MagicMock()
    runner.hook_runner = HookRunner()
    agent = MagicMock()
    agent.stop = AsyncMock(side_effect=lambda: events.append("agent_closed"))
    pipeline = AgentPipeline(
        agent=agent,
        turn_runner=runner,
        input_adapter=MagicMock(),
        output_adapter=MagicMock(),
        registry=registry,
    )
    resource = Resource()
    instance = AgentInstance(
        descriptor=MagicMock(),
        context_manager=MagicMock(),
        pipeline=pipeline,
        resources=(resource,),
    )

    with pytest.raises(RuntimeError, match="hook cleanup failed"):
        await instance.stop()

    assert events == ["hook_failed", "agent_closed", "resource_failed"]
    assert instance.resources == (resource,)
