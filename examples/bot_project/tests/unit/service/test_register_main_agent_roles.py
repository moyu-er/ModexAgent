"""T1 data-layer透传 test: ``_register_main_agent`` reads ``roles`` from
``MainAgentSpec`` and writes it onto the constructed ``AgentDescriptor``.

The main-agent factory in ``pool_builder.py`` is the main-agent-side twin
of ``AgentTemplate.materialize`` (subagent side). Both must propagate
``roles`` from the wire model to the runtime descriptor.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from bot.service.pool.pool_construction import _register_main_agent

from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.multi_agent.pool_config.specs import MainAgentSpec


@pytest.mark.asyncio
async def test_register_main_agent_passes_roles_to_descriptor() -> None:
    """MainAgentSpec.roles lands on AgentDescriptor.roles via _register_main_agent."""
    fake_instance = MagicMock()
    fake_instance.pipeline = MagicMock()
    fake_instance.stop = AsyncMock()

    pool = MagicMock()
    pool.register_resident = AsyncMock()

    factory = MagicMock()
    factory.create_agent = AsyncMock(return_value=fake_instance)

    main_spec = MainAgentSpec(agent_name="main", roles=["coordinator"])

    await _register_main_agent(
        pool=pool,
        main_spec=main_spec,
        assembly_deps=PoolAssemblyDeps(),
        system_prompt="",
        safety=RuntimeSafetyPolicy(),
        pool_name="default",
        factory=factory,
        broker=MagicMock(),
        context_manager=MagicMock(),
        bot_model_config=None,
    )

    call_kwargs = factory.create_agent.call_args.kwargs
    descriptor = call_kwargs.get("descriptor") or factory.create_agent.call_args.args[0]
    assert descriptor.roles == ["coordinator"]


@pytest.mark.asyncio
async def test_register_main_agent_roles_default_empty() -> None:
    """When MainAgentSpec omits roles, descriptor.roles defaults to []."""
    fake_instance = MagicMock()
    fake_instance.pipeline = MagicMock()
    fake_instance.stop = AsyncMock()

    pool = MagicMock()
    pool.register_resident = AsyncMock()

    factory = MagicMock()
    factory.create_agent = AsyncMock(return_value=fake_instance)

    main_spec = MainAgentSpec(agent_name="main")

    await _register_main_agent(
        pool=pool,
        main_spec=main_spec,
        assembly_deps=PoolAssemblyDeps(),
        system_prompt="",
        safety=RuntimeSafetyPolicy(),
        pool_name="default",
        factory=factory,
        broker=MagicMock(),
        context_manager=MagicMock(),
        bot_model_config=None,
    )

    call_kwargs = factory.create_agent.call_args.kwargs
    descriptor = call_kwargs.get("descriptor") or factory.create_agent.call_args.args[0]
    assert descriptor.roles == []


@pytest.mark.asyncio
async def test_register_main_agent_preserves_multiple_roles() -> None:
    """Multiple roles (preset + custom) round-trip through _register_main_agent."""
    fake_instance = MagicMock()
    fake_instance.pipeline = MagicMock()
    fake_instance.stop = AsyncMock()

    pool = MagicMock()
    pool.register_resident = AsyncMock()

    factory = MagicMock()
    factory.create_agent = AsyncMock(return_value=fake_instance)

    main_spec = MainAgentSpec(
        agent_name="main",
        roles=["coordinator", "communicator", "custom-role"],
    )

    await _register_main_agent(
        pool=pool,
        main_spec=main_spec,
        assembly_deps=PoolAssemblyDeps(),
        system_prompt="",
        safety=RuntimeSafetyPolicy(),
        pool_name="default",
        factory=factory,
        broker=MagicMock(),
        context_manager=MagicMock(),
        bot_model_config=None,
    )

    call_kwargs = factory.create_agent.call_args.kwargs
    descriptor = call_kwargs.get("descriptor") or factory.create_agent.call_args.args[0]
    assert descriptor.roles == ["coordinator", "communicator", "custom-role"]
