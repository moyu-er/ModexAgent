"""System prompt providers — individual sections of the system prompt pipeline.

Each provider wraps one data source and provides versioned, cacheable content.
Providers are ordered by their position in the pipeline list (not by priority).
"""

from __future__ import annotations

import hashlib
import logging
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from modex_agent.core.agent import AgentCommKind, AgentContext, current_agent_context
from modex_agent.core.capabilities import Modality, ModelInfo
from modex_agent.core.constants import (
    _NO_DIR_SENTINEL,
    AgentRole,
    format_working_directory_line,
)
from modex_agent.core.prompt import SystemPromptProvider
from modex_agent.core.scope import MemoryContext
from modex_agent.core.session_id import SessionInfo, session_id_prefix_of
from modex_agent.memory.injection.archive import (
    ArchiveInjectionConfig,
    ArchiveInjectionSection,
    build_archive_injection_section,
)
from modex_agent.runtime.enums import TurnCustomKey
from modex_agent.utils.timezone import get_user_timezone

if TYPE_CHECKING:
    from modex_agent.core.tool_manager import ToolManager
    from modex_agent.memory.core.system import MemorySystem
    from modex_agent.memory.pruned.manager import PrunedManager

logger = logging.getLogger(__name__)

_TODO_TASK_DISCIPLINE_PROMPT = """\
## Task Tracking — read before every reply

You own `todo_write` and `todo_read`. The list is the user's window into your
progress, so an out-of-date list is a failure of the task itself, not just
bookkeeping. Two obligations, in this order:

1. **Update BEFORE you reply.** The instant you finish a task — before writing
   any summary or ending the turn — call `todo_write`: mark the finished item
   `completed` and promote the next `pending` item to `in_progress`. Describing
   work as done in prose while the list still shows it `in_progress` is the
   single most common mistake; do not make it.
2. **Never end with stale work.** Do not end your turn while `pending` or
   `in_progress` items remain, unless you are blocked or explicitly waiting on
   the user. If blocked, keep the item `in_progress` and add a `pending` item
   describing the blocker.

On resume / "continue" / "try again": call `todo_read` first, then continue the
`in_progress` item.

Worked example — note that `todo_write` is called at EVERY transition, never
batched to the end:

  user: "Run the tests and fix any failures"
  -> todo_write: [Run tests: in_progress] [Fix failures: pending]
  -> (run tests; 3 failures found)
  -> todo_write: [Run tests: completed] [Fix A: in_progress] [Fix B,C: pending]
  -> (fix A)
  -> todo_write: [Fix A: completed] [Fix B: in_progress] [Fix C: pending]
  -> (fix B, fix C)
  -> todo_write: [Fix A: completed] [Fix B: completed] [Fix C: completed]
  -> "Done — 3 failures fixed, tests green."
"""


class BasePromptProvider(SystemPromptProvider):
    """Static base system prompt (agent personality). Never refreshes."""

    def __init__(self, base_prompt: str) -> None:
        super().__init__()
        self._base_prompt = base_prompt

    async def _fetch_version(self) -> str:
        return "static"

    async def _fetch_content(self) -> str:
        return self._base_prompt


class TodoAwareSystemPromptProvider(SystemPromptProvider):
    """Task-discipline reminder injected only when the agent owns todo tools.

    The version is binary and stable per agent (``todo-enabled`` or ``no-todo``),
    so agents with the same tool set share the same cached prefix. Content is
    gated independently of version so a ``no-todo`` agent emits nothing.
    """

    def __init__(self, tool_manager: ToolManager | None) -> None:
        super().__init__()
        self._tool_manager = tool_manager

    def _has_todo_tools(self) -> bool:
        if self._tool_manager is None:
            return False
        return self._tool_manager.is_registered("todo_read") and (
            self._tool_manager.is_registered("todo_write")
        )

    async def _fetch_version(self) -> str:
        return "todo-enabled" if self._has_todo_tools() else "no-todo"

    async def _fetch_content(self) -> str:
        return _TODO_TASK_DISCIPLINE_PROMPT if self._has_todo_tools() else ""


