"""ContextForkBuilder — fork-context construction + file-registry ownership.

Extracted from AgentCommunicationService._create_dynamic_subagent (ADR-0015 D5).
Owns the fork-file registry (relocated from communication._FORK_FILE_REGISTRY).
``build(...)`` returns fork XML content; ``cleanup(session_id)`` removes the
file + registry entry on session eviction.
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
    """Builds fork context XML and owns the fork-file registry."""

    def __init__(self) -> None:
        self._registry: dict[str, Path] = {}

    async def build(
        self,
        *,
        parent_session: "SessionInfo | str",
        agent_type: str,
        invocation_id: str,
        fork_max_messages: int,
        fork_workspace: Path | None,
        template_memory: "MemoryConfig | None",
        subagent_memory_system: "MemorySystem | None",
        parent_name: str,
    ) -> str | None:
        """Build fork XML. Returns None if fork_workspace is unavailable."""
        if fork_workspace is None:
            logger.warning(
                "Fork context: no workspace for %s, skipping injection", agent_type,
            )
            return None
        fork_file = fork_workspace / "fork_contexts" / f"{agent_type}_{invocation_id}.xml"
        if fork_file.exists():
            logger.info("Fork context: loaded persisted file for %s/%s", agent_type, invocation_id)
            return fork_file.read_text(encoding="utf-8")

        # Initial creation: empty placeholder, then two-stage truncate + governance.
        fork_xml = (
            f'<forked_context source="{parent_name}">'
            f"  <info>No parent messages available.</info>"
            f"</forked_context>"
        )
        try:
            from modex_agent.core.scope import MemoryContext

            parent_ctx = MemoryContext(session_id=str(parent_session))
            if subagent_memory_system is not None:
                parent_messages = await subagent_memory_system.get_history(parent_ctx)
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
                    "Fork context: no memory_system for %s, fork will be empty", agent_type,
                )
            fork_file.parent.mkdir(parents=True, exist_ok=True)
            fork_file.write_text(fork_xml, encoding="utf-8")
            logger.info("Fork context: persisted for %s/%s", agent_type, invocation_id)
        except Exception:
            logger.exception("Fork context: failed to build for %s, continuing with empty", agent_type)

        return fork_xml

    def register_for_cleanup(self, *, session_id: str, fork_workspace: Path, agent_type: str, invocation_id: str) -> None:
        """Register a persisted fork file for cleanup on session eviction."""
        fork_file = fork_workspace / "fork_contexts" / f"{agent_type}_{invocation_id}.xml"
        if fork_file.exists():
            self._registry[session_id] = fork_file

    def cleanup(self, session_id: str) -> None:
        """Delete the persisted fork context file for a session, if any. No-op if none."""
        fork_file = self._registry.pop(session_id, None)
        if fork_file is not None and fork_file.exists():
            try:
                fork_file.unlink()
                logger.debug("Fork context file cleaned: %s", fork_file)
            except OSError:
                pass
