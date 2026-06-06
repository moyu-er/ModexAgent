"""KnowledgeConsolidator — ReAct-based agent that reads archive knowledge.md files
and updates long-term knowledge files (SOUL.md, USER.md, MEMORY.md).

Uses ReActAgent with scoped file tools: read from archive dirs + knowledge dir,
write/edit only in knowledge dir.
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


class KnowledgeConsolidator(KnowledgeConsolidatorBase):
    """Reads archive knowledge.md files and updates knowledge files.

    The agent uses a ReAct loop with scoped file tools:
    - Read: allowed in archive directories AND knowledge directory
    - Write/Edit: allowed only in knowledge directory
    - List: allowed in archive directories AND knowledge directory
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
    def build_tools(
        archive_dirs: list[Path],
        knowledge_dir: Path,
    ) -> list[Any]:
        """Create scoped file tools: read archive + knowledge, write knowledge only.

        Args:
            archive_dirs: Archive directories the agent can read from.
            knowledge_dir: Knowledge directory the agent can read/write.

        Returns:
            List of 4 Tool instances (read, write, edit, list).
        """
        read_dirs = [d.resolve() for d in archive_dirs] + [knowledge_dir.resolve()]
        write_dirs = [knowledge_dir.resolve()]
        return [
            ScopedReadFileTool(read_dirs),
            ScopedWriteFileTool(write_dirs),
            ScopedEditFileTool(write_dirs),
            ScopedListTool(read_dirs),
        ]

    def build_system_prompt(
        self,
        archive_dirs: list[Path],
        knowledge_dir: Path,
    ) -> str:
        """Build the system prompt with allowed directories and archive file list.

        Args:
            archive_dirs: Archive directories containing knowledge.md files.
            knowledge_dir: Knowledge directory for output files.

        Returns:
            Fully resolved system prompt string.
        """
        prompt = _get_registry().get_system("knowledge/consolidator")

        # Build allowed directories section
        dir_lines = [f"- {knowledge_dir.resolve()}"]

        # Add each archive directory with its knowledge.md
        for ad in archive_dirs:
            dir_path = ad.resolve()
            km = dir_path / "knowledge.md"
            if km.exists():
                dir_lines.append(f"- {dir_path} (knowledge.md: {km.stat().st_size} bytes)")
            else:
                dir_lines.append(f"- {dir_path}")

        prompt = prompt.replace(
            "You can ONLY access files in the directories listed below.",
            "You can ONLY access files in the directories listed below.\n"
            + "\n".join(dir_lines)
            + "\n",
        )

        # Append archive file inventory
        archive_inventory: list[str] = []
        for ad in archive_dirs:
            archive_inventory.append(f"- {ad.resolve() / 'knowledge.md'}")
        inventory_text = "\n".join(archive_inventory)
        prompt += (
            f"\n## Available Archive Files\n"
            f"The following knowledge.md files are available for reading:\n"
            f"{inventory_text}\n"
        )

        return prompt

    async def consolidate(
        self,
        archive_ids: list[int],
        archive_base: Path,
        knowledge_dir: Path,
        *,
        max_iterations: int | None = None,
    ) -> bool:
        """Main entry: read knowledge.md from archives, update knowledge files.

        Args:
            archive_ids: List of archive IDs to process.
            archive_base: Base directory containing archive subdirectories.
            knowledge_dir: Directory containing knowledge files to update.
            max_iterations: Optional override for max ReAct iterations.
                When ``None``, ``self.max_iterations`` is used.

        Returns:
            True if the agent ran successfully, False otherwise.
        """
        if not archive_ids:
            return True  # Nothing to do

        effective_max_iterations = (
            max_iterations if max_iterations is not None else self.max_iterations
        )
        archive_dirs = [archive_base / str(aid) for aid in archive_ids]

        # Build user message with current knowledge files content
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
            "Read the archive knowledge.md files listed in the system prompt.\n"
            "Analyze them and update the knowledge files (SOUL.md/USER.md/MEMORY.md).\n"
            "Use ONLY the read/write/edit/ls tools. Do NOT call bash, shell, python, "
            "or any other tool.\n\n"
            + "\n\n".join(knowledge_context)
        )

        trace_key = "_".join(str(aid) for aid in archive_ids) or "none"

        # Retry once on failure
        for attempt in range(2):
            if await self._run_agent(
                archive_dirs,
                knowledge_dir,
                user_msg,
                archive_base=archive_base,
                trace_key=trace_key,
                max_iterations=effective_max_iterations,
            ):
                return True
            logger.warning(
                "KnowledgeConsolidator attempt %d failed for archive_ids=%s",
                attempt + 1,
                archive_ids,
            )

        return False

    async def _run_agent(
        self,
        archive_dirs: list[Path],
        knowledge_dir: Path,
        user_msg: str,
        archive_base: Path,
        trace_key: str,
        *,
        max_iterations: int,
    ) -> bool:
        """Run the ReAct agent once. Returns True on success, False on failure.

        Args:
            archive_dirs: Archive directories to read from.
            knowledge_dir: Knowledge directory to write to.
            user_msg: User message with current knowledge content.
            max_iterations: Max ReAct iterations for this run.

        Returns:
            True if agent completed successfully, False otherwise.
        """
        system_prompt = self.build_system_prompt(archive_dirs, knowledge_dir)

        # Build tools and register them
        tools = self.build_tools(archive_dirs, knowledge_dir)
        tool_manager = InMemoryToolManager()
        for tool in tools:
            tool_manager.register(tool)

        # Build context
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