class _CommSubProvider(ABC):
    """Internal strategy object for ``AgentCommunicationSystemPromptProvider``.

    Each sub-module contributes one communication-context section. Sub-modules
    are NOT independent ``SystemPromptProvider`` instances — the composite
    provider owns the version/cache contract and delegates content/version
    computation to its sub-modules.
    """

    @abstractmethod
    def applies(self) -> bool:
        """Return True if this sub-module's section should be emitted."""

    @abstractmethod
    def version_part(self) -> str:
        """Return the version fragment for this sub-module."""

    @abstractmethod
    def content(self) -> str:
        """Return the prompt section content (empty if N/A)."""


class _PeerCommSubProvider(_CommSubProvider):
    """Remote-agent reply contract — moved from the deleted
    ``PeerCommunicationSystemPromptProvider``.

    Fires when the agent owns ``send_to_peer`` AND at least one target is a
    remote agent (a ``CommunicationTarget`` whose ``tree_ref`` is set — the
    target does not share this agent's bus, so there is no implicit reply
    path). For those targets the agent's ordinary output is invisible and a
    reply is only possible via ``send_to_peer``.
    """

    def __init__(self, tool_manager: ToolManager | None) -> None:
        self._tool_manager = tool_manager

    def _remote_target_names(self) -> list[str]:
        if self._tool_manager is None:
            return []
        tool = self._tool_manager.get_tool("send_to_peer")
        if tool is None:
            return []
        from modex_agent.multi_agent.tools import SendToPeerTool

        if not isinstance(tool, SendToPeerTool):
            return []
        return sorted(t.name for t in tool.list_targets() if t.tree_ref is not None)

    def applies(self) -> bool:
        return bool(self._remote_target_names())

    def version_part(self) -> str:
        names = self._remote_target_names()
        return "peer:" + ",".join(names)

    def content(self) -> str:
        names = self._remote_target_names()
        if not names:
            return ""
        name_list = "\n".join(f"  - {name}" for name in names)
        return (
            "## Communicating With Remote Agents\n\n"
            "Some agents you can reach via `send_to_peer` cannot see anything "
            "you produce normally — not this reply, not your reasoning, not your "
            "tool output. For these agents the ONLY way they ever hear from you "
            "is a `send_to_peer` call aimed at them.\n\n"
            "Agents that require explicit sends:\n"
            f"{name_list}\n\n"
            "Replies are OPTIONAL. Only call `send_to_peer` back when the sender "
            "actually needs your response — do NOT acknowledge just to be polite, "
            "and do NOT ping-pong. If the incoming message does not require action "
            "from you, end your turn without replying.\n"
        )


class _SubagentDispatchSubProvider(_CommSubProvider):
    """[DEPRECATED] Main-agent subagent dispatch contract.

    Effective information was fully covered by TaskDispatchTool.description;
    the NEED_DECISION/PROGRESS_UPDATE prefix contract was unfulfilled
    (subagents were never instructed to use these prefixes in final results).
    Retained for reference.

    Originally fired when the agent was NOT a subagent (``comm_kind`` is
    ``None`` or ``NORMAL``) AND the agent owned the ``task`` tool AND at
    least one target was a subagent (``kind == SUBAGENT``). Main agents
    were constructed with ``comm_kind=None`` (the default), so the check
    used ``== SUBAGENT`` rather than ``!= NORMAL`` to treat ``None`` as
    main/normal.
    """

    def __init__(
        self,
        tool_manager: ToolManager | None,
        comm_kind: AgentCommKind | None,
    ) -> None:
        self._tool_manager = tool_manager
        self._comm_kind = comm_kind

    def _subagent_target_names(self) -> list[str]:
        if self._tool_manager is None:
            return []
        tool = self._tool_manager.get_tool("task")
        if tool is None:
            return []
        from modex_agent.multi_agent.tools import TaskDispatchTool

        if not isinstance(tool, TaskDispatchTool):
            return []
        return sorted(t.name for t in tool.list_targets() if t.kind == AgentCommKind.SUBAGENT)

    def applies(self) -> bool:
        return False

    def version_part(self) -> str:
        names = self._subagent_target_names()
        return "dispatch:" + ",".join(names)

    def content(self) -> str:
        return (
            "## Dispatching Subagents\n\n"
            "Subagents cannot see anything you output directly. To assign a NEW task,\n"
            "use the `task` tool — its `content` parameter carries the full task\n"
            "description, and the tool guides you to construct a high-quality prompt.\n\n"
            "To CONTINUE an existing subagent session (e.g. after receiving a\n"
            "NEED_DECISION response), use `task` with the `invocation_id`\n"
            "from the prior task result.\n\n"
            "After dispatching, end your turn — the notification resumes you with the\n"
            "result when the subagent finishes.\n\n"
            "Subagents surface structured prefixes in their delivered result:\n"
            "- `NEED_DECISION: <question>` — needs your decision. Continue the session\n"
            "  (task with same invocation_id) with your answer.\n"
            "- `PROGRESS_UPDATE: <info>` — informational, no action needed.\n"
        )


