"""KnowledgeConsolidator — ReAct-based agent that receives pre-read archive
knowledge.md extracts and updates long-term knowledge files
(SOUL.md, USER.md, MEMORY.md).

Extends :class:`ScopedFileAgent` for common ReAct wiring.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from framework.agents.summarizer.abc import (
    _get_registry,
    KnowledgeConsolidatorBase,
)
from framework.agents.summarizer.scoped_file_agent import ScopedFileAgent

logger = logging.getLogger(__name__)

_KNOWLEDGE_FILES = ("SOUL.md", "USER.md", "MEMORY.md")
_ARCHIVE_KNOWLEDGE_FILE = "knowledge.md"


class KnowledgeConsolidator(ScopedFileAgent, KnowledgeConsolidatorBase):
    """Knowledge consolidation agent — scoped to the knowledge directory only.

    The agent uses a ReAct loop with file tools scoped to *knowledge_dir*.
    Archive ``knowledge.md`` content is pre-read by :meth:`consolidate`
    and provided inline in the user message — the agent never touches
    archive directories.
    """

    def __init__(self, provider: Any, max_iterations: int = 25) -> None:
        super().__init__(provider=provider, max_iterations=max_iterations)

    # -- prompt builder -----------------------------------------------------

    @staticmethod
    def build_system_prompt(knowledge_dir: Path) -> str:
        """Build the system prompt with the knowledge directory injected."""
        return _get_registry().get_system(
            "knowledge/consolidator",
            allowed_dir=str(knowledge_dir.resolve()),
        )

    # -- public entry point -------------------------------------------------

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
                    f"## Archive {aid} — knowledge.md\n(empty)"
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
            "Archives are listed oldest→newest (ascending archive IDs).\n"
            "Compare them with the current knowledge files and apply updates.\n"
            "Use ONLY the read/write/edit/ls tools. Do NOT call bash, shell, "
            "python, or any other tool.\n\n"
            + "\n\n".join(archive_sections)
            + "\n\n"
            + "\n\n".join(knowledge_context)
        )

        system_prompt = self.build_system_prompt(knowledge_dir)
        trace_key = invocation_id or "_".join(str(aid) for aid in archive_ids) or "none"
        session_id = f"knowledge-consolidator-{trace_key}"
        trace_path = archive_base / "traces" / f"consolidator-{trace_key}.jsonl"

        logger.info(
            "KnowledgeConsolidator starting: archives=%s invocation=%s",
            archive_ids, invocation_id or trace_key,
        )

        for attempt in range(2):
            ok = await self._run_agent(
                system_prompt=system_prompt,
                user_msg=user_msg,
                allowed_dirs=[knowledge_dir],
                session_id=session_id,
                agent_name="KnowledgeConsolidator",
                trace_path=trace_path,
                max_iterations=effective_max_iterations,
            )
            if ok:
                # Verify at least one knowledge file exists (sanity check —
                # the agent may have run without actually writing anything).
                any_exists = any(
                    (knowledge_dir / fname).exists()
                    and (knowledge_dir / fname).stat().st_size > 0
                    for fname in _KNOWLEDGE_FILES
                )
                if any_exists:
                    logger.info(
                        "KnowledgeConsolidator succeeded: archives=%s invocation=%s attempt=%d",
                        archive_ids, invocation_id or trace_key, attempt + 1,
                    )
                    return True
                logger.warning(
                    "KnowledgeConsolidator agent ran but no knowledge files found, retrying. "
                    "archives=%s invocation=%s attempt=%d",
                    archive_ids, invocation_id or trace_key, attempt + 1,
                )
            logger.warning(
                "KnowledgeConsolidator attempt %d failed archive_ids=%s invocation=%s",
                attempt + 1, archive_ids, invocation_id or trace_key,
            )

        return False
