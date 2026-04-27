from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from framework.pipeline.pipeline import AgentPipeline
    from framework.session.agent_session import AgentSession

logger = logging.getLogger(__name__)

from framework.core.context import ContextManager, InMemoryContextManager
from framework.core.emitter import EmitterConfig
from framework.core.runtime_context import RuntimeContextManager
from framework.core.tool_manager import InMemoryToolManager

try:
    from framework.extensions.llm import LiteLLMProvider
except ImportError:
    LiteLLMProvider = None  # type: ignore[misc,assignment]

from .agent_skill_manager import AgentSkillManager
from .descriptor import AgentDescriptor, AgentInstance
from .filtered_tool_manager import FilteredToolManager
from .inbox.consumer import InboxConsumer
from .inbox.hook import InboxFlushHook
from .inbox.producer import InboxProducer
from .inbox.server import InboxServer

if TYPE_CHECKING:
    pass

if TYPE_CHECKING:
    from framework.core.agent import Agent


class AgentFactory(ABC):
    """Agent 工厂抽象基类。"""

    @abstractmethod
    async def create_agent(
        self,
        descriptor: AgentDescriptor,
        mode: Literal["pipeline", "session", "ephemeral"],
        conversation_id: str | None = None,
        context_manager: ContextManager | None = None,
        broker: Any | None = None,
        tool_manager: InMemoryToolManager | None = None,
        skill_manager: AgentSkillManager | None = None,
        sanitizer: Any | None = None,
        command_interceptor: Any | None = None,
        subagent_manager: Any | None = None,
        hooks: list[Any] | None = None,
        output_adapter: Any | None = None,
        context_manager_factory: Callable[[str], ContextManager] | None = None,
    ) -> AgentInstance:
        """根据描述符和模式创建 AgentInstance。"""
        ...