class _SubagentConsultationSubProvider(_CommSubProvider):
    """SUBAGENT consultation contract — ask parent for input via
    ``send_to_agent``.

    Fires when ``comm_kind == SUBAGENT``.
    """

    def __init__(
        self,
        tool_manager: ToolManager | None,
        comm_kind: AgentCommKind | None,
    ) -> None:
        self._tool_manager = tool_manager
        self._comm_kind = comm_kind

    def applies(self) -> bool:
        return self._comm_kind == AgentCommKind.SUBAGENT

    def version_part(self) -> str:
        return "consult"

    def content(self) -> str:
        return (
            "## Consulting Your Parent\n\n"
            "Use `send_to_agent` only to ask your parent a question or request a "
            "decision when you cannot proceed without input. Do not use it to report "
            "results or progress."
        )


class AgentCommunicationSystemPromptProvider(SystemPromptProvider):
    """Composite provider for agent-communication context.

    Replaces the deleted ``PeerCommunicationSystemPromptProvider`` with a
    two-part contract whose applicability depends on the agent's topology
    (``comm_kind``) and the shape of its ``send_to_agent`` target set:

    - ``_PeerCommSubProvider`` — remote-agent reply contract (peer targets
      whose ``tree_ref`` is set).
    - ``_SubagentConsultationSubProvider`` — SUBAGENT consultation contract
      (ask parent for input via ``send_to_agent``).

    The deprecated ``_SubagentDispatchSubProvider`` is retained in the module
    but no longer instantiated — its effective content was fully covered by
    ``TaskDispatchTool.description``.

    The composite provider owns the version/cache contract; sub-modules are
    internal strategy objects that contribute version fragments and content
    sections. Version is ``"comm:"`` + ``|``-joined fragments of all applying
    sub-modules (``"comm:none"`` if none apply); content is ``\\n\\n``-joined
    sections of all applying sub-modules, empty strings skipped (``""`` if
    none apply).
    """

    def __init__(
        self,
        tool_manager: ToolManager | None,
        comm_kind: AgentCommKind | None,
    ) -> None:
        super().__init__()
        self._sub_providers: list[_CommSubProvider] = [
            _PeerCommSubProvider(tool_manager),
            _SubagentConsultationSubProvider(tool_manager, comm_kind),
        ]

    async def _fetch_version(self) -> str:
        parts = [sub.version_part() for sub in self._sub_providers if sub.applies()]
        return "comm:" + "|".join(parts) if parts else "comm:none"

    async def _fetch_content(self) -> str:
        sections = [sub.content() for sub in self._sub_providers if sub.applies()]
        return "\n\n".join(s for s in sections if s)


class RuntimeProvider(SystemPromptProvider):
    """Runtime metadata for the current turn — date/hour, platform, and working directory.

    The working directory is upstream-injected from ``InputMessage.workspace``;
    when absent, the directory section is omitted entirely. The version key
    includes a directory hash so the prompt cache refreshes on workspace change.
    """

    def __init__(self, working_directory: Path | None = None) -> None:
        super().__init__()
        self._working_directory = working_directory

    async def _fetch_version(self) -> str:
        hour = datetime.now(get_user_timezone()).strftime("%Y-%m-%d-%H")
        if self._working_directory is None:
            return f"{hour}:{_NO_DIR_SENTINEL}"
        directory_version = hashlib.md5(str(self._working_directory).encode()).hexdigest()[:16]
        return f"{hour}:{directory_version}"

    async def _fetch_content(self) -> str:
        platform_raw = sys.platform
        platform_name = {
            "win32": "Windows",
            "darwin": "macOS",
            "linux": "Linux",
        }.get(platform_raw, platform_raw)
        lines = [
            "## Runtime",
            f"Platform: {platform_name}",
        ]
        dir_line = format_working_directory_line(self._working_directory)
        if dir_line is not None:
            lines.append(dir_line)
        return "\n".join(lines)


