"""Multi-agent communication tool — dynamic description and parameters from CommunicationTargetStore."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from modex_agent.core.session_id import SessionInfo
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


def resolve_parent_name(context: "AgentContext | None") -> str | None:
    """Resolve the parent agent NAME for the currently executing subagent.

    Reads ``context.session.parent_session_id`` (populated by the production
    poller-driven path) and extracts the agent_name segment. Returns ``None``
    when there is no context or no recorded parent — callers must treat that
    as "no resolvable parent" (best-effort, never raises).

    Single source of truth: both the subagent ``CommunicationTargetStore``
    (dynamic target menu) and the service-layer topology defense use this so
    they cannot drift apart.
    """
    if context is None:
        return None
    parent_sid = context.session.parent_session_id
    if not parent_sid:
        return None
    return SessionInfo.from_str(parent_sid).agent_name


def _current_parent_name() -> str | None:
    """Resolve the parent name from the ``current_agent_context`` contextvar.

    Returns ``None`` when the contextvar is unset or no parent is recorded.
    Kept as a thin wrapper so ``CommunicationTargetStore`` stays testable
    without depending on the contextvar module at import time.
    """
    from modex_agent.core.agent import current_agent_context

    return resolve_parent_name(current_agent_context.get(None))


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
            "description": (
                "The agent that assigned your task — the exact value of source= "
                "from the <agent_message> you received."
            ),
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

    Two modes:

    - **Normal mode** (``for_subagent=False``, the main agent): targets are a
      static set maintained via ``add`` / ``pop_by_name`` — every NORMAL +
      SUBAGENT agent the main agent can address.
    - **Subagent mode** (``for_subagent=True``): the static dict is ignored.
      The single target — the parent that assigned this task — is resolved
      dynamically at call time from ``current_agent_context``. This is required
      because the tool instance is reused across different invokers, so any
      parent baked at materialize time would go stale. The subagent's
      ``send_to_agent`` is for CONSULTATION only; the deliverable goes to
      OUTPUT.md (enforced elsewhere).
    """

    def __init__(self, *, for_subagent: bool = False) -> None:
        self._targets: dict[str, CommunicationTarget] = {}
        self._for_subagent = for_subagent
        self._description: str | None = None

    # -- mutation -------------------------------------------------------------

    def add(self, target: CommunicationTarget) -> None:
        # In subagent mode the static dict is intentionally unused — the
        # target is resolved dynamically. No-op so callers (e.g. template
        # wiring that doesn't know the mode) stay safe.
        if self._for_subagent:
            return
        if target.name not in self._targets:
            self._targets[target.name] = target
            self._description = None

    def pop_by_name(self, name: str) -> None:
        if self._for_subagent:
            return
        if self._targets.pop(name, None) is not None:
            self._description = None

    # -- query ----------------------------------------------------------------

    def list(self) -> list[CommunicationTarget]:
        if self._for_subagent:
            parent = self._parent_target()
            return [parent] if parent is not None else []
        return list(self._targets.values())

    def has(self, name: str) -> bool:
        if self._for_subagent:
            parent = self._parent_target()
            return parent is not None and parent.name == name
        return name in self._targets

    def _parent_target(self) -> CommunicationTarget | None:
        parent_name = _current_parent_name()
        if parent_name is None:
            return None
        return CommunicationTarget(
            name=parent_name,
            kind=AgentCommKind.NORMAL,
            description="the agent that assigned your task",
        )

    # -- description (dynamic, cached in normal mode only) --------------------

    @property
    def description(self) -> str:
        # In subagent mode the parent is resolved at call time from the
        # contextvar; caching would freeze a parent across different invokers
        # reusing the same tool instance. Normal mode caches as before.
        if self._for_subagent:
            return self._build()
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
            "ASYNCHRONOUS: after sending you may stop and do nothing — end your turn.",
            "Don't poll or read the output files yet. The result comes back on its own as",
            "a completion notification, which resumes you; then read the referenced Output file.",
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
        parent = self._parent_target()
        lines = [
            "Ask the agent that assigned you this task a clarifying question or "
            "for a decision.",
            "",
            "Your task arrived as:",
            '  <agent_message source="<PARENT_NAME>" invocation_id="...">',
            "    <content>...</content>",
            "  </agent_message>",
        ]
        if parent is not None:
            lines.append(
                f"Use that exact {parent.name!r} (the `source` value) as "
                "`target_agent`. It is your only valid target."
            )
        else:
            lines.append("No parent is currently available.")
        lines.extend(
            [
                "",
                "Use this tool ONLY to consult your parent when you cannot proceed "
                "without input:",
                '  content: "QUESTION: ..." or "NEED_DECISION: ...".',
                "Then stop and wait — the reply comes back to you as another "
                "<agent_message>.",
                "",
                "This tool is for consultation, not for returning your result. "
                "Your deliverable",
                "still goes to OUTPUT.md (write it there as instructed); nothing "
                "you send here",
                "counts as your answer.",
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
