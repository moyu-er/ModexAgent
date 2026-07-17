"""ContextForkBuilder — pure-computation fork-context construction (T18).

Extracted from AgentCommunicationService._create_dynamic_subagent (ADR-0015 D5).
T18 simplified the builder to a pure computation: ``build(...)`` queries the
parent session's message history (via the MemorySystem, which under T09
routes through ``MessageStore.load_messages()``), applies lossy compaction,
and returns the XML string directly. No fork XML files are written to disk.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from modex_agent.core.message import ChatMessage
from modex_agent.memory.snapshot import format_snapshot_xml

if TYPE_CHECKING:
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.ioc.configs.memory import MemoryConfig
    from modex_agent.memory.core.system import MemorySystem

logger = logging.getLogger(__name__)


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
        template_memory: MemoryConfig | None,
        subagent_memory_system: MemorySystem | None,
        parent_name: str,
    ) -> str | None:
        """Build fork XML from parent message history.

        Returns the placeholder XML when the memory system is absent, returns
        no messages, or raises.
        """
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
                    fork_xml = format_snapshot_xml(truncated, parent_name)
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