class ModelInfoProvider(SystemPromptProvider):
    """Declares the active model's perceptual capabilities to the agent.

    ``ModelInfo`` is supplied at construction (per-turn, threaded from
    ``runtime_info[RuntimeInfoKey.MODEL_INFO]`` via ``assemble_context`` →
    ``load``). Content is a generic capability declaration; tools that
    behave differently per modality (e.g. ``read`` returning image content)
    are mentioned as examples, not bound. Emits nothing when ``model_info``
    is ``None``.
    """

    def __init__(self, model_info: ModelInfo | None) -> None:
        super().__init__()
        self._model_info = model_info

    async def _fetch_version(self) -> str:
        if self._model_info is None:
            return "model:none"
        modalities = ",".join(sorted(m.value for m in self._model_info.capabilities.modalities))
        return f"model:{self._model_info.model_name}:{modalities}"

    async def _fetch_content(self) -> str:
        if self._model_info is None:
            return ""
        caps = self._model_info.capabilities
        if caps.supports(Modality.IMAGE):
            return (
                "## Your Capabilities\n\n"
                "You can perceive images. Tools that return image content "
                "(e.g. `read` on an image file) deliver it directly to you."
            )
        return (
            "## Your Capabilities\n\n"
            "You cannot perceive images. Tools that would return image "
            "content (e.g. `read` on an image file) will instead return a "
            "text notice."
        )


class CoreMemoryProvider(SystemPromptProvider):
    """Core memory files (SOUL.md, USER.md, MEMORY.md). Never refreshes during react."""

    def __init__(self, core_memory_xml: str) -> None:
        super().__init__()
        self._core_memory_xml = core_memory_xml

    async def _fetch_version(self) -> str:
        return "static"

    async def _fetch_content(self) -> str:
        return self._core_memory_xml


# Moved to ``core.skills.provider`` to avoid a ``core → memory`` reverse
# import when ``SkillManager.build_provider()`` constructs it. Re-exported
# here for callers that import all providers from this module.
from modex_agent.core.skills.provider import SkillProvider  # noqa: E402,F401


class ExperienceProvider(SystemPromptProvider):
    """Experience metadata XML. Default: static (extensible for future refresh)."""

    def __init__(self, experience_xml: str) -> None:
        super().__init__()
        self._experience_xml = experience_xml

    async def _fetch_version(self) -> str:
        return "static"

    async def _fetch_content(self) -> str:
        return self._experience_xml


_AGENT_ROLE_CONTRACT_REVIEWER = """\
## Role Contract — Reviewer

You are a verification role. Your final reply MUST contain a \
<verification status="passed|failed" reason="brief justification"/> tag. \
Use `status="passed"` only when the reviewed change is correct, complete, and \
verified. Use `status="failed"` when issues remain. The coordinator relies on \
this tag to decide next steps."""

_AGENT_ROLE_CONTRACT_IMPLEMENTER = """\
## Role Contract — Implementer

You are an implementation role. After any code modification, you MUST run \
verification commands (tests, lint, build, or typecheck as appropriate) or \
explicitly explain why verification cannot be run. Declaring a task complete \
without verification is a failure mode."""

_AGENT_ROLE_CONTRACT_COORDINATOR = """\
## Role Contract — Coordinator

You are a coordinator role. Verification-role agents report back with a \
<verification status="passed|failed" reason="..."/> tag. When status is \
`failed`, you MUST dispatch the implementation role to fix the issues — do not \
end your turn with unresolved failures. Max 2 review cycles before escalating \
to the user."""

