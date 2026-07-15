"""ContextForkBuilder — pure-computation fork-context construction (T18).

Extracted from AgentCommunicationService._create_dynamic_subagent (ADR-0015 D5).
T18 simplified the builder to a pure computation: ``build(...)`` queries the
parent session's message history (via the MemorySystem, which under T09
routes through ``MessageStore.load_messages()``), applies lossy compaction,
and returns the XML string directly. No fork XML files are written to disk;
there is no file registry and no ``cleanup``.

``register_for_cleanup`` and ``cleanup`` are retained as no-ops for caller
compatibility (``ForkContextProvider`` and ``AgentPool`` still call them);
they will be removed once those callers are migrated.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from modex_agent.core.message import ChatMessage

if TYPE_CHECKING:
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.ioc.configs.memory import MemoryConfig
    from modex_agent.memory.core.system import MemorySystem

logger = logging.getLogger(__name__)


def _messages_to_xml(messages: list[ChatMessage], parent_name: str) -> str:
    """Convert ChatMessage list to XML for system-prompt injection."""
    lines = [
        f'<forked_context source="{parent_name}">',
        f"  <info>Inherited {len(messages)} messages from parent session.</info>",
    ]
    for i, msg in enumerate(messages):
        role = msg.role
        content = str(msg.content or "")[:2000]
        name_attr = ""
        if role == "tool" and msg.name:
            name_attr = f' name="{msg.name}"'
        lines.append(f'  <message index="{i}" role="{role}"{name_attr}>')
        lines.append(f"    <![CDATA[{content}]]>")
        lines.append("  </message>")
    lines.append("</forked_context>")
    return "\n".join(lines)


class ContextForkBuilder:
    """Builds fork context XML from parent message history (pure computation).

    ``build`` queries the parent session's messages through the subagent
    memory system (the application-facing read path that under T09 routes
    through ``MessageStore.load_messages()``), truncates to the last
    ``fork_max_messages``, optionally applies lossy compaction from the
    template's governance config, and returns the XML string. No files are
    written; the caller passes the string to the subagent's prompt providers.
    """

    async def build(
        self,
        *,
        parent_session: SessionInfo | str,
        agent_type: str,
        invocation_id: str,
        fork_max_messages: int,
        fork_workspace: Path | None,
        template_memory: MemoryConfig | None,
        subagent_memory_system: MemorySystem | None,
        parent_name: str,
    ) -> str | None:
        """Build fork XML from parent message history.

        ``fork_workspace`` is accepted for caller compatibility but no longer
        used (T18 removed file I/O). Returns the placeholder XML when the
        memory system is absent, returns no messages, or raises.
        """
        del fork_workspace  # retained for API compatibility; no file I/O
        fork_xml = (
            f'<forked_context source="{parent_name}">'
            f"  <info>No parent messages available.</info>"
            f"</forked_context>"
        )
        try:
            from modex_agent.core.scope import MemoryContext

            parent_ctx = MemoryContext(session_id=str(parent_session))
            if subagent_memory_system is not None:
                parent_messages = await subagent_memory_system.get_full_history(parent_ctx)
                if parent_messages:
                    truncated = parent_messages[-fork_max_messages:]
                    if (
                        template_memory is not None
                        and template_memory.governance is not None
                        and template_memory.governance.lossy_compaction is not None
                    ):
                        from modex_agent.memory.context_governance import (
                            LossyContentCompactionGovernance,
                        )

                        lc = template_memory.governance.lossy_compaction
                        governor = LossyContentCompactionGovernance(
                            tool_result_head_chars=lc.tool_result_head_chars,
                            assistant_head_chars=lc.assistant_head_chars,
                            agent_head_chars=lc.agent_head_chars,
                            user_head_chars=lc.user_head_chars,
                            tool_args_head_chars=lc.tool_args_head_chars,
                        )
                        msg_dicts: list[dict[str, Any]] = [m.model_dump() for m in truncated]
                        compacted = await governor.apply(msg_dicts)
                        truncated = [ChatMessage(**m) for m in compacted]
                    fork_xml = _messages_to_xml(truncated, parent_name)
            else:
                logger.warning(
                    "Fork context: no memory_system for %s, fork will be empty",
                    agent_type,
                )
        except Exception:
            logger.exception(
                "Fork context: failed to build for %s, continuing with empty", agent_type
            )

        return fork_xml

    def register_for_cleanup(
        self,
        *,
        session_id: str,
        fork_workspace: Path,
        agent_type: str,
        invocation_id: str,
    ) -> None:
        """No-op (T18). Retained for caller compatibility.

        Previously registered a persisted fork file for cleanup on session
        eviction. With file I/O removed, there is nothing to register.
        """
        del session_id, fork_workspace, agent_type, invocation_id

    def cleanup(self, session_id: str) -> None:
        """No-op (T18). Retained for caller compatibility.

        Previously deleted the persisted fork context file for a session.
        With file I/O removed, there is nothing to clean up.
        """
        del session_id
