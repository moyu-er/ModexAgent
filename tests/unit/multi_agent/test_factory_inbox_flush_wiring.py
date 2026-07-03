"""Factory auto-injection of InboxFlushHook must reach pipeline.hook_runner.

The turn loop dispatches ONLY ``pipeline.hook_runner`` (via ``runtime.hooks``);
``pipeline.hooks`` (the ``hooks=`` ctor list) is never dispatched. So any hook
the factory auto-injects must be added to ``hook_runner``, not appended to the
dead list — otherwise fold-in silently never fires (notably for subagents,
which have no separate manual wiring path the way main does via pool_builder).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from modex_agent.hook import HookRunner
from modex_agent.hook.builtin import InboxFlushHook
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.descriptor import AgentDescriptor
from modex_agent.multi_agent.factory import DefaultAgentFactory
from modex_agent.multi_agent.inbox.server_memory import InMemoryInboxServer


@pytest.mark.asyncio
async def test_factory_auto_injects_inbox_flush_hook_onto_hook_runner() -> None:
    """The auto-injected InboxFlushHook must land on pipeline.hook_runner."""
    broker = InMemoryMessageBroker()
    await broker.start()
    try:
        factory = DefaultAgentFactory(
            default_llm_provider=MagicMock(),
            inbox_server=InMemoryInboxServer(),
            default_hook_runner=HookRunner(),
        )
        descriptor = AgentDescriptor(address=AgentAddress(kind="agent", name="scout"))
        instance = await factory.create_agent(descriptor, broker=broker)

        runner = instance.pipeline.hook_runner
        assert runner is not None, "hook_runner must exist when default_hook_runner is wired"
        dispatched = [spec.hook for spec in runner.hook_specs]
        assert any(isinstance(h, InboxFlushHook) for h in dispatched), (
            "InboxFlushHook must be on pipeline.hook_runner (the only dispatched registry) "
            "so fold-in actually fires mid-turn"
        )
    finally:
        await broker.stop()


@pytest.mark.asyncio
async def test_factory_no_inbox_flush_hook_when_strategy_none() -> None:
    """Isolation: inbox_strategy='none' opts the agent out of fold-in."""
    broker = InMemoryMessageBroker()
    await broker.start()
    try:
        factory = DefaultAgentFactory(
            default_llm_provider=MagicMock(),
            inbox_server=InMemoryInboxServer(),
            default_hook_runner=HookRunner(),
        )
        descriptor = AgentDescriptor(
            address=AgentAddress(kind="agent", name="scout"),
            inbox_strategy="none",
        )
        instance = await factory.create_agent(descriptor, broker=broker)

        runner = instance.pipeline.hook_runner
        assert runner is not None
        dispatched = [spec.hook for spec in runner.hook_specs]
        assert not any(isinstance(h, InboxFlushHook) for h in dispatched), (
            "inbox_strategy='none' must opt out of InboxFlushHook auto-injection"
        )
    finally:
        await broker.stop()