_AGENT_ROLE_CONTRACT_PLANNER = """\
## Role Contract — Planner

You are a planning role. Produce concrete, step-by-step implementation plans. \
Identify files to touch, patterns to follow, and risks. Do not implement — \
hand off to the implementation role."""

_AGENT_ROLE_CONTRACT_SCOUT = """\
## Role Contract — Scout

You are an exploration role. Map relevant files, patterns, and constraints. \
Report findings concisely. Do not implement or modify code."""

_AGENT_ROLE_CONTRACT_ORACLE = """\
## Role Contract — Oracle

You are a consulting role. Provide architecture and design reasoning. Weigh \
tradeoffs, identify edge cases, recommend approaches. Do not implement."""

_AGENT_ROLE_CONTRACT_COMMUNICATOR = """\
## Role Contract — Communicator

You are a communication role. Relay information between agents or to the user \
accurately and concisely. Do not modify code or make decisions."""

_AGENT_ROLE_CONTRACTS: dict[str, str] = {
    AgentRole.REVIEWER.value: _AGENT_ROLE_CONTRACT_REVIEWER,
    AgentRole.IMPLEMENTER.value: _AGENT_ROLE_CONTRACT_IMPLEMENTER,
    AgentRole.COORDINATOR.value: _AGENT_ROLE_CONTRACT_COORDINATOR,
    AgentRole.PLANNER.value: _AGENT_ROLE_CONTRACT_PLANNER,
    AgentRole.SCOUT.value: _AGENT_ROLE_CONTRACT_SCOUT,
    AgentRole.ORACLE.value: _AGENT_ROLE_CONTRACT_ORACLE,
    AgentRole.COMMUNICATOR.value: _AGENT_ROLE_CONTRACT_COMMUNICATOR,
}


class AgentRoleContractProvider(SystemPromptProvider):
    """Injects role-specific runtime contracts based on the agent's ``roles``.

    For each preset role present in ``roles``, the provider appends a short
    contract segment shaping the agent's behavior (e.g. REVIEWER must emit a
    ``<verification status="passed|failed" .../>`` tag; IMPLEMENTER must run
    verification after code changes). Unrecognized role strings are ignored
    silently — the provider injects nothing for them and does not error.

    Version is derived from the sorted set of recognized roles so the cache
    invalidates exactly when the recognized role set changes. Unrecognized
    roles do not affect the version (they contribute no content). Order of
    ``roles`` does not affect the version, but content order follows the
    input list so callers can shape prompt ordering deterministically.
    """

    def __init__(self, roles: list[str]) -> None:
        super().__init__()
        self._roles: list[str] = list(roles)

    async def _fetch_version(self) -> str:
        recognized = sorted(r for r in self._roles if r in _AGENT_ROLE_CONTRACTS)
        return f"roles:{','.join(recognized)}" if recognized else "roles:none"

    async def _fetch_content(self) -> str:
        parts: list[str] = []
        for role in self._roles:
            contract = _AGENT_ROLE_CONTRACTS.get(role)
            if contract is not None:
                parts.append(contract)
        return "\n\n---\n\n".join(parts)


class ProviderBlocksProvider(SystemPromptProvider):
    """Static blocks from memory providers. Refreshes on content hash change."""

    def __init__(self, blocks: list[str]) -> None:
        super().__init__()
        self._blocks = blocks

    async def _fetch_version(self) -> str:
        combined = "\n".join(self._blocks)
        if not combined:
            return "empty"
        return hashlib.md5(combined.encode()).hexdigest()[:16]

    async def _fetch_content(self) -> str:
        return "\n\n".join(self._blocks)


class ProviderPrefetchProvider(SystemPromptProvider):
    """Provider prefetch results. Refreshes when query changes."""

    def __init__(self, query: str, prefetch_content: str = "") -> None:
        super().__init__()
        self._query = query
        self._prefetch_content = prefetch_content

    async def _fetch_version(self) -> str:
        if not self._query:
            return "no-query"
        return hashlib.md5(self._query.encode()).hexdigest()[:16]

    async def _fetch_content(self) -> str:
        if not self._prefetch_content:
            return ""
        from modex_agent.utils.xml import xml_text

        return f"<related_facts>\n{xml_text(self._prefetch_content)}\n</related_facts>"


