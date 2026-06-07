"""Multi-agent communication tool — dynamic description and parameters from CommunicationTargetStore."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from framework.core.tool_manager import Tool, ToolConfig
from framework.multi_agent.comm_kind import AgentCommKind

if TYPE_CHECKING:
    from framework.core.agent import AgentContext
    from framework.messaging.broker import MessageBroker
    from framework.multi_agent.address import AgentAddress
    from framework.multi_agent.bus import AgentMessageBus
    from framework.multi_agent.comm_tracker import CommunicationTracker
    from framework.multi_agent.communication import AgentCommunicationService
    from framework.multi_agent.registry import AgentRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommunicationTarget:
    """A single communicable agent."""

    name: str
    kind: AgentCommKind
    description: str = ""


# -- parameter schemas --------------------------------------------------------

_NORMAL_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_agent": {"type": "string", "description": "Name of the target agent."},
        "content": {"type": "string", "description": "Complete task description with necessary context."},
        "invocation_id": {
            "type": ["string", "null"],
            "description": (
                "Omit or null to start a new task. "
                "To continue an existing session, pass the invocation_id "
                "from the target agent's previous reply."
            ),
        },
    },
    "required": ["target_agent", "content", "invocation_id"],
}

_SUBAGENT_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_agent": {"type": "string", "description": "Your parent agent name."},
        "content": {"type": "string", "description": "Message content."},
    },
    "required": ["target_agent", "content"],
}


class CommunicationTargetStore:
    """Mutable target set with lazy description cache — invalidated on mutation.

    External API: add / pop_by_name / list (returns copy) / has.
    ``description`` and ``parameters`` are derived from current targets
    and cached until the next mutation.
    """

    def __init__(self, *, for_subagent: bool = False) -> None:
        self._targets: dict[str, CommunicationTarget] = {}
        self._for_subagent = for_subagent
        self._description: str | None = None

    # -- mutation -------------------------------------------------------------

    def add(self, target: CommunicationTarget) -> None:
        if target.name not in self._targets:
            self._targets[target.name] = target
            self._description = None

    def pop_by_name(self, name: str) -> None:
        if self._targets.pop(name, None) is not None:
            self._description = None

    # -- query ----------------------------------------------------------------

    def list(self) -> list[CommunicationTarget]:
        return list(self._targets.values())

    def has(self, name: str) -> bool:
        return name in self._targets

    # -- description (dynamic, cached) ----------------------------------------

    @property
    def description(self) -> str:
        if self._description is None:
            self._description = self._build()
        assert self._description is not None
        return self._description

    # -- parameters (derived from for_subagent) -------------------------------

    @property
    def parameters(self) -> dict[str, Any]:
        return _SUBAGENT_PARAMS if self._for_subagent else _NORMAL_PARAMS

    # -- builders -------------------------------------------------------------

    def _build(self) -> str:
        return self._build_subagent() if self._for_subagent else self._build_normal()

    def _build_normal(self) -> str:
        lines = ["Dispatch a task to another agent. Results arrive in your next turn."]
        if not self._targets:
            lines.append("No targets currently available.")
            return "\n".join(lines)
        lines.append("")
        lines.append("Available targets:")
        for t in self._targets.values():
            entry = f"  - {t.name} ({t.kind.value})"
            if t.description:
                entry += f": {t.description}"
            lines.append(entry)
        lines.extend([
            "",
            "Usage:",
            "  target_agent: Name from the list above.",
            "  content: Complete task description with context.",
            "  invocation_id: Omit or null to start a new task. To continue",
            "    an existing session, pass the invocation_id from the agent's last reply.",
            "",
            "Important: Does NOT wait for result. Results arrive in your next turn.",
        ])
        return "\n".join(lines)

    def _build_subagent(self) -> str:
        lines = ["Send a message to your parent agent for coordination."]
        if not self._targets:
            lines.append("No parent available.")
            return "\n".join(lines)
        lines.append("")
        lines.append("Your parent:")
        for t in self._targets.values():
            lines.append(f"  - {t.name}")
        lines.extend([
            "",
            "Usage:",
            "  target_agent: Name from above.",
            '  content: "NEED_DECISION: <question>" for blocking decisions,',
            '    "PROGRESS_UPDATE: <info>" for non-blocking updates.',
            "",
            "Important: You can ONLY message your parent.",
        ])
        return "\n".join(lines)


class SendToAgentTool(Tool):
    """Send a message to another agent — description and parameters from CommunicationTargetStore.

    ``tool.description`` and ``tool.parameters`` read from the store;
    updated automatically when targets change via ``add_target`` / ``pop_target_by_name``.
    """

    def __init__(
        self,
        *,
        store: CommunicationTargetStore,
        source: AgentAddress,
        broker: MessageBroker,
        registry: AgentRegistry,
        agent_bus: AgentMessageBus,
        service: AgentCommunicationService,
        comm_tracker: CommunicationTracker | None = None,
        wakeup_timeout: float = 1.0,
    ) -> None:
        self._store = store
        self._source = source
        self._broker = broker
        self._registry = registry
        self._agent_bus = agent_bus
        self._service = service
        self._comm_tracker = comm_tracker
        self._wakeup_timeout = wakeup_timeout
        super().__init__(
            name="send_to_agent",
            parameters=store.parameters,
            config=ToolConfig(),
        )

    @property
    def description(self) -> str:
        return self._store.description

    # -- target management delegates ------------------------------------------

    def add_target(self, target: CommunicationTarget) -> None:
        self._store.add(target)

    def pop_target_by_name(self, name: str) -> None:
        self._store.pop_by_name(name)

    def list_targets(self) -> list[CommunicationTarget]:
        return self._store.list()

    def has_target(self, name: str) -> bool:
        return self._store.has(name)

    # -- execution ------------------------------------------------------------

    async def execute(self, **kwargs: Any) -> str:
        target_agent = str(kwargs.get("target_agent", ""))
        content = str(kwargs.get("content", ""))
        invocation_id_value = kwargs.get("invocation_id")
        if invocation_id_value is None:
            invocation_id = None
        elif isinstance(invocation_id_value, str) and invocation_id_value.strip().lower() == "null":
            invocation_id = None
        else:
            invocation_id: str | None = str(invocation_id_value)

        if not self.has_target(target_agent):
            available = ", ".join(t.name for t in self.list_targets())
            return f"Error: '{target_agent}' is not a valid communication target. Available: {available}"

        context = self._get_context()
        if context is None:
            return "Error: no agent context available"
        return await self._service.send_async(
            target_agent=target_agent, content=content,
            invocation_id=invocation_id, context=context,
        )

    @staticmethod
    def _get_context() -> AgentContext | None:
        from framework.core.agent import current_agent_context
        return current_agent_context.get(None)
