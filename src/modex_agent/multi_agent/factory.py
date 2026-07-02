from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from modex_agent.control.channel import InMemoryControlChannel

logger = logging.getLogger(__name__)

from modex_agent.core.context import ContextManager, InMemoryContextManager
from modex_agent.core.runtime_context import RuntimeContextManager
from modex_agent.core.session_registry import SessionRegistry
from modex_agent.core.tool_manager import InMemoryToolManager

try:
    from modex_agent.providers import LiteLLMProvider
except ImportError:
    LiteLLMProvider = None  # type: ignore[misc,assignment]

from modex_agent.core.skills.filter import AllowListFilter
from modex_agent.core.skills.manager import SkillManager
from modex_agent.hook import HookRunner
from modex_agent.hook.builtin import InboxFlushHook
from modex_agent.tools.filter import FilteredToolManager

from .comm_kind import AgentCommKind
from .descriptor import AgentDescriptor, AgentInstance
from .inbox.consumer import InboxConsumer
from .inbox.producer import InboxProducer
from .inbox.server import InboxServer

if TYPE_CHECKING:
    pass

if TYPE_CHECKING:
    from modex_agent.core.agent import Agent


class AgentFactory(ABC):
    """Agent 工厂抽象基类。"""

    @abstractmethod
    async def create_agent(
        self,
        descriptor: AgentDescriptor,
        session_id: str | None = None,
        context_manager: ContextManager | None = None,
        broker: Any | None = None,
        tool_manager: InMemoryToolManager | None = None,
        skill_manager: SkillManager | None = None,
        sanitizer: Any | None = None,
        command_interceptor: Any | None = None,
        subagent_service: Any | None = None,
        hooks: list[Any] | None = None,
        output_adapter: Any | None = None,
        context_manager_factory: Callable[[str], ContextManager] | None = None,
    ) -> AgentInstance:
        """根据描述符创建 AgentInstance。"""
        ...