class ArchiveProvider(SystemPromptProvider):
    """Backend-neutral archive summaries that refresh when retrieved content changes.

    The version check is TTL-cached (``_VERSION_TTL_SECONDS``) to avoid
    rebuilding the injection section on every LLM iteration. The section is
    rebuilt only when the TTL expires and the version has actually changed.

    TODO(mid-turn-archive-refresh): if cleanup generates a new archive mid-turn
    within the TTL window, the agent won't see it until the TTL expires. Accepted
    trade-off — the 5s window is short relative to typical turn duration.
    """

    _VERSION_TTL_SECONDS: float = 5.0

    def __init__(
        self,
        memory_system: MemorySystem,
        context: MemoryContext,
        config: ArchiveInjectionConfig | None = None,
    ) -> None:
        super().__init__()
        self._memory_system = memory_system
        self._context = context
        self._config = config or ArchiveInjectionConfig()
        self._section = ArchiveInjectionSection(version="0", content="")
        self._version_cached_at: float = 0.0
        self._cached_version: str = ""

    async def _fetch_version(self) -> str:
        now = time.monotonic()
        if now - self._version_cached_at < self._VERSION_TTL_SECONDS:
            return self._cached_version
        self._section = await build_archive_injection_section(
            self._memory_system,
            self._context,
            self._config,
        )
        self._cached_version = self._section.version
        self._version_cached_at = now
        return self._cached_version

    async def _fetch_content(self) -> str:
        return self._section.content


class PrunedProvider(SystemPromptProvider):
    """Pruned memory catalog. Must refresh on cleanup.

    The version check is TTL-cached (``_VERSION_TTL_SECONDS``) to avoid
    querying the manager on every LLM iteration. The version is rechecked
    only when the TTL expires.

    TODO(mid-turn-pruned-refresh): if cleanup updates the pruned catalog mid-turn
    within the TTL window, the agent won't see it until the TTL expires. Accepted
    trade-off — the 5s window is short relative to typical turn duration.
    """

    _VERSION_TTL_SECONDS: float = 5.0

    def __init__(self, pruned_manager: PrunedManager, session_id: str = "") -> None:
        super().__init__()
        self._manager = pruned_manager
        self._session_id = session_id
        self._version_cached_at: float = 0.0
        self._cached_version: str = ""

    async def _fetch_version(self) -> str:
        now = time.monotonic()
        if now - self._version_cached_at < self._VERSION_TTL_SECONDS:
            return self._cached_version
        try:
            self._cached_version = self._manager.get_version(session_id=self._session_id)
        except Exception:
            self._cached_version = ""
        self._version_cached_at = now
        return self._cached_version

    async def _fetch_content(self) -> str:
        try:
            xml = self._manager.get_injection_xml(session_id=self._session_id)
            return xml or ""
        except Exception:
            return ""


class OutputMdProvider(SystemPromptProvider):
    """[DEPRECATED] Dynamic OUTPUT.md path — computed per-turn from session_id.

    OutputMdProvider is no longer registered (subagent deliverable is now
    reply-text-based, hook owns file writes). Retained for reference.

    Every invocation gets the correct OUTPUT.md path, regardless of
    whether the subagent is pool-reused or freshly created.
    """

    def __init__(self, output_base_dir: Path, session_id: str) -> None:
        super().__init__()
        self._output_base_dir = output_base_dir
        self._session_id = session_id

    async def _fetch_version(self) -> str:
        return self._session_id  # per-session → always refresh on session change

    async def _fetch_content(self) -> str:
        output_path = self._output_base_dir / self._session_id / "OUTPUT.md"
        return (
            "## Output\n\n"
            f"Write your final deliverable to `{output_path}` using the `write` or `edit` tool. "
            "Your caller reads OUTPUT.md, not your conversation messages — "
            "if it isn't written there, your work is lost. "
            'After writing, say briefly "done, see OUTPUT.md".'
        )


