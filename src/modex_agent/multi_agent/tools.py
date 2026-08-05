"""Multi-agent communication tool — dynamic description and parameters from CommunicationTargetStore."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import Tool, ToolConfig
from modex_agent.multi_agent.comm_kind import AgentCommKind

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.messaging.broker import MessageBroker
    from modex_agent.multi_agent.address import AgentAddress
    from modex_agent.multi_agent.bus import AgentMessageBus
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


def _current_agent_context() -> AgentContext | None:
    """Return the current ``AgentContext`` from the ``current_agent_context`` contextvar.

    Returns ``None`` when the contextvar is unset. Lazy import keeps the
    module importable without the contextvar dependency at import time.
    Shared by ``SendToAgentTool`` and ``TaskDispatchTool`` so both resolve
    context through one source of truth.
    """
    from modex_agent.core.agent import current_agent_context

    return current_agent_context.get(None)


@dataclass(frozen=True)
class CommunicationTarget:
    name: str
    kind: AgentCommKind
    description: str = ""
    pool_name: str = ""
    bus_ref: AgentMessageBus | None = None
    execution_strategy: ExecutionStrategyKind = ExecutionStrategyKind.REACT


# -- parameter schemas --------------------------------------------------------

_SUBAGENT_DESC_LIMIT = 40


def _truncate_desc(desc: str, limit: int = _SUBAGENT_DESC_LIMIT) -> str:
    """Truncate a subagent description to ``limit`` chars, appending ``...``."""
    if len(desc) <= limit:
        return desc
    return desc[:limit].rstrip() + "..."


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
            "description": (
                "Message content — a continuation of an existing subagent session, "
                "a message to a peer, or a consultation. Not for dispatching new "
                "subagent tasks (use the `task` tool)."
            ),
        },
        "invocation_id": {
            "type": ["string", "null"],
            "description": (
                "Task continuation id. Pass null to start a new task; the tool result "
                "includes an invocation_id to pass back for follow-ups in the same task. "
                "The target's session_id is '{invocation_id}.{target_agent}'."
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
                "The agent that assigned your task — the exact name shown after "
                "'Message from agent' in the system-reminder you received."
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
        if target.name in self._targets:
            existing = self._targets[target.name]
            raise ValueError(
                f"Duplicate communication target name {target.name!r}: "
                f"existing pool={existing.pool_name!r}, "
                f"new pool={target.pool_name!r}. "
                "Target names MUST be unique across all reachable pools."
            )
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

    def get(self, name: str) -> CommunicationTarget | None:
        """Look up a target by name. Returns None if not found.

        In subagent mode the store is single-target (parent); resolves via
        the contextvar just like ``has`` / ``list`` so the lookup cannot
        drift from the dynamic semantics.
        """
        if self._for_subagent:
            parent = self._parent_target()
            return parent if parent is not None and parent.name == name else None
        return self._targets.get(name)

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
            "Communicate with another agent — your ONLY channel for messaging,",
            "continuation, and peer coordination.",
            "",
            "Only `content` reaches the target; your reasoning, tool calls, and",
            "this reply stay local. Sends are asynchronous — end your turn after;",
            "the response comes back on its own. Don't send just to acknowledge.",
            "",
        ]
        if not self._targets:
            lines.append("No targets currently available.")
            return "\n".join(lines)
        lines.extend(
            [
                "Use this tool for:",
                "  - Continuing an existing subagent session (pass invocation_id).",
                "  - Messaging a peer agent as an equal.",
                "  - Replying to a remote agent that sent you a message.",
                "",
                "For dispatching a NEW subagent task, use the `task` tool instead.",
                "",
                "Available targets (use the exact name as target_agent):",
            ]
        )
        subagent_targets = [t for t in self._targets.values() if t.kind == AgentCommKind.SUBAGENT]
        normal_targets = [t for t in self._targets.values() if t.kind == AgentCommKind.NORMAL]
        if subagent_targets:
            lines.append("")
            lines.append("Subagents (for continuing sessions; use the `task` tool for new tasks):")
            for t in subagent_targets:
                entry = f"  - {t.name}"
                if t.description:
                    entry += f": {_truncate_desc(t.description)}"
                lines.append(entry)
        if normal_targets:
            lines.append("")
            lines.append("Peer targets (for messaging and coordination):")
            for t in normal_targets:
                entry = f"  - {t.name}"
                if t.description:
                    entry += f": {t.description}"
                lines.append(entry)
        return "\n".join(lines)

    def _build_subagent(self) -> str:
        parent = self._parent_target()
        lines = [
            "Ask the agent that assigned you this task a clarifying question or for a decision.",
            "",
            "Your task arrived as a system-reminder starting with",
            "\"Message from agent '<PARENT_NAME>':\".",
        ]
        if parent is not None:
            lines.append(
                f"Use that exact {parent.name!r} (the name shown after "
                "'Message from agent') as `target_agent`. It is your only valid target."
            )
        else:
            lines.append("No parent is currently available.")
        lines.extend(
            [
                "",
                "Use this tool ONLY to consult your parent when you cannot proceed without input:",
                '  content: "QUESTION: ..." or "NEED_DECISION: ...".',
                "Then stop and wait — the reply comes back to you as another system-reminder.",
                "",
                "This tool is for consultation, not for returning your result. Your deliverable",
                "still goes to OUTPUT.md (write it there as instructed); nothing you send here",
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
        wakeup_timeout: float = 1.0,
    ) -> None:
        self._store = store
        self._source = source
        self._broker = broker
        self._registry = registry
        self._agent_bus = agent_bus
        self._service = service
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

        context = _current_agent_context()
        if context is None:
            return "Error: no agent context available"

        caller_name = context.session.agent_name
        if caller_name and target_agent == caller_name:
            return (
                f"Error: You are {caller_name!r} — you cannot send a message "
                f"to yourself. Choose a different target."
            )

        target = self._store.get(target_agent)
        if target is None:
            available = ", ".join(t.name for t in self.list_targets())
            return f"Error: '{target_agent}' is not a valid communication target. Available: {available}"

        return await self._service.send_async(
            target=target,
            content=content,
            invocation_id=invocation_id,
            context=context,
        )


# -- task dispatch tool -------------------------------------------------------

_TASK_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_agent": {
            "type": "string",
            "description": (
                "REQUIRED: exact name of the target subagent. "
                "MUST be one of the names listed under 'Available subagents:' "
                "in the tool description."
            ),
        },
        "content": {
            "type": "string",
            "description": (
                "Complete, self-contained task description. The subagent starts "
                "with a fresh context — it cannot see your conversation, "
                "reasoning, or tool results. Include: concrete objective, "
                "relevant context (file paths, constraints), scope (code or "
                "research), expected output, verification method, and boundaries."
            ),
        },
    },
    "required": ["target_agent", "content"],
}


class TaskDispatchTool(Tool):
    """Dispatch a fresh task to a subagent — always invocation_id=None.

    A thin sibling of ``SendToAgentTool`` that only accepts SUBAGENT targets
    and always starts a new subagent session (no continuation). The richer
    description steers the LLM toward high-quality, self-contained task prompts.
    """

    def __init__(
        self,
        *,
        store: CommunicationTargetStore,
        service: AgentCommunicationService,
    ) -> None:
        self._store = store
        self._service = service
        super().__init__(
            name="task",
            parameters=_TASK_PARAMS,
            config=ToolConfig(),
        )

    @property
    def description(self) -> str:
        return self._build_description()

    def _build_description(self) -> str:
        lines = [
            "Dispatch a new task to a subagent. The subagent starts with a fresh context",
            "and runs autonomously — it cannot see your conversation, reasoning, or prior",
            "tool results. Everything it needs must be in `content`.",
            "",
            "When NOT to use this tool:",
            "- If you want to read a specific file, use the read tool directly — it's faster",
            "- If you are searching for a specific pattern, use grep or glob directly",
            "- If no available subagent is a good fit for the task, do it yourself",
            "",
            "When to use:",
            "- Complex, multi-step tasks that need autonomous execution",
            "- Tasks that require a specialized subagent's tools or knowledge",
            "",
            "Usage notes:",
            "1. Launch multiple tasks concurrently when they are independent — use multiple",
            "   tool calls in a single message.",
            "2. Once you delegate work to a subagent, do not duplicate that work yourself.",
            "   Continue with non-overlapping tasks, or end your turn and wait for the result.",
            "3. The subagent's result is returned to you only — relay a concise summary to",
            "   the user if needed.",
            "4. Construct a high-quality task with:",
            "   - TASK: What exactly to do (concrete objective, not a topic)",
            "   - CONTEXT: Relevant file paths, patterns, constraints",
            "   - SCOPE: Write code or just research (search/read/analyze)",
            "   - OUTPUT: Exactly what to return in the final reply",
            "   - VERIFICATION: How to verify (e.g., test commands)",
            "   - BOUNDARIES: What NOT to do, out-of-scope items",
            "5. The subagent's output should generally be trusted.",
            "",
            'A one-line task like "fix the bug" is insufficient — the subagent\'s result',
            "quality is directly proportional to your prompt quality.",
            "",
            "After dispatching, end your turn. You'll be resumed with the result when",
            "the subagent finishes. To CONTINUE an existing session, use send_to_agent",
            "with the invocation_id from a prior task result.",
            "",
        ]
        subagent_targets = [t for t in self._store.list() if t.kind == AgentCommKind.SUBAGENT]
        if not subagent_targets:
            lines.append("No subagents currently available.")
        else:
            lines.append("Available subagents (use the exact name as target_agent):")
            for t in subagent_targets:
                entry = f"  - {t.name}"
                if t.description:
                    entry += f": {t.description}"
                lines.append(entry)
        return "\n".join(lines)

    def list_targets(self) -> list[CommunicationTarget]:
        """Return all targets from the shared store (same as SendToAgentTool)."""
        return self._store.list()

    def get_dynamic_schema(self) -> dict[str, Any]:
        """Return schema with target_agent enum bound to current SUBAGENT targets."""
        schema = super().get_dynamic_schema()
        function = dict(schema.get("function", {}))
        parameters = dict(function.get("parameters", {}))
        properties = dict(parameters.get("properties", {}))

        subagent_names = [t.name for t in self._store.list() if t.kind == AgentCommKind.SUBAGENT]
        if subagent_names and "target_agent" in properties:
            properties["target_agent"] = {
                **properties["target_agent"],
                "enum": subagent_names,
            }

        parameters["properties"] = properties
        function["parameters"] = parameters
        return {**schema, "function": function}

    async def execute(self, **kwargs: Any) -> str:
        target_agent = str(kwargs.get("target_agent", ""))
        content = str(kwargs.get("content", ""))

        context = _current_agent_context()
        if context is None:
            return "Error: no agent context available"

        caller_name = context.session.agent_name
        if caller_name and target_agent == caller_name:
            return (
                f"Error: You are {caller_name!r} — you cannot dispatch a task "
                f"to yourself. Choose a different target."
            )

        target = self._store.get(target_agent)
        if target is None:
            available = ", ".join(
                t.name for t in self._store.list() if t.kind == AgentCommKind.SUBAGENT
            )
            return (
                f"Error: '{target_agent}' is not a valid task target. "
                f"Available subagents: {available}"
            )

        if target.kind != AgentCommKind.SUBAGENT:
            return (
                f"Error: '{target_agent}' is a {target.kind.value} agent — "
                f"task dispatches to subagents only. "
                f"Use send_to_agent for peer communication."
            )

        return await self._service.send_async(
            target=target,
            content=content,
            invocation_id=None,
            context=context,
        )
