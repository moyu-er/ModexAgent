"""System prompt providers — individual sections of the system prompt pipeline.

Each provider wraps one data source and provides versioned, cacheable content.
Providers are ordered by their position in the pipeline list (not by priority).
"""

from __future__ import annotations

import hashlib
import logging
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from modex_agent.core.prompt import SystemPromptProvider
from modex_agent.core.scope import MemoryContext
from modex_agent.core.session_id import SessionInfo, session_id_prefix_of
from modex_agent.memory.injection.archive import (
    ArchiveInjectionConfig,
    ArchiveInjectionSection,
    build_archive_injection_section,
)
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


class PeerCommunicationSystemPromptProvider(SystemPromptProvider):
    """Injects the remote-agent reply contract.

    Fires only when the agent owns ``send_to_agent`` AND at least one of its
    targets is a remote agent (a ``CommunicationTarget`` whose ``bus_ref`` is
    set — the target does not share this agent's bus, so there is no implicit
    reply path). For those targets the agent's ordinary output is invisible
    and a reply is only possible via ``send_to_agent``.

    The contract makes this unmissable without ever naming the underlying
    topology (the agent stays unaware of pools, main-vs-subagent roles, or any
    routing machinery): it only knows some reachable agents require explicit
    sends to receive anything back.

    Replies are OPTIONAL — forcing them would create infinite ping-pong.

    Version is derived from the sorted remote target names so the cache
    invalidates exactly when the reachable set changes.
    """

    def __init__(self, tool_manager: ToolManager | None) -> None:
        super().__init__()
        self._tool_manager = tool_manager

    def _remote_target_names(self) -> list[str]:
        if self._tool_manager is None:
            return []
        tool = self._tool_manager.get_tool("send_to_agent")
        if tool is None:
            return []
        from modex_agent.multi_agent.tools import SendToAgentTool

        if not isinstance(tool, SendToAgentTool):
            return []
        return sorted(
            t.name for t in tool.list_targets() if t.bus_ref is not None
        )

    async def _fetch_version(self) -> str:
        names = self._remote_target_names()
        return "remote-comm:" + ",".join(names) if names else "no-remote-comm"

    async def _fetch_content(self) -> str:
        names = self._remote_target_names()
        if not names:
            return ""
        name_list = "\n".join(f"  - {name}" for name in names)
        return (
            "## Communicating With Remote Agents\n\n"
            "Some agents you can reach via `send_to_agent` cannot see anything "
            "you produce normally — not this reply, not your reasoning, not your "
            "tool output. For these agents the ONLY way they ever hear from you "
            "is a `send_to_agent` call aimed at them.\n\n"
            "Agents that require explicit sends:\n"
            f"{name_list}\n\n"
            "Replies are OPTIONAL. Only call `send_to_agent` back when the sender "
            "actually needs your response — do NOT acknowledge just to be polite, "
            "and do NOT ping-pong. If the incoming message does not require action "
            "from you, end your turn without replying.\n"
        )


class RuntimeProvider(SystemPromptProvider):
    """Runtime metadata — current date/hour and platform. Refreshes hourly."""

    async def _fetch_version(self) -> str:
        return datetime.now(get_user_timezone()).strftime("%Y-%m-%d-%H")

    async def _fetch_content(self) -> str:
        current_time = datetime.now(get_user_timezone()).strftime("%Y-%m-%d %Hh")
        platform_raw = sys.platform
        platform_name = {
            "win32": "Windows",
            "darwin": "macOS",
            "linux": "Linux",
        }.get(platform_raw, platform_raw)
        lines = [
            "## Runtime",
            f"Current Time: {current_time} (hour precision, not exact)",
            f"Platform: {platform_name}",
        ]
        return "\n".join(lines)


class KnowledgeProvider(SystemPromptProvider):
    """Knowledge files (SOUL.md, USER.md, MEMORY.md). Never refreshes during react."""

    def __init__(self, knowledge_xml: str) -> None:
        super().__init__()
        self._knowledge_xml = knowledge_xml

    async def _fetch_version(self) -> str:
        return "static"

    async def _fetch_content(self) -> str:
        return self._knowledge_xml


class SkillProvider(SystemPromptProvider):
    """Skill metadata XML. Never refreshes during react."""

    def __init__(self, skill_xml: str) -> None:
        super().__init__()
        self._skill_xml = skill_xml

    async def _fetch_version(self) -> str:
        return "static"

    async def _fetch_content(self) -> str:
        return self._skill_xml


class ExperienceProvider(SystemPromptProvider):
    """Experience metadata XML. Default: static (extensible for future refresh)."""

    def __init__(self, experience_xml: str) -> None:
        super().__init__()
        self._experience_xml = experience_xml

    async def _fetch_version(self) -> str:
        return "static"

    async def _fetch_content(self) -> str:
        return self._experience_xml


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
    """Backend-neutral archive summaries that refresh when retrieved content changes."""

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

    async def _fetch_version(self) -> str:
        self._section = await build_archive_injection_section(
            self._memory_system,
            self._context,
            self._config,
        )
        return self._section.version

    async def _fetch_content(self) -> str:
        return self._section.content