class DefaultAgentFactory(AgentFactory):
    """默认 Agent 工厂实现。"""

    def __init__(
        self,
        default_llm_provider: Any | None = None,
        default_tool_manager: InMemoryToolManager | None = None,
        skill_manager: AgentSkillManager | None = None,
        sanitizer: Any | None = None,
        command_interceptor: Any | None = None,
        subagent_manager: Any | None = None,
        inbox_server: InboxServer | None = None,
        default_hooks: list[Any] | None = None,
    ):
        self._default_llm_provider = default_llm_provider
        self._default_tool_manager = default_tool_manager
        self._skill_manager = skill_manager
        self._sanitizer = sanitizer
        self._command_interceptor = command_interceptor
        self._subagent_manager = subagent_manager
        self._inbox_server = inbox_server
        self._default_hooks = list(default_hooks) if default_hooks else []
        self._inbox_producer = InboxProducer(inbox_server) if inbox_server else None
        self._inbox_consumer = InboxConsumer(inbox_server) if inbox_server else None
        # Shared runtime-context manager across all agents created by this factory.
        # Per-session isolation is handled internally via SessionScope.
        self._runtime_context_manager = RuntimeContextManager()

    def _resolve_llm_provider(self, descriptor: AgentDescriptor) -> Any:
        if self._default_llm_provider is not None:
            return self._default_llm_provider
        if LiteLLMProvider is None:
            raise ImportError("LiteLLMProvider is not available. Install with: pip install litellm")
        cfg = descriptor.llm_config
        return LiteLLMProvider(
            model=cfg.model or "gpt-4o",
            api_key=None,
            base_url=None,
            temperature=cfg.temperature if cfg.temperature is not None else 0.7,
            max_tokens=cfg.max_tokens,
            reasoning_effort=cfg.reasoning_effort,
        )

    def _get_builder(self, execution_strategy: str):
        """根据 execution_strategy 返回对应的 agent builder 类。"""
        if execution_strategy in ("react", "pipeline"):
            from framework.agents.react.builder import ReActAgentBuilder
            return ReActAgentBuilder
        return None

    def _build_agent(self, descriptor: AgentDescriptor, provider: Any) -> Agent:
        builder = self._get_builder(descriptor.execution_strategy)
        if builder is None:
            msg = f"Unsupported execution_strategy: {descriptor.execution_strategy}"
            raise ValueError(msg)
        return builder.build_agent(descriptor, provider)

    def _resolve_context_manager(
        self,
        descriptor: AgentDescriptor,
        conversation_id: str | None = None,
        provided_context_manager: ContextManager | None = None,
    ) -> ContextManager:
        """根据 descriptor 的 context_strategy 解析合适的 ContextManager。"""
        if provided_context_manager is not None:
            return provided_context_manager
        if descriptor.context_manager is not None:
            return descriptor.context_manager
        if descriptor.context_strategy == "ephemeral":
            from framework.core.context import EphemeralContextManager

            # 每次创建全新的 EphemeralContextManager，不保留历史、不写入文件
            return EphemeralContextManager(
                base_system_prompt=descriptor.system_prompt_template or ""
            )
        # persistent / shared 默认使用标准内存管理器
        return InMemoryContextManager(base_system_prompt=descriptor.system_prompt_template or "")

    async def create_agent(
        self,
        descriptor: AgentDescriptor,
        mode: Literal["pipeline", "session", "ephemeral"],
        conversation_id: str | None = None,
        context_manager: ContextManager | None = None,
        broker: Any | None = None,
        tool_manager: InMemoryToolManager | None = None,
        skill_manager: Any | None = None,
        sanitizer: Any | None = None,
        command_interceptor: Any | None = None,
        subagent_manager: Any | None = None,
        hooks: list[Any] | None = None,
        output_adapter: Any | None = None,
        context_manager_factory: Callable[[str], ContextManager] | None = None,
    ) -> AgentInstance:
        provider = self._resolve_llm_provider(descriptor)
        agent = self._build_agent(descriptor, provider)

        # Context manager
        ctx_mgr = self._resolve_context_manager(descriptor, conversation_id, context_manager)

        # Tool manager with filtering
        tool_mgr = tool_manager or self._default_tool_manager or InMemoryToolManager()
        filtered_tools = FilteredToolManager(
            base=tool_mgr,
            allowed_tools=descriptor.allowed_tools,
            denied_tools=descriptor.denied_tools,
        )

        # Skill manager filtering (wrap if skills configured)
        skill_mgr = skill_manager or self._skill_manager
        if descriptor.allowed_skills is not None and skill_mgr is not None:
            skill_mgr = AgentSkillManager(
                base=skill_mgr,
                allowed_skills=descriptor.allowed_skills,
            )

        pipeline: AgentPipeline | None = None
        session: AgentSession | None = None
        agent_hooks: list[Any] = list(self._default_hooks) + list(hooks or [])

        if mode == "pipeline":
            from framework.pipeline.pipeline import AgentPipeline
            from framework.messaging.broker_bridge import (
                BrokerInputAdapter,
                BrokerOutputAdapter,
            )
            from framework.messaging.broker_memory import InMemoryMessageBroker

            if broker is None:
                logger.warning(
                    "Creating pipeline agent with isolated broker. "
                    "Pass broker= for mesh communication."
                )
                broker = InMemoryMessageBroker()
                await broker.start()
            address = descriptor.address
            input_adapter = BrokerInputAdapter(broker=broker, address=address)
            from framework.pipeline.adapters import OutputAdapter

            if output_adapter is not None and isinstance(output_adapter, OutputAdapter):
                pipe_output_adapter = output_adapter
                emitter_output_adapter = output_adapter
            else:
                pipe_output_adapter = BrokerOutputAdapter(
                    broker=broker,
                    sender=address,
                    default_topic=f"agent:{address.name}:out",
                )
                emitter_output_adapter = BrokerOutputAdapter(
                    broker=broker, sender=address, default_topic=f"agent:{address.name}:out"
                )
            builder = self._get_builder(descriptor.execution_strategy)
            if builder is None:
                raise ValueError(f"Unsupported execution_strategy: {descriptor.execution_strategy}")
            emitter_factory = builder.build_emitter_factory(emitter_output_adapter)
            pipeline = AgentPipeline(
                agent=agent,
                context_manager=ctx_mgr,
                tool_manager=filtered_tools,
                input_adapter=input_adapter,
                output_adapter=pipe_output_adapter,
                emitter_factory=emitter_factory,
                max_iterations=descriptor.max_iterations,
                skill_manager=skill_mgr,
                hooks=agent_hooks,
                context_manager_factory=context_manager_factory,
                runtime_context_manager=self._runtime_context_manager,
                safety=descriptor.safety_policy,
            )
            # Auto-inject InboxFlushHook for pipeline-mode resident agents
            if descriptor.inbox_strategy != "none" and self._inbox_consumer is not None:
                agent_hooks.append(
                    InboxFlushHook(
                        consumer=self._inbox_consumer,
                        agent_name=address.name,
                    )
                )
        elif mode in ("session", "ephemeral"):
            from framework.session.agent_session import AgentSession
            session = AgentSession(
                agent=agent,
                context_manager=ctx_mgr,
                tool_manager=filtered_tools,
                skill_manager=skill_mgr,
                hooks=agent_hooks,
                sanitizer=sanitizer or self._sanitizer,
                command_interceptor=command_interceptor or self._command_interceptor,
                subagent_manager=subagent_manager or self._subagent_manager,
                runtime_context_manager=self._runtime_context_manager,
            )
            # Auto-inject InboxFlushHook for session-mode agents
            if descriptor.inbox_strategy != "none" and self._inbox_consumer is not None:
                agent_hooks.append(
                    InboxFlushHook(
                        consumer=self._inbox_consumer,
                        agent_name=descriptor.address.name,
                    )
                )

        return AgentInstance(
            descriptor=descriptor,
            agent=agent,
            context_manager=ctx_mgr,
            tool_manager=filtered_tools,
            pipeline=pipeline,
            session=session,
            emitter_config=EmitterConfig(),
            hooks=agent_hooks,
        )