class DefaultAgentFactory(AgentFactory):
    """默认 Agent 工厂实现。"""

    def __init__(
        self,
        default_llm_provider: Any | None = None,
        default_tool_manager: InMemoryToolManager | None = None,
        skill_manager: SkillManager | None = None,
        sanitizer: Any | None = None,
        command_interceptor: Any | None = None,
        subagent_service: Any | None = None,
        inbox_server: InboxServer | None = None,
        default_hooks: list[Any] | None = None,
        default_hook_runner: Any | None = None,
        default_interceptor_chain: Any | None = None,
        default_turn_store: Any | None = None,
        control_channel: InMemoryControlChannel | None = None,
        trace_store: Any | None = None,
        session_registry: SessionRegistry | None = None,
    ) -> None:
        self._default_llm_provider = default_llm_provider
        self._default_tool_manager = default_tool_manager
        self._skill_manager = skill_manager
        self._sanitizer = sanitizer
        self._command_interceptor = command_interceptor
        self._subagent_service = subagent_service
        self._inbox_server = inbox_server
        self._default_hooks = list(default_hooks) if default_hooks else []
        self._default_hook_runner = default_hook_runner
        self._default_interceptor_chain = default_interceptor_chain
        self._default_turn_store = default_turn_store
        self._control_channel = control_channel
        self._trace_store = trace_store
        self._session_registry = session_registry
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
            from modex_agent.agents.react.builder import ReActAgentBuilder

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
        session_id: str | None = None,
        provided_context_manager: ContextManager | None = None,
    ) -> ContextManager:
        """根据 descriptor 的 context_strategy 解析合适的 ContextManager。"""
        if provided_context_manager is not None:
            return provided_context_manager
        if descriptor.context_manager is not None:
            return descriptor.context_manager
        if descriptor.context_strategy == "ephemeral":
            # 每次创建全新的 InMemoryContextManager，不保留历史、不写入文件
            return InMemoryContextManager(
                base_system_prompt=descriptor.system_prompt_template or ""
            )
        # persistent / shared 默认使用标准内存管理器
        return InMemoryContextManager(base_system_prompt=descriptor.system_prompt_template or "")

    async def create_agent(
        self,
        descriptor: AgentDescriptor,
        session_id: str | None = None,
        context_manager: ContextManager | None = None,
        broker: Any | None = None,
        tool_manager: InMemoryToolManager | None = None,
        skill_manager: SkillManager | None = None,
        sanitizer: Any | None = None,
        command_interceptor: Any | None = None,
        subagent_service: Any | None = None,
        hooks: list[Any] | None = None,
        output_adapter: Any | None = None,
        context_manager_factory: Callable[[str], ContextManager] | None = None,
    ) -> AgentInstance:
        provider = self._resolve_llm_provider(descriptor)
        agent = self._build_agent(descriptor, provider)

        # Context manager
        ctx_mgr = self._resolve_context_manager(descriptor, session_id, context_manager)

        # Tool manager with filtering
        tool_mgr = tool_manager or self._default_tool_manager or InMemoryToolManager()
        filtered_tools = FilteredToolManager(
            base=tool_mgr,
            allowed_tools=descriptor.allowed_tools,
            denied_tools=descriptor.denied_tools,
        )

        # Skill manager filtering (rebuild with filter if skills configured)
        # Subagents must NOT inherit the main agent's SkillManager —
        # they either have their own skills or none at all.
        if skill_manager is not None:
            skill_mgr = skill_manager
        elif descriptor.comm_kind != AgentCommKind.SUBAGENT:
            skill_mgr = self._skill_manager
        else:
            skill_mgr = None
        if descriptor.allowed_skills is not None and skill_mgr is not None:
            skill_mgr = SkillManager(
                source=skill_mgr._source,
                skill_filter=AllowListFilter(names=set(descriptor.allowed_skills)),
                builder=skill_mgr._builder,
                cache=skill_mgr._cache,
            )

        agent_hooks: list[Any] = list(self._default_hooks) + list(hooks or [])

        # InboxFlushHook is auto-injected for every agent whose
        # ``inbox_strategy != "none"`` and whose factory has a consumer. It is
        # added to ``pipeline.hook_runner`` AFTER construction (below) — NOT to
        # ``agent_hooks``, because ``AgentPipeline`` stores that list on
        # ``pipeline.hooks``, which the turn loop never dispatches (only
        # ``hook_runner`` is dispatched via ``runtime.hooks``). Putting it on
        # the runner here is the single wiring point for BOTH main and
        # subagent fold-in; pool_builder no longer adds a separate copy.
        auto_inbox_flush = (
            InboxFlushHook(
                consumer=self._inbox_consumer,
                agent_name=descriptor.address.name,
            )
            if descriptor.inbox_strategy != "none" and self._inbox_consumer is not None
            else None
        )

        # Subagent governance: build a lightweight chain (tool chain repair +
        # final legality) from the template's MemoryConfig. Main agents
        # already have governance wired on the pipeline at the pool level
        # (see pool_builder._wire_main_pipeline), so we only build here for
        # subagents. create_subagent_governance handles missing config by
        # returning a default (tool chain repair + final legality) chain.
        subagent_governance: Any | None = None
        if descriptor.comm_kind == AgentCommKind.SUBAGENT:
            from modex_agent.ioc.factories.governance import create_subagent_governance

            subagent_governance = create_subagent_governance(descriptor.memory_config)

        from modex_agent.messaging.broker_bridge import (
            BrokerInputAdapter,
            BrokerOutputAdapter,
        )
        from modex_agent.messaging.broker_memory import InMemoryMessageBroker
        from modex_agent.multi_agent.router import DefaultMeshRouter
        from modex_agent.pipeline.pipeline import AgentPipeline

        if broker is None:
            logger.warning(
                "Creating pipeline agent with isolated broker. Pass broker= for mesh communication."
            )
            broker = InMemoryMessageBroker()
            await broker.start()
        address = descriptor.address
        input_adapter = BrokerInputAdapter(broker=broker, address=address)
        from modex_agent.pipeline.adapters import OutputAdapter

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
        # Each agent needs its own HookRunner so that agent-specific hooks
        # (e.g. SubagentAutoSendHook) don't leak across agents.
        hook_runner = (
            HookRunner(self._default_hook_runner.hook_specs)
            if self._default_hook_runner is not None
            else None
        )
        # Per-agent InterceptorChain copy to prevent cross-agent state leakage.
        # Mirrors the HookRunner copy pattern above.
        agent_interceptor_chain = None
        if self._default_interceptor_chain is not None:
            from modex_agent.interceptor.chain import InterceptorChain

            agent_interceptor_chain = InterceptorChain(self._default_interceptor_chain.interceptors)
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
            hook_runner=hook_runner,
            interceptor_chain=agent_interceptor_chain,
            turn_store=self._default_turn_store,
            context_manager_factory=context_manager_factory,
            runtime_context_manager=self._runtime_context_manager,
            safety=descriptor.safety_policy,
            agent_descriptor=descriptor,
            router=DefaultMeshRouter(session_registry=self._session_registry),
            control_channel=self._control_channel,
            governance=subagent_governance,
        )

        # Auto-inject TraceCollectorHook — ALL agents get per-session trace.
        # The hook reads the store per turn from ctx.runtime.services.trace_store
        # (resolved per turn from the active workspace's pool_data), so it
        # follows workspace switches and works for main agents and subagents
        # uniformly — no special-casing, no distinction.
        from modex_agent.hook import HookErrorPolicy, HookSpec
        from modex_agent.trace import TraceCollectorHook

        # Hooks that must be DISPATCHED (the turn loop only runs hook_runner,
        # never the pipeline.hooks list) are added here as HookSpecs.
        live_hooks = [TraceCollectorHook()]
        if auto_inbox_flush is not None:
            live_hooks.append(auto_inbox_flush)
        if pipeline.hook_runner is not None:
            for hook in live_hooks:
                pipeline.hook_runner.add(
                    HookSpec(hook=hook, on_error=HookErrorPolicy.LOG)
                )
        else:
            pipeline.hooks.extend(live_hooks)

        return AgentInstance(
            descriptor=descriptor,
            context_manager=ctx_mgr,
            pipeline=pipeline,
        )
