"""Bot subagent governance carries the memory-compaction head (per-model compaction T3).

The bot's subagent road is the framework's: ``AgentTemplate`` builds one
``MemorySystemContextManager`` per materialization and passes it to
``DefaultAgentFactory.create_agent`` (the bot's ``_build_agent_factory``
product wraps exactly that factory). These tests pin the bot-relevant
properties through the REAL bot factory seam:

- a bot subagent with its per-materialization memory CM gets
  ``MemoryCompactionGovernance`` as the chain head, bound to THAT CM
  (NOT the pool-level CM the main agent uses — a subagent's load() runs
  through its own CM, so the same-instance constraint binds here);
- the resolver returns the exact ``MemoryContext`` instance the subagent's
  ``load()`` built;
- without a memory CM the legacy repair-only chain is kept.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# Bot tests resolve ``bot.*`` via the repo root inserted into sys.path.
sys.path.insert(0, str(Path(__file__).parents[3]))

from bot.service.pool.agent_factory import _build_agent_factory

from modex_agent.core.agent import AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.memory.compaction_governance import MemoryCompactionGovernance
from modex_agent.memory.context import InMemoryContextManager
from modex_agent.memory.context_governance import (
    CompositeGovernance,
    ToolChainRepairGovernance,
)
from modex_agent.memory.default_system import DefaultMemorySystem
from modex_agent.memory.history import ListMessageHistory
from modex_agent.memory.injection import FullInjectionPolicy
from modex_agent.memory.layers.factory import MemoryLayerFactory
from modex_agent.memory.presets import subagent_memory
from modex_agent.memory.registry import DefaultMemoryStoreRegistry
from modex_agent.memory.system import MemorySystemContextManager
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import AgentDescriptor, AgentLLMConfig
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.factory import AgentCommKind
from modex_agent.tools.manager import InMemoryToolManager

_SESSION = "task-77.researcher"


def _bot_factory() -> Any:
    return _build_agent_factory(
        provider=MagicMock(name="provider"),
        tool_manager=None,
        inbox_server=MagicMock(name="inbox_server"),
        inbox_consumer=MagicMock(name="inbox_consumer"),
        shared_hooks=[],
        shared_hook_runner=MagicMock(name="hook_runner"),
        shared_interceptor_chain=None,
        control_channel=None,
        workspace_resolver=None,
        pool_name="main",
        emitter_factory=None,
    )


def _subagent_descriptor() -> AgentDescriptor:
    return AgentDescriptor(
        address=AgentAddress(name="researcher"),
        llm_config=AgentLLMConfig(model="m1"),
        system_prompt_template="You are a researcher.",
        execution_strategy="react",
        context_strategy="persistent",
        comm_kind=AgentCommKind.SUBAGENT,
        memory_config=subagent_memory(),
    )


def _subagent_cm(tmp_path: Path) -> MemorySystemContextManager:
    """The per-materialization CM shape ``AgentTemplate`` builds (session-only)."""
    registry = DefaultMemoryStoreRegistry(tmp_path)
    memory_system = DefaultMemorySystem(
        layer_set=MemoryLayerFactory.single_user(registry=registry),
        store_registry=registry,
    )
    return MemorySystemContextManager(
        memory_system=memory_system,
        default_agent_id="researcher",
        default_agent_role="subagent",
        injection_policy=FullInjectionPolicy(),
    )


def _governance(instance: Any) -> Any:
    pipeline = instance.pipeline
    assert pipeline is not None
    builder = pipeline._turn_runner.turn_context_builder
    assert builder is not None
    return builder.governance


async def test_bot_subagent_chain_head_binds_to_its_own_cm(tmp_path: Path) -> None:
    factory = _bot_factory()
    subagent_cm = _subagent_cm(tmp_path)
    instance = await factory.create_agent(
        _subagent_descriptor(),
        broker=InMemoryMessageBroker(),
        context_manager=subagent_cm,  # what template materialization passes
    )

    governance = _governance(instance)
    assert isinstance(governance, CompositeGovernance)
    strategies = governance._strategies
    assert len(strategies) == 2
    head = strategies[0]
    assert isinstance(head, MemoryCompactionGovernance)
    assert isinstance(strategies[1], ToolChainRepairGovernance)
    # Bound to the SUBAGENT's own memory system — the one its history
    # compacts through — not to any pool-level system.
    assert head._memory_system is subagent_cm.memory_system

    # Same-instance constraint on the subagent's own CM: after load() built
    # the session context, the resolver returns exactly that instance.
    await subagent_cm.load(_SESSION)
    agent_ctx = AgentContext(
        system_prompt="",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str(_SESSION),
        runtime=None,
    )
    resolved = head._memory_context_resolver(agent_ctx)
    assert resolved is subagent_cm.resolve_memory_context(_SESSION)
    assert resolved is subagent_cm._context_cache[_SESSION]
    # Scope identity follows the subagent CM's defaults (agent_id), proving
    # the resolver did NOT fall back to a main/pool-shaped context.
    assert resolved.agent_id == "researcher"


async def test_bot_subagent_without_memory_cm_keeps_legacy_chain() -> None:
    factory = _bot_factory()
    instance = await factory.create_agent(
        _subagent_descriptor(),
        broker=InMemoryMessageBroker(),
        context_manager=InMemoryContextManager(base_system_prompt="x"),
    )

    governance = _governance(instance)
    assert isinstance(governance, CompositeGovernance)
    assert len(governance._strategies) == 1
    assert isinstance(governance._strategies[0], ToolChainRepairGovernance)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
