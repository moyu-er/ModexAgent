"""Agent pool construction and main-agent registration.

Extracted from ``pool_builder.py`` (ADR-0025 ticket 6 split). Builds the
``AgentPool``, registers the main (NORMAL) agent with factory defaults,
and initializes default long-term memory files.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from bot.service.model_config import BotModelConfig
from modex_agent.core import AgentCommKind
from modex_agent.core.agent import ExecutionStrategyKind
from modex_agent.core.capabilities import ModelInfo
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.memory.default_system import DefaultMemorySystem
from modex_agent.memory.scope import MemoryContext
from modex_agent.messaging import MessageBroker
from modex_agent.multi_agent import (
    AgentPool,
    DefaultAgentFactory,
)
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.pool_config import PoolAssemblyDeps
from modex_agent.persistence.session_registry import SessionRegistry
from modex_agent.persistence.session_store import SessionStore
from modex_agent.scope.spec import AgentSpec

from ..model_config import _resolved_or_placeholder

logger = logging.getLogger(__name__)


async def ensure_long_term_defaults(
    project_dir: Path,
    memory_cfg: MemoryConfig | None,
    memory_system: DefaultMemorySystem,
) -> None:
    """Initialize default long-term memory files if core memory is enabled.

    Supports both old ``long_term`` config (deprecated) and new ``core``
    config. Template paths in config are relative to the project directory.
    Resolves them to absolute paths before calling ``ensure_defaults`` so
    the core memory layer finds templates regardless of CWD (critical after
    ``/cd`` switches the conversation to a different workspace).
    """
    if memory_cfg is None:
        return

    core_enabled = False
    if memory_cfg.long_term is not None and memory_cfg.long_term.enabled:
        core_enabled = True
    if memory_cfg.core is not None and memory_cfg.core.enabled:
        core_enabled = True
    if not core_enabled:
        return

    lt_mgr = memory_system.core_memory_manager
    if lt_mgr is None:
        return

    raw_template_dir: str | None = None
    if memory_cfg.core is not None:
        raw_template_dir = memory_cfg.core.default_templates_dir
    if not raw_template_dir and memory_cfg.long_term is not None:
        raw_template_dir = memory_cfg.long_term.default_templates_dir
    if raw_template_dir:
        abs_template_dir = str((project_dir / raw_template_dir).resolve())
        lt_mgr._config = lt_mgr._config.model_copy(
            update={"default_templates_dir": abs_template_dir}
        )

    defaults: dict[str, str] = {
        "soul": (
            "## 沟通风格\n"
            "- 使用中文回复，风格自然、简洁\n"
            "- 优先给出直接答案，再补充解释\n"
            "- 不确定的事情如实说明，不编造\n"
        ),
        "user": (
            "## 用户画像\n- 首次使用，暂无特定偏好记录\n- 后续对话中会逐渐积累用户习惯和偏好\n"
        ),
        "memory": ("## 相关知识\n- 暂无特定领域知识记录\n- 长期对话中会自动整理和更新\n"),
    }

    ctx = MemoryContext(session_id="default", user_id="default")
    await lt_mgr.ensure_defaults(ctx, defaults)
    logger.info("Long-term memory defaults ensured")


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


async def _register_external_main_agent(
    pool: AgentPool,
    main_spec: AgentSpec,
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
    """Register an external main agent through its strategy-aware factory.

    The external agent is the declared pool's root
    :class:`modex_agent.scope.spec.AgentSpec`; its ``max_steps`` /
    ``execution_strategy`` / ``roles`` / ``description`` are read from
    ``main_spec``.
    """
    from modex_agent.multi_agent.descriptor import (
        AgentDescriptor,
        AgentLLMConfig,
    )

    resolved_cfg = _resolved_or_placeholder(bot_model_config)
    default_resolved = resolved_cfg.default_resolved()
    descriptor = AgentDescriptor(
        address=AgentAddress(name=main_spec.name),
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
        execution_strategy=ExecutionStrategyKind(main_spec.execution_strategy),
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
        skill_resolver=None,
        context_manager=context_manager,
        hooks=[],
        output_adapter=output_adapter,
    )
    await pool.register_resident(descriptor, instance)
    logger.info(
        "Pool '%s': main agent '%s' registered (factory defaults)",
        pool_name,
        main_spec.name,
    )