class PrunedProvider(SystemPromptProvider):
    """Pruned memory catalog. Must refresh on cleanup."""

    def __init__(self, pruned_manager: PrunedManager, session_id: str = "") -> None:
        super().__init__()
        self._manager = pruned_manager
        self._session_id = session_id

    async def _fetch_version(self) -> str:
        try:
            return self._manager.get_version(session_id=self._session_id)
        except Exception:
            return ""

    async def _fetch_content(self) -> str:
        try:
            xml = self._manager.get_injection_xml(session_id=self._session_id)
            return xml or ""
        except Exception:
            return ""


class OutputMdProvider(SystemPromptProvider):
    """Dynamic OUTPUT.md path — computed per-turn from session_id.

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
            "## CRITICAL: Your Output File (OUTPUT.md)\n\n"
            "You MUST write your final deliverable to this exact file "
            "using the `write` tool:\n\n"
            f"  {output_path}\n\n"
            "**Rules — failure to follow these means your work is lost:**\n\n"
            "1. **Write to OUTPUT.md before your final message.** "
            "Use the `write` or `edit` tool with the path above.\n"
            "2. **What you say in conversation is NOT your deliverable.** "
            "Only the content of OUTPUT.md reaches your caller.\n"
            "3. **Do NOT summarise in conversation and skip OUTPUT.md.** "
            "Your caller reads OUTPUT.md, not your chat messages.\n"
            "4. **Write the COMPLETE result** — full analysis, code, or report.\n"
            "5. **You have write access to this path** even if other "
            "directories are read-only. The `write` or `edit` tool works for this file.\n"
            "\n"
            "**Workflow:** do your task → use `write` or `edit` to save OUTPUT.md → "
            "say briefly \"done, see OUTPUT.md\" as your final message."
        )


# ── Subagent per-invocation context (APPEND parent prompt + FORK context) ──
#
# These two move the invocation-specific parts of a subagent's system prompt
# out of the materialize-time baked string. A reused instance (the pool keeps
# one per agent_type) rebuilds them per invocation via load(session_id), so the
# 2nd+ invocation no longer inherits the 1st's parent prompt / fork snapshot.


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
    fork_workspace: Path | None
    template_memory: Any


class AppendParentPromptProvider(SystemPromptProvider):
    """Subagent APPEND mode — prepend the current parent's system prompt.

    Per-invocation: rebuilt every ``load()``. ``parent_session_id`` is the
    authoritative parent for THIS turn (threaded from the dispatch envelope via
    runtime_info, not recovered from a store); ``lookup(parent_session_id)``
    resolves the parent's ``system_prompt_template`` from the in-memory pool (or
    ``None``), so a reused instance reflects each invocation's own parent.
    """

    def __init__(
        self,
        lookup: Callable[[str], Awaitable[str | None]],
        parent_session_id: str,
    ) -> None:
        super().__init__()
        self._lookup = lookup
        self._parent_session_id = parent_session_id

    async def _fetch_version(self) -> str:
        return self._parent_session_id  # refresh when the parent changes

    async def _fetch_content(self) -> str:
        try:
            prompt = await self._lookup(self._parent_session_id)
        except Exception:
            logger.warning(
                "AppendParentPromptProvider: lookup failed for parent %s",
                self._parent_session_id,
                exc_info=True,
            )
            return ""
        return prompt or ""


class ForkContextProvider(SystemPromptProvider):
    """Subagent FORK context — parent history snapshot, per-invocation.

    ``parent_session_id`` is the authoritative parent for this turn (threaded
    from the dispatch envelope via runtime_info). ``ContextForkBuilder.build``
    is idempotent per ``(agent_type, invocation_id)`` (first turn of a session
    writes the fork file, later turns read it), so the per-turn cost is
    bounded. The provider registers the fork file for eviction-driven cleanup
    on first build.
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
                fork_workspace=self._spec.fork_workspace,
                template_memory=self._spec.template_memory,
                subagent_memory_system=self._memory_system,
                parent_name=parent_name,
            )
            if not fork_xml:
                return ""
            if self._spec.fork_workspace is not None:
                self._spec.builder.register_for_cleanup(
                    session_id=self._session_id,
                    fork_workspace=self._spec.fork_workspace,
                    agent_type=self._spec.agent_type,
                    invocation_id=invocation_id,
                )
            return (
                "## Fork Context\n\n"
                f"You are a subagent running from a fork of agent '{parent_name}'.\n"
                "The context below is READ-ONLY reference. Do NOT continue the "
                "prior conversation. Your task starts now.\n\n"
                + str(fork_xml)
            )
        except Exception:
            logger.warning(
                "ForkContextProvider: failed for %s", self._session_id, exc_info=True
            )
            return ""