@dataclass(frozen=True)
class ForkContextSpec:
    """Wiring holder for ForkContextProvider.

    Frozen dataclass (type-safety rule 11 escape hatch): a tuple-like internal
    record that crosses the materialize → MemorySystemContextManager seam. It
    holds a mutable builder; no runtime field validation is required, and it
    carries no behaviour beyond field access. ``memory_system`` and the parent
    session are deliberately NOT here — the parent arrives per turn via
    ``runtime_info`` (threaded from the dispatch envelope), and
    ``memory_system`` is injected by ``load()`` from the context manager's own
    memory system to avoid a construction cycle.
    """

    builder: Any
    agent_type: str
    fork_max_messages: int


class ForkContextProvider(SystemPromptProvider):
    """Subagent FORK context — parent history snapshot, per-invocation.

    ``parent_session_id`` is the authoritative parent for this turn (threaded
    from the dispatch envelope via runtime_info). ``ContextForkBuilder.build``
    queries the parent session's message history and returns the fork XML;
    the provider wraps it in a READ-ONLY reference header.
    """

    def __init__(
        self,
        spec: ForkContextSpec,
        session_id: str,
        memory_system: MemorySystem,
        parent_session_id: str,
    ) -> None:
        super().__init__()
        self._spec = spec
        self._session_id = session_id
        self._memory_system = memory_system
        self._parent_session_id = parent_session_id

    async def _fetch_version(self) -> str:
        return self._session_id  # refresh on session change

    async def _fetch_content(self) -> str:
        try:
            parent = SessionInfo.from_str(self._parent_session_id)
            parent_name = str(parent).split(".")[-1]
            invocation_id = session_id_prefix_of(self._session_id)
            fork_xml = await self._spec.builder.build(
                parent_session=parent,
                agent_type=self._spec.agent_type,
                invocation_id=invocation_id,
                fork_max_messages=self._spec.fork_max_messages,
                subagent_memory_system=self._memory_system,
                parent_name=parent_name,
            )
            if not fork_xml:
                return ""
            return (
                "## Fork Context\n\n"
                f"You are a subagent running from a fork of agent '{parent_name}'.\n"
                "The context below is READ-ONLY reference. Do NOT continue the "
                "prior conversation. Your task starts now.\n\n" + str(fork_xml)
            )
        except Exception:
            logger.warning("ForkContextProvider: failed for %s", self._session_id, exc_info=True)
            return ""


