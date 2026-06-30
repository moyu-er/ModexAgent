"""Multi-agent communication tool — dynamic description and parameters from CommunicationTargetStore."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from modex_agent.core.tool_manager import Tool, ToolConfig
from modex_agent.multi_agent.comm_kind import AgentCommKind

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.messaging.broker import MessageBroker
    from modex_agent.multi_agent.address import AgentAddress
    from modex_agent.multi_agent.bus import AgentMessageBus
    from modex_agent.multi_agent.comm_tracker import CommunicationTracker
    from modex_agent.multi_agent.communication import AgentCommunicationService
    from modex_agent.multi_agent.registry import AgentRegistry

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
        "target_agent": {
            "type": "string",
            "description": (
                "REQUIRED: exact name of the target agent. "
                "MUST be one of the names listed under 'Available targets:' in the tool description. "
                "Do not invent names, do not use descriptions as names, and do not guess."
            ),
        },
        "content": {
            "type": "string",
            "description": "Complete task description with necessary context.",
        },
        "invocation_id": {
            "type": ["string", "null"],
            "description": (
                "Pass null to start a new task. The tool result will include an invocation_id. "
                "To continue an existing session, pass that exact invocation_id back. "
                "The target agent's session_id is '{invocation_id}.{target_agent}'."
            ),
        },
    },
    "required": ["target_agent", "content", "invocation_id"],
}

_SUBAGENT_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_agent": {
            "type": "string",
            "description": "Exact name of your parent agent (from the list above).",
        },
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
        lines = [
            "Send a message to another agent: coordinate, ask, reply, or hand off work.",
            "",
            "The target sees nothing you say outside of `content` — put the full message",
            "there. When it finishes, its result comes back to you AUTOMATICALLY as a",
            "completion notification (with a summary and a link to its output); the target",
            "does not need to call anything to reply.",
            "",
            "Use this tool to:",
            "  - exchange information, questions, decisions, or status with another agent;",
            "  - hand off a self-contained subtask when work is better split off to a specialist.",
            "",
            "ASYNCHRONOUS: the target works on its own. Don't block, poll, or read output",
            "files right after calling — wait for the completion notification, then read",
            "the referenced Output file.",
            "",
        ]
        if not self._targets:
            lines.append("No targets currently available.")
            return "\n".join(lines)
        lines.append("Available targets (use the exact name as target_agent):")
        for t in self._targets.values():
            entry = f"  - {t.name} ({t.kind.value})"
            if t.description:
                entry += f": {t.description}"
            lines.append(entry)
        lines.extend(
            [
                "",
                "Parameters:",
                "  target_agent: Exact name from the list above.",
                "  content: The full message or task — self-contained, since it's all the target sees.",
                "  invocation_id: Pass null to start a new exchange. The tool result includes an invocation_id; pass that exact id back to continue the same exchange.",
            ]
        )
        return "\n".join(lines)

    def _build_subagent(self) -> str:
        lines = ["Send a message to your parent agent for coordination."]
        if not self._targets:
            lines.append("No parent available.")
            return "\n".join(lines)
        lines.append("")
        lines.append("Your parent (use the exact name as target_agent):")
        for t in self._targets.values():
            lines.append(f"  - {t.name}")
        lines.extend(
            [
                "",
                "Usage:",
                "  target_agent: Exact name from above.",
                '  content: "NEED_DECISION: <question>" for blocking decisions,',
                '    "PROGRESS_UPDATE: <info>" for non-blocking updates.',
                "",
                "Important: You can ONLY message your parent.",
            ]
        )
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

    def get_dynamic_schema(self) -> dict[str, Any]:
        """Return schema with target_agent enum bound to current available targets."""
        schema = super().get_dynamic_schema()
        function = dict(schema.get("function", {}))
        parameters = dict(function.get("parameters", {}))
        properties = dict(parameters.get("properties", {}))

        target_names = [t.name for t in self.list_targets()]
        if target_names and "target_agent" in properties:
            properties["target_agent"] = {
                **properties["target_agent"],
                "enum": target_names,
            }

        parameters["properties"] = properties
        function["parameters"] = parameters
        return {**schema, "function": function}

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
        if (
            invocation_id_value is None
            or isinstance(invocation_id_value, str)
            and invocation_id_value.strip().lower() == "null"
        ):
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
            target_agent=target_agent,
            content=content,
            invocation_id=invocation_id,
            context=context,
        )

    @staticmethod
    def _get_context() -> AgentContext | None:
        from modex_agent.core.agent import current_agent_context

        return current_agent_context.get(None)
