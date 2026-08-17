"""Agent pool construction and main-agent registration.

Extracted from ``pool_builder.py`` (ADR-0025 ticket 6 split). Builds the
``AgentPool`` and registers the main (NORMAL) agent with factory defaults.
"""

from __future__ import annotations

import logging
from typing import Any

from bot.service.model_config import BotModelConfig
from modex_agent.core.capabilities import ModelInfo
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.session_registry import SessionRegistry
from modex_agent.core.session_store import SessionStore
from modex_agent.messaging import MessageBroker
from modex_agent.multi_agent import (
    AgentPool,
    DefaultAgentFactory,
)
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.pool_config import PoolAssemblyDeps
from modex_agent.multi_agent.pool_config.specs import MainAgentSpec

from .._assembly_helpers import _resolved_or_placeholder

logger = logging.getLogger(__name__)


def _build_agent_pool(
    broker: Any,
    factory: Any,
    agent_bus: Any,
    inbox_consumer: Any,
    session_factory: Any,
    safety: Any,
    retention: Any,
    pool_name: str,
    *,
    session_registry: SessionRegistry | None = None,
    session_store: SessionStore | None = None,
) -> AgentPool:
    pool = AgentPool(
        broker=broker,
        agent_factory=factory,
        agent_bus=agent_bus,
        inbox_consumer=inbox_consumer,
        session_factory=session_factory,
        safety=safety,
        retention=retention,
        session_registry=session_registry,
        session_store=session_store,
    )
    logger.info("Pool '%s': AgentPool created", pool_name)
    return pool


async def _register_main_agent(
    pool: AgentPool,
    main_spec: MainAgentSpec,
    assembly_deps: PoolAssemblyDeps,
    system_prompt: str,
    safety: RuntimeSafetyPolicy,
    pool_name: str,
    *,
    factory: DefaultAgentFactory,
    broker: MessageBroker,
    context_manager: Any,
    bot_model_config: BotModelConfig | None,
    output_adapter: Any | None = None,
) -> None:
    """Register the main (NORMAL) agent with factory defaults (Design B).

    The normal agent is a plain ``MainAgentSpec`` (inline in ``pool.yml``); its
    ``max_steps`` / ``tool_preset`` / ``tool_supplements`` / ``approval`` /
    ``use_terminal`` / ``terminal_visibility`` are read from ``main_spec``.
    """
    from modex_agent.multi_agent.descriptor import (
        AgentDescriptor,
        AgentLLMConfig,
    )

    resolved_cfg = _resolved_or_placeholder(bot_model_config)
    default_resolved = resolved_cfg.default_resolved()
    descriptor = AgentDescriptor(
        address=AgentAddress(kind="agent", name=main_spec.agent_name),
        llm_config=AgentLLMConfig(
            model=default_resolved.model.model,
            temperature=default_resolved.model.temperature,
            max_output_tokens=default_resolved.model.max_output_tokens,
            reasoning_effort=default_resolved.model.reasoning_effort,
            model_info=ModelInfo(
                model_name=default_resolved.model.model,
                capabilities=default_resolved.capabilities,
            ),
        ),
        system_prompt_template=system_prompt,
        max_iterations=main_spec.max_steps,
        execution_strategy=main_spec.execution_strategy,
        context_strategy="persistent",
        safety_policy=safety,
        comm_kind=AgentCommKind.NORMAL,
        memory_config=assembly_deps.memory,
        roles=list(main_spec.roles),
        role_description=main_spec.description,
    )
    instance = await factory.create_agent(
        descriptor,
        broker=broker,
        tool_manager=None,
        skill_manager=None,
        context_manager=context_manager,
        hooks=[],
        output_adapter=output_adapter,
    )
    await pool.register_resident(descriptor, instance)
    logger.info(
        "Pool '%s': main agent '%s' registered (factory defaults)",
        pool_name,
        main_spec.agent_name,
    )
