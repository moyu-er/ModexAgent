"""KnowledgeConsolidator — ReAct-based agent that receives pre-read archive
knowledge.md extracts and updates long-term knowledge files
(SOUL.md, USER.md, MEMORY.md).

Archive content is provided directly in the user message — the agent only
needs scoped file tools for the knowledge directory (read existing files,
write/update SOUL.md / USER.md / MEMORY.md).  No archive directory access.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from framework.agents.react.agent import ReActAgent
from framework.agents.summarizer.abc import (
    _get_registry,
    KnowledgeConsolidatorBase,
)
from framework.core.agent import AgentContext
from framework.agents.summarizer.emitter import SummarizerTrajectoryEmitter
from framework.core.tool_manager import InMemoryToolManager
from framework.core.types import MessageRole
from framework.memory.history import ListMessageHistory
from framework.memory.tools import (
    ScopedEditFileTool,
    ScopedListTool,
    ScopedReadFileTool,
    ScopedWriteFileTool,
)

logger = logging.getLogger(__name__)

_KNOWLEDGE_FILES = ("SOUL.md", "USER.md", "MEMORY.md")
_ARCHIVE_KNOWLEDGE_FILE = "knowledge.md"


class KnowledgeConsolidator(KnowledgeConsolidatorBase):
    """Knowledge consolidation agent — scoped to the knowledge directory only.

    The agent uses a ReAct loop with file tools scoped to *knowledge_dir*:
    - Read / List: knowledge directory (SOUL.md, USER.md, MEMORY.md)
    - Write / Edit: knowledge directory (SOUL.md, USER.md, MEMORY.md)

    Archive ``knowledge.md`` content is pre-read by the caller and provided
    inline in the user message — the agent never touches archive directories.
    """

    def __init__(
        self,
        provider: Any,
        max_iterations: int = 25,
    ) -> None:
        from framework.core.provider import LLMProvider

        if not isinstance(provider, LLMProvider):
            raise TypeError(
                f"provider must be LLMProvider, got {type(provider).__name__}"
            )

        self._provider = provider
        self.max_iterations: int = max_iterations
        self._react_agent = ReActAgent(provider=self._provider, mode="clean")

    @staticmethod
    def build_tools(knowledge_dir: Path) -> list[Any]:
        """Create scoped file tools restricted to the knowledge directory.

        All four tools (read, write, edit, list) are limited to
        *knowledge_dir* — the agent has no access to archive directories.
        """
        allowed = [knowledge_dir.resolve()]
        return [
            ScopedReadFileTool(allowed),
            ScopedWriteFileTool(allowed),
            ScopedEditFileTool(allowed),
            ScopedListTool(allowed),
        ]

    @staticmethod
    def build_system_prompt(knowledge_dir: Path) -> str:
        """Build the system prompt with the knowledge directory injected.

        The template has a placeholder ``{allowed_dir}`` that is replaced
        with the resolved knowledge directory path.
        """
        prompt = _get_registry().get_system(
            "knowledge/consolidator",
            allowed_dir=str(knowledge_dir.resolve()),
        )
        return prompt

    async def consolidate(
        self,
        archive_ids: list[int],
        archive_base: Path,
        knowledge_dir: Path,
        *,
        max_iterations: int | None = None,
        invocation_id: str = "",
    ) -> bool:
        """Main entry: process knowledge.md extracts, update knowledge files.

        Reads ``knowledge.md`` from each archive directory and provides the
        content inline in the user message.  The agent never sees archive
        paths or filesystem layout.

        Args:
            archive_ids: List of archive IDs to process.
            archive_base: Base directory containing archive subdirectories.
            knowledge_dir: Directory containing knowledge files to update.
            max_iterations: Optional override for max ReAct iterations.
                When ``None``, ``self.max_iterations`` is used.
            invocation_id: Caller-supplied UUID for trace correlation.

        Returns:
            True if the agent ran successfully, False otherwise.
        """
        if not archive_ids:
            return True  # Nothing to do

        effective_max_iterations = (
            max_iterations if max_iterations is not None else self.max_iterations
        )

        # Pre-read knowledge.md content from each archive (inline, no paths)
        archive_sections: list[str] = []
        for aid in archive_ids:
            km_path = archive_base / str(aid) / _ARCHIVE_KNOWLEDGE_FILE
            if km_path.exists():
                raw = km_path.read_text(encoding="utf-8")
                archive_sections.append(
                    f"## Archive {aid} — knowledge.md\n"
                    f"<!-- source: archive {aid} -->\n"
                    f"{raw}"
                )
            else:
                archive_sections.append(
                    f"## Archive {aid} — knowledge.md\n"
                    f"(empty)"
                )

        # Current knowledge files (first 500 chars each for context)
        knowledge_context: list[str] = []
        for fname in _KNOWLEDGE_FILES:
            fpath = knowledge_dir / fname
            if fpath.exists():
                content = fpath.read_text(encoding="utf-8")
                if len(content) > 500:
                    content = (
                        content[:500]
                        + f"\n... ({len(content)} chars total)"
                    )
                knowledge_context.append(f"## Current {fname}\n{content}")
            else:
                knowledge_context.append(f"## Current {fname}\n(empty file)")

        user_msg = (
            "The archive knowledge.md extracts below contain facts for review.\n"
            "Compare them with the current knowledge files and apply updates.\n"
            "Use ONLY the read/write/edit/ls tools. Do NOT call bash, shell, "
            "python, or any other tool.\n\n"
            + "\n\n".join(archive_sections)
            + "\n\n"
            + "\n\n".join(knowledge_context)
        )

        trace_key = invocation_id or "_".join(str(aid) for aid in archive_ids) or "none"
        logger.info(
            "KnowledgeConsolidator starting: archives=%s invocation=%s",
            archive_ids, invocation_id or trace_key,
        )

        for attempt in range(2):
            if await self._run_agent(
                knowledge_dir, user_msg, archive_base,
                trace_key, effective_max_iterations,
            ):
                logger.info(
                    "KnowledgeConsolidator succeeded: archives=%s invocation=%s attempt=%d",
                    archive_ids, invocation_id or trace_key, attempt + 1,
                )
                return True
            logger.warning(
                "KnowledgeConsolidator attempt %d failed archive_ids=%s invocation=%s",
                attempt + 1, archive_ids, invocation_id or trace_key,
            )

        return False

    async def _run_agent(
        self,
        knowledge_dir: Path,
        user_msg: str,
        archive_base: Path,
        trace_key: str,
        max_iterations: int,
    ) -> bool:
        """Run the ReAct agent once. Returns True on success."""
        system_prompt = self.build_system_prompt(knowledge_dir)

        tools = self.build_tools(knowledge_dir)
        tool_manager = InMemoryToolManager()
        for tool in tools:
            tool_manager.register(tool)

        history = ListMessageHistory([
            {"role": MessageRole.USER, "content": user_msg},
        ])
        context = AgentContext(
            system_prompt=system_prompt,
            history=history,
            tool_manager=tool_manager,
            session_id="knowledge-consolidator",
            max_iterations=max_iterations,
            temperature=0.2,
        )

        trace_path = archive_base / "traces" / f"consolidator-{trace_key}.jsonl"
        emitter = SummarizerTrajectoryEmitter(
            session_id=f"knowledge-consolidator-{trace_key}",
            agent_name="KnowledgeConsolidator",
            trace_path=trace_path,
        )

        try:
            await self._react_agent.run(context, emitter)
            return True
        except Exception:
            logger.exception("KnowledgeConsolidator agent execution error")
            return False