class GraphWorkflowProvider(SystemPromptProvider):
    """Graph workflow guidance — deliver-tool routing instructions.

    Fires only when the agent is a graph node main agent (``is_node_execution``
    + ``graph_context`` both set). This is the upper layer: graph mode sits
    above the normal/subagent split, so subagents — even in graph mode —
    never receive graph workflow content. They are atomic agents whose
    results flow back to the parent graph node.

    In normal sessions ``graph_context`` is ``None`` so version is
    ``no-graph`` and content is empty — the pipeline skips it entirely.

    Gate: ``_is_graph_node_execution(ctx)`` checks ``graph_context`` is set
    AND ``GRAPH_TOPOLOGY_CONTEXT`` state key exists (set only by
    ``GraphTopologyConfigurator`` whose gate is ``is_node_execution and
    NORMAL``). This excludes subagents who have ``graph_context`` but no
    topology key.

    Configuration matrix (see ``docs/design/session-tree/layered-config-matrix.md``):

    | Mode                  | GraphWorkflowProvider |
    |-----------------------|-----------------------|
    | native main session   | empty (no-graph)      |
    | native main graph     | full content          |
    | native sub session    | empty                 |
    | native sub graph      | empty (excluded)      |
    | external (any)        | not used (no pipeline)|
    """

    async def _fetch_version(self) -> str:
        ctx = _get_agent_context()
        if not _is_graph_node_execution(ctx):
            return "no-graph"
        return "graph"

    async def _fetch_content(self) -> str:
        ctx = _get_agent_context()
        if not _is_graph_node_execution(ctx):
            return ""
        assert ctx is not None  # narrowed by _is_graph_node_execution
        from modex_agent.runtime.enums import TurnCustomKey

        parts: list[str] = ["## Graph Node Context\n"]

        parts.append("### Workflow Guidance\n\n")
        parts.append(
            "You are a node in a graph workflow. Your regular text output "
            "is NOT delivered to anyone — it stays in your local context "
            "only. The ONLY way to route your work to downstream nodes is "
            "the `deliver` tool.\n\n"
            "You MUST call `deliver` before finishing. Check the `deliver` "
            "tool description for available targets and their roles.\n\n"
            "**Deliver Content Guidelines**\n\n"
            "Your deliver `content` is the ONLY information downstream nodes "
            "receive from you. They cannot see your reasoning, tool calls, "
            "or intermediate steps. Write it as a handoff to the next "
            "agent — enough context to continue, not a full transcript.\n\n"
            "**Pattern 1 — Producer** (you produce new work):\n"
            "- Task: What you were asked to do (one or two sentences).\n"
            "- Result: What you produced or found. Reference files by "
            "path instead of pasting full content. Include key decisions "
            "and their rationale.\n"
            "- Status: Done / partial / blocked. If partial, state "
            "what remains. If blocked, state the obstacle.\n\n"
            "**Pattern 2 — Relay** (you selectively pass upstream content "
            "downstream):\n"
            "Use this when your role is to filter and summarize upstream "
            "content for downstream nodes, not to produce new content. Do "
            "NOT forward upstream content verbatim — select and transform it.\n"
            "- Source: Which upstream node(s) this content is derived from.\n"
            "- Selection: What you included and why it's relevant to the "
            "downstream node.\n"
            "- Summary: The filtered/summarized content, written for the "
            "downstream node's needs.\n"
            "- Omitted: What you excluded (briefly — so downstream knows "
            "what's missing).\n\n"
            "If you deliver multiple times, later delivers can be short "
            "fragments — but your final deliver should be self-contained "
            "enough for the downstream node to act on without re-reading "
            "your inputs.\n"
        )

        if ctx.runtime is not None and ctx.runtime.state is not None:
            topology = ctx.runtime.state.custom.get(TurnCustomKey.GRAPH_TOPOLOGY_CONTEXT)
            if topology:
                parts.append("### Topology\n\n")
                parts.append(topology)
                parts.append("\n")
            desc = ctx.runtime.state.custom.get(TurnCustomKey.GRAPH_NODE_DESCRIPTION)
            if desc:
                parts.append("\n### Your Role\n\n")
                parts.append(desc)
            knowledge_dir = ctx.runtime.state.custom.get(TurnCustomKey.GRAPH_KNOWLEDGE_DIR)
            if knowledge_dir:
                parts.append("\n### Knowledge Base\n\n")
                parts.append(
                    "A shared knowledge base is available via the `knowledge_base` "
                    "tool. It lets you record findings, decisions, and questions that "
                    "other nodes can read, even if they are not directly downstream "
                    "from you.\n\n"
                    "Use it to:\n"
                    "- Record important discoveries in `findings` (append by convention).\n"
                    "- Log key decisions in `decisions` (append by convention).\n"
                    "- Track unresolved questions in `open_questions`.\n"
                    "- Use `grep` to check if a topic has already been recorded before "
                    "writing a duplicate entry.\n"
                    "- The `changelog` is auto-maintained - do not write to it directly.\n\n"
                    "A summary of recent findings and open questions is injected at the "
                    "start of each turn. Use the tool for full content or searches.\n"
                )

        return "".join(parts)


def _get_agent_context() -> AgentContext | None:
    return current_agent_context.get(None)


def _is_graph_node_execution(ctx: AgentContext | None) -> bool:
    """True when this turn is a graph node main agent execution.

    Graph mode is the upper layer: it requires both ``graph_context`` (the
    graph runtime is active) and the ``GRAPH_TOPOLOGY_CONTEXT`` state key
    (set only by ``GraphTopologyConfigurator`` whose gate is
    ``is_node_execution and agent_kind == NORMAL``). Subagents — even in
    graph mode — never have the topology key, so they are excluded.

    This keeps graph workflow content (deliver guidance, topology, knowledge
    base instructions) strictly on graph node main agents, while subagents
    remain atomic agents regardless of whether they run inside a graph.
    """
    if ctx is None or ctx.graph_context is None:
        return False
    if ctx.runtime is None or ctx.runtime.state is None:
        return False
    return ctx.runtime.state.custom.get(TurnCustomKey.GRAPH_TOPOLOGY_CONTEXT) is not None
