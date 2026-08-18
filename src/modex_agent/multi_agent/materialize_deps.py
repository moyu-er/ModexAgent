"""AgentMaterializeDeps — bundled dependencies for AgentTemplate.materialize.

Replaces the ~30 scattered constructor parameters that
AgentCommunicationService used to take to tentatively support subagent
construction. Constructed once at pool wiring time and passed into
AgentTemplate.materialize per call (ADR-0015 D5).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from modex_agent.agents.external.subagent_builder import SubagentExternalBuilder
    from modex_agent.core.llm_struct import RuntimeSafetyPolicy
    from modex_agent.core.session_id import SessionIdFactory
    from modex_agent.core.session_registry import SessionRegistry
    from modex_agent.hook.notification import AgentNotificationService
    from modex_agent.memory.registry import MemoryStoreRegistry
    from modex_agent.messaging.broker import MessageBroker
    from modex_agent.multi_agent.bus import AgentMessageBus
    from modex_agent.multi_agent.context_fork import ContextForkBuilder
    from modex_agent.multi_agent.factory import AgentFactory
    from modex_agent.multi_agent.inbox.consumer import InboxConsumer
    from modex_agent.multi_agent.pool import AgentPool
    from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
    from modex_agent.multi_agent.workspace_paths import WorkspacePathResolver
    from modex_agent.pipeline.adapters import OutputAdapter
    from modex_agent.tools.mcp.registry import McpConnectionRegistry
    from modex_agent.tools.workspace_scoped import WorkspaceRootProvider

from modex_agent.core.capabilities import ModelInfo
from modex_agent.core.constants import ReasoningEffort
from modex_agent.core.emitter import ContentEmitter
from modex_agent.runtime.store import TodoStore


@dataclass(frozen=True)
class AgentMaterializeDeps:
    """Bundled construction deps for AgentTemplate.materialize."""

    agent_factory: AgentFactory
    pool: AgentPool
    session_factory: SessionIdFactory
    broker: MessageBroker
    tree: SessionTreeManager
    safety: RuntimeSafetyPolicy | None = None
    llm_model: str | None = None
    # TODO(model-config-convergence): 模型调用参数 temperature/max_output_tokens 应只由
    # LLMProvider 持有；此处经 descriptor/context 透传属冗余复制。待 ReactLlmClient
    # 不再传这两参后，本字段/参数可连同 AgentContext.temperature/max_output_tokens、
    # AgentLLMConfig、AgentMaterializeDeps 的同名字段一并删除。收敛目标见
    # docs/superpowers/plans/2026-07-03-bot-multi-model.md §框架配置收敛后续。
    llm_temperature: float = 0.7
    llm_max_output_tokens: int | None = None
    llm_reasoning_effort: ReasoningEffort = ReasoningEffort.NONE
    llm_model_info: ModelInfo | None = None
    project_dir: Path | None = None
    notification_service: AgentNotificationService | None = None
    inbox_consumer: InboxConsumer | None = None
    agent_bus: AgentMessageBus | None = None
    output_adapter_factory: Callable[[], OutputAdapter] | None = None
    root_provider: WorkspaceRootProvider | None = None
    session_registry: SessionRegistry | None = None
    on_subagent_created: Callable[[str, str], Awaitable[None]] | None = None
    context_fork_builder: ContextForkBuilder | None = None
    workspace_path_resolver: WorkspacePathResolver | None = None
    mcp_registry: McpConnectionRegistry | None = None
    todo_store: TodoStore | None = None
    subagent_external_builder: SubagentExternalBuilder | None = None
    memory_store_registry: MemoryStoreRegistry | None = None
    """Main agent's ``MemoryStoreRegistry``, threaded to native subagent
    ``build_session_only_memory`` so subagents share the workspace's SQLite
    backend (or file backend) instead of defaulting to a separate
    ``DefaultMemoryStoreRegistry``. ``None`` for framework tests / non-bot
    callers — falls back to file-based per-workspace registry."""
    emitter_factory: Callable[[str], ContentEmitter] | None = None
    """WebUI (or other channel) emitter factory for transcript persistence.

    Injected post-build via ``turn_runner.set_emitter_factory``. The
    ``_create_with_emitter`` wrapper in ``pool_builder`` handles main agents
    and react subagents; ``AgentTemplate._materialize_external`` calls the
    shared ``_inject_emitter_and_pool_context`` helper for external
    subagents (which bypass the wrapper). Both paths converge on the same
    ``set_emitter_factory`` ABC method (architecture rule 15)."""
    control_origin: str = ""
    """Bot HTTP listener origin (e.g. ``http://127.0.0.1:21800``).

    Surfaced as ``MODEX_CONTROL_ORIGIN`` in the native subagent env spec so
    ``modexctl`` can locate the bot's control API. Injected by the business
    layer (``build_control_origin`` in ``bot.config.webui_config``) via
    ``create_pool``; empty string when not configured (framework tests,
    non-bot callers)."""
