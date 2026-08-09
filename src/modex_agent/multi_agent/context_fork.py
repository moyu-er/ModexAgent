"""ContextForkBuilder — pure-computation fork-context construction (T18).

Extracted from AgentCommunicationService._create_dynamic_subagent (ADR-0015 D5).
T18 simplified the builder to a pure computation: ``build(...)`` queries the
parent session's message history (via the MemorySystem, which under T09
routes through ``MessageStore.load_messages()``) and returns the XML string
directly. No fork XML files are written to disk.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from modex_agent.core.scope import MemoryContext
from modex_agent.memory.snapshot import format_snapshot_xml

if TYPE_CHECKING:
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.memory.core.system import MemorySystem

logger = logging.getLogger(__name__)


class ContextForkBuilder:
    """Builds fork context XML from parent message history (pure computation).

    ``build`` queries the parent session's messages through the subagent
    memory system (the application-facing read path that under T09 routes
    through ``MessageStore.load_messages()``), limits the result to the last
    ``fork_max_messages``, and returns the XML string. No files are written;
    the caller passes the string to the subagent's prompt providers.
    """

    async def build(
        self,
        *,
        parent_session: SessionInfo | str,
        agent_type: str,
        invocation_id: str,
        fork_max_messages: int,
        subagent_memory_system: MemorySystem,
        parent_name: str,
    ) -> str | None:
        """Build fork XML from parent message history.

        Returns the placeholder XML when history retrieval fails.
        """
        fork_xml = (
            f'<forked_context source="{parent_name}">'
            f"  <info>No parent messages available.</info>"
            f"</forked_context>"
        )
        try:
            parent_ctx = MemoryContext(session_id=str(parent_session))
            parent_messages = await subagent_memory_system.get_full_history(
                parent_ctx, limit=fork_max_messages
            )
            fork_xml = format_snapshot_xml(parent_messages, parent_name)
        except Exception:
            logger.exception(
                "Fork context: failed to build for %s, continuing with empty", agent_type
            )

        return fork_xml
