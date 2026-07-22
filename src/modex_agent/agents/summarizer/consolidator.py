"""CoreMemoryConsolidator — ReAct-based agent that receives pre-read archive
knowledge.md extracts and updates long-term core memory files
(SOUL.md, USER.md, MEMORY.md).

Extends :class:`ScopedFileAgent` for common ReAct wiring.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from modex_agent.agents.summarizer.abc import (
    CoreMemoryConsolidatorBase,
    _get_registry,
)
from modex_agent.agents.summarizer.scoped_file_agent import ScopedFileAgent

logger = logging.getLogger(__name__)

_KNOWLEDGE_FILES = ("SOUL.md", "USER.md", "MEMORY.md")
# NOTE: the on-disk archive content file stays as "knowledge.md" — it is an
# archive artifact, not part of the Core Memory layer rename (ADR-0035).
_ARCHIVE_CORE_MEMORY_FILE = "knowledge.md"


class CoreMemoryConsolidator(ScopedFileAgent, CoreMemoryConsolidatorBase):
    """Core memory consolidation agent — scoped to the core memory directory only.

    The agent uses a ReAct loop with file tools scoped to *core_memory_dir*.
    Archive ``knowledge.md`` content is pre-read by :meth:`consolidate`
    and provided inline in the user message — the agent never touches
    archive directories.
    """

    def __init__(self, provider: Any, max_iterations: int = 25) -> None:
        super().__init__(provider=provider, max_iterations=max_iterations)

    # -- prompt builder -----------------------------------------------------

    @staticmethod
    def build_system_prompt(core_memory_dir: Path) -> str:
        """Build the system prompt with the core memory directory injected."""
        return _get_registry().get_system(
            "core_memory/consolidator",
            allowed_dir=str(core_memory_dir.resolve()),
        )

    @staticmethod
    def build_user_message(
        archive_sections_text: str,
        current_core_memory_sections_text: str,
    ) -> str:
        """Build the user message from the template with variable substitution.

        The archive and core memory sections are inserted without XML escaping
        so that raw markdown content is preserved for the agent.
        """
        template = _get_registry().get_user(
            "core_memory/consolidator",
            archive_sections="__ARCHIVE_SECTIONS__",
            current_core_memory_sections="__CURRENT_CORE_MEMORY_SECTIONS__",
        )
        return template.replace("__ARCHIVE_SECTIONS__", archive_sections_text).replace(
            "__CURRENT_CORE_MEMORY_SECTIONS__", current_core_memory_sections_text
        )

    # -- public entry point -------------------------------------------------

    async def consolidate(
        self,
        archive_ids: list[int],
        archive_base: Path,
        core_memory_dir: Path,
        *,
        max_iterations: int | None = None,
        invocation_id: str = "",
    ) -> bool:
        """Main entry: process knowledge.md extracts, update core memory files.

        Pre-reads ``knowledge.md`` from each archive directory and provides
        the content inline in the user message.
        """
        if not archive_ids:
            return True

        effective_max_iterations = (
            max_iterations if max_iterations is not None else self.max_iterations
        )

        # Pre-read knowledge.md content from each archive (inline, no paths)
        archive_sections: list[str] = []
        for aid in archive_ids:
            km_path = archive_base / str(aid) / _ARCHIVE_CORE_MEMORY_FILE
            if km_path.exists():
                raw = km_path.read_text(encoding="utf-8")
                archive_sections.append(
                    f"## Archive {aid} — knowledge.md\n<!-- source: archive {aid} -->\n{raw}"
                )
            else:
                archive_sections.append(f"## Archive {aid} — knowledge.md\n(empty)")

        # Current core memory files (first 2000 chars each for context)
        core_context: list[str] = []
        for fname in _KNOWLEDGE_FILES:
            fpath = core_memory_dir / fname
            if fpath.exists():
                content = fpath.read_text(encoding="utf-8")
                if len(content) > 2000:
                    content = content[:2000] + f"\n... ({len(content)} chars total)"
                core_context.append(f"## Current {fname}\n{content}")
            else:
                core_context.append(f"## Current {fname}\n(empty file)")

        user_msg = self.build_user_message(
            archive_sections_text="\n\n".join(archive_sections),
            current_core_memory_sections_text="\n\n".join(core_context),
        )

        system_prompt = self.build_system_prompt(core_memory_dir)
        trace_key = invocation_id or "_".join(str(aid) for aid in archive_ids) or "none"
        session_id = f"core-memory-consolidator-{trace_key}"
        trace_path = archive_base / "traces" / f"consolidator-{trace_key}.jsonl"

        logger.info(
            "CoreMemoryConsolidator starting: archives=%s invocation=%s",
            archive_ids,
            invocation_id or trace_key,
        )

        for attempt in range(2):
            ok = await self._run_agent(
                system_prompt=system_prompt,
                user_msg=user_msg,
                allowed_dirs=[core_memory_dir],
                session_id=session_id,
                agent_name="CoreMemoryConsolidator",
                trace_path=trace_path,
                max_iterations=effective_max_iterations,
            )
            if ok:
                # Unlike ArchiveSummarizer (which writes files from scratch
                # and must verify they exist), core memory files are already
                # present via ensure_defaults.  The agent may legitimately
                # decide no updates are needed — that is still success.
                logger.info(
                    "CoreMemoryConsolidator succeeded: archives=%s invocation=%s attempt=%d",
                    archive_ids,
                    invocation_id or trace_key,
                    attempt + 1,
                )
                return True
            logger.warning(
                "CoreMemoryConsolidator attempt %d failed archive_ids=%s invocation=%s",
                attempt + 1,
                archive_ids,
                invocation_id or trace_key,
            )

        return False
