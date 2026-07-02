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
    from modex_agent.core.llm_struct import RuntimeSafetyPolicy
    from modex_agent.core.session_id import SessionIdFactory
    from modex_agent.core.session_registry import SessionRegistry
    from modex_agent.hook.notification import AgentNotificationService
    from modex_agent.messaging.broker import MessageBroker
    from modex_agent.multi_agent.bus import AgentMessageBus
    from modex_agent.multi_agent.comm_tracker import CommunicationTracker
    from modex_agent.multi_agent.context_fork import ContextForkBuilder
    from modex_agent.multi_agent.factory import AgentFactory
    from modex_agent.multi_agent.inbox.consumer import InboxConsumer
    from modex_agent.multi_agent.pool import AgentPool
    from modex_agent.multi_agent.workspace_paths import WorkspacePathResolver
    from modex_agent.pipeline.adapters import OutputAdapter
    from modex_agent.tools.workspace_scoped import WorkspaceRootProvider


@dataclass(frozen=True)
class AgentMaterializeDeps:
    """Bundled construction deps for AgentTemplate.materialize."""

    agent_factory: "AgentFactory"
    pool: "AgentPool"
    session_factory: "SessionIdFactory"
    broker: "MessageBroker"
    comm_tracker: "CommunicationTracker | None" = None
    safety: "RuntimeSafetyPolicy | None" = None
    llm_model: str | None = None
    llm_temperature: float = 0.7
    llm_max_tokens: int | None = None
    project_dir: Path | None = None
    notification_service: "AgentNotificationService | None" = None
    inbox_consumer: "InboxConsumer | None" = None
    agent_bus: "AgentMessageBus | None" = None
    output_adapter_factory: "Callable[[], OutputAdapter] | None" = None
    root_provider: "WorkspaceRootProvider | None" = None
    session_registry: "SessionRegistry | None" = None
    on_subagent_created: "Callable[[str, str], Awaitable[None]] | None" = None
    context_fork_builder: "ContextForkBuilder | None" = None
    workspace_path_resolver: "WorkspacePathResolver | None" = None
