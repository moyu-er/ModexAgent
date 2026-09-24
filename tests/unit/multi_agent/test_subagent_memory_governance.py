"""Subagent governance gets the memory-compaction head (per-model compaction T3).

``DefaultAgentFactory.create_agent`` builds the SUBAGENT governance chain.
When the subagent's context manager is memory-backed (every native
materialization passes the per-subagent ``MemorySystemContextManager`` built
by ``AgentTemplate``), the chain head is ``MemoryCompactionGovernance`` bound
to THAT context manager:

- its memory system is ``ctx_mgr.memory_system`` — the system the subagent's
  ``ScopedMessageHistory`` compacts through;
- its resolver returns the SAME ``MemoryContext`` instances the subagent's
  ``load()`` builds (turn_runner never overrides a subagent's CM with the
  pool's — see ``turn_runner.py`` ``_locked_process``), satisfying the T3
  same-instance hard constraint;
- a non-memory context manager keeps the legacy repair-only chain.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from modex_agent.core.agent import AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.memory.context import InMemoryContextManager
from modex_agent.memory.default_system import DefaultMemorySystem
from modex_agent.memory.history import ListMessageHistory
from modex_agent.memory.injection import FullInjectionPolicy
from modex_agent.memory.layers.factory import MemoryLayerFactory
from modex_agent.memory.presets import subagent_memory
from modex_agent.memory.registry import DefaultMemoryStoreRegistry
from modex_agent.memory.system import MemorySystemContextManager
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import AgentDescriptor, AgentLLMConfig, DefaultAgentFactory
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.factory import AgentCommKind
from modex_agent.tools.manager import InMemoryToolManager

_SESSION = "task-123.scout"


def _subagent_descriptor() -> AgentDescriptor:
    return AgentDescriptor(
        address=AgentAddress(name="scout"),
        llm_config=AgentLLMConfig(model="m1"),
        system_prompt_template="You are a scout.",
        execution_strategy="react",
        context_strategy="persistent",
        comm_kind=AgentCommKind.SUBAGENT,
        memory_config=subagent_memory(),
    )


def _memory_context_manager(tmp_path: Path) -> MemorySystemContextManager:
    registry = DefaultMemoryStoreRegistry(tmp_path)
    memory_system = DefaultMemorySystem(
        layer_set=MemoryLayerFactory.single_user(registry=registry),
        store_registry=registry,
    )
    return MemorySystemContextManager(
        memory_system=memory_system,
        default_agent_id="scout",
        default_agent_role="subagent",
        injection_policy=FullInjectionPolicy(),
    )


def _governance_of(instance: object) -> object:
    pipeline = instance.pipeline
    assert pipeline is not None
    builder = pipeline._turn_runner.turn_context_builder
    assert builder is not None
    return builder.governance


def _agent_ctx() -> AgentContext:
    return AgentContext(
        system_prompt="",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str(_SESSION),
        runtime=None,
    )


class TestSubagentMemoryGovernanceHead:
    async def test_memory_cm_yields_compaction_head_bound_to_it(self, tmp_path: Path) -> None:
        """A memory-backed subagent CM → chain head MemoryCompactionGovernance
        carrying THAT CM's memory system and a resolver over THAT CM."""
        from modex_agent.memory.compaction_governance import MemoryCompactionGovernance
        from modex_agent.memory.context_governance import (
            CompositeGovernance,
            ToolChainRepairGovernance,
        )

        cm = _memory_context_manager(tmp_path)
        factory = DefaultAgentFactory(default_llm_provider=MagicMock())
        instance = await factory.create_agent(
            _subagent_descriptor(),
            broker=InMemoryMessageBroker(),
            context_manager=cm,
        )

        governance = _governance_of(instance)
        assert isinstance(governance, CompositeGovernance)
        strategies = governance._strategies
        assert len(strategies) == 2
        assert isinstance(strategies[0], MemoryCompactionGovernance)
        assert isinstance(strategies[1], ToolChainRepairGovernance)
        assert strategies[0]._memory_system is cm.memory_system

        # Same-instance hard constraint: after load() built the session's
        # MemoryContext inside cm, the governance resolver hands back exactly
        # that instance (memory stores are keyed by MemoryContext).
        await cm.load(_SESSION)
        agent_ctx = _agent_ctx()
        resolved = strategies[0]._memory_context_resolver(agent_ctx)
        assert resolved is cm.resolve_memory_context(_SESSION)
        assert resolved is cm._context_cache[_SESSION]

    async def test_non_memory_cm_keeps_legacy_repair_only_chain(self) -> None:
        from modex_agent.memory.context_governance import (
            CompositeGovernance,
            ToolChainRepairGovernance,
        )

        factory = DefaultAgentFactory(default_llm_provider=MagicMock())
        instance = await factory.create_agent(
            _subagent_descriptor(),
            broker=InMemoryMessageBroker(),
            context_manager=InMemoryContextManager(base_system_prompt="x"),
        )

        governance = _governance_of(instance)
        assert isinstance(governance, CompositeGovernance)
        assert len(governance._strategies) == 1
        assert isinstance(governance._strategies[0], ToolChainRepairGovernance)

    async def test_normal_agent_still_gets_no_factory_governance(
        self, tmp_path: Path
    ) -> None:
        """Main agents register their governance at pool wiring
        (``_wire_main_pipeline``), never here — unchanged behavior."""
        descriptor = _subagent_descriptor().model_copy(
            update={"comm_kind": AgentCommKind.NORMAL}
        )
        factory = DefaultAgentFactory(default_llm_provider=MagicMock())
        instance = await factory.create_agent(
            descriptor,
            broker=InMemoryMessageBroker(),
            context_manager=_memory_context_manager(tmp_path),
        )
        assert _governance_of(instance) is None


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
