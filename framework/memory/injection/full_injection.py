from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from framework.memory.core.models import (
    InjectionResult,
    MemoryBudget,
)
from framework.memory.core.scope import MemoryContext
from framework.memory.core.system import MemorySystem
from framework.memory.injection.policy import MemoryInjectionPolicy
from framework.memory.pruned.manager import PrunedManager
from framework.memory.tags import ArchiveTag, KnowledgeTag
from framework.memory.utils import estimate_text_tokens
from framework.utils.xml import xml_attr, xml_text

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PromptSection:
    """Internal: section content with priority for sorting during assembly."""

    content: str
    priority: int = 0


class FullInjectionPolicy(MemoryInjectionPolicy):
    """Main agent policy — knowledge + archive + providers + session.

    Injection order (deterministic, priority-ordered):
    1. Previous conversations disclaimer → priority=110
    2. Knowledge: identity, user profile, known facts → priority=100
    3. Archive summaries: older topics → priority=70
    4. Provider static blocks → priority=60
    5. Provider prefetch → priority=50
    6. Session visible messages → messages field of result
    """

    def __init__(
        self,
        *,
        budget: MemoryBudget | None = None,
        max_history_entries: int = 3,
        pruned_manager: PrunedManager | None = None,
        archive_inject_count: int = 3,
        archive_inject_max_chars: int = 1000,
    ) -> None:
        self._budget = budget or MemoryBudget()
        self._max_history = max_history_entries
        self._pruned_manager = pruned_manager
        self._archive_inject_count = archive_inject_count
        self._archive_inject_max_chars = archive_inject_max_chars

    async def assemble(
        self,
        *,
        context: MemoryContext,
        memory_system: MemorySystem,
        query: str = "",
    ) -> InjectionResult:
        sections: list[_PromptSection] = []

        self._inject_disclaimer(sections)
        await self._inject_knowledge(sections, context, memory_system, query)
        await self._inject_archive(sections, context, memory_system)
        self._inject_pruned_catalog(sections, context)
        await self._inject_provider_blocks(sections, memory_system)
        await self._inject_provider_prefetch(sections, context, memory_system, query)

        sections = self._trim_by_priority(sections)

        session_msgs = await memory_system.get_history(
            context, max_messages=self._budget.max_history_messages
        )

        system_prompt = "\n\n".join(s.content for s in sections) if sections else ""
        return InjectionResult(
            system_prompt=system_prompt,
            messages=list(session_msgs),
        )

    # -- injection helpers ---------------------------------------------------

    def _inject_disclaimer(self, sections: list[_PromptSection]) -> None:
        """Inject a header and disclaimer about injected memory sections."""
        sections.append(
            _PromptSection(
                content=(
                    "## Memory & Past Sessions\n\n"
                    "Below is your persistent memory (identity, user info, learned facts) "
                    "and previous conversation history (transcripts and summaries). "
                    "These are from **past** sessions — they are NOT part of the current "
                    "conversation.\n\n"
                    "**Your current conversation always takes priority.** If anything "
                    "below conflicts with the current conversation, trust the current "
                    "conversation. When summaries and full transcripts differ, "
                    "transcripts are the authoritative source."
                ),
                priority=110,
            )
        )

    async def _inject_knowledge(
        self,
        sections: list[_PromptSection],
        context: MemoryContext,
        memory_system: MemorySystem,
        query: str,
    ) -> None:
        """Inject knowledge as natural XML with relative file names.

        The directory path is emitted once at the top of the section; each
        element carries only the filename (e.g. ``file="SOUL.md"``).
        """
        try:
            knowledge = await memory_system.retrieve_knowledge(context, query=query)
            knowledge_dir = await memory_system.get_knowledge_directory(context)

            xml_parts: list[str] = []

            if knowledge.soul:
                file_attr = ""
                if knowledge_dir:
                    file_attr = ' file="SOUL.md"'
                tag = KnowledgeTag.YOUR_IDENTITY.value
                xml_parts.extend(
                    [
                        f'<{tag}{file_attr} editable="true"'
                        f' description="Who you are: personality, principles, and behavior rules">'
                        f"\n{xml_text(knowledge.soul)}\n"
                        f"</{tag}>",
                    ]
                )

            if knowledge.user:
                file_attr = ""
                if knowledge_dir:
                    file_attr = ' file="USER.md"'
                tag = KnowledgeTag.USER_PROFILE.value
                xml_parts.extend(
                    [
                        f'<{tag}{file_attr} editable="true"'
                        f' description="Facts about the user: name, preferences, habits, communication style">'
                        f"\n{xml_text(knowledge.user)}\n"
                        f"</{tag}>",
                    ]
                )

            if knowledge.memory:
                file_attr = ""
                if knowledge_dir:
                    file_attr = ' file="MEMORY.md"'
                tag = KnowledgeTag.KNOWN_FACTS.value
                xml_parts.extend(
                    [
                        f'<{tag}{file_attr} editable="false"'
                        f' description="Known facts about the project: conventions, decisions, verified solutions">'
                        f"\n{xml_text(knowledge.memory)}\n"
                        f"</{tag}>",
                    ]
                )

            if xml_parts:
                xml_content = "\n".join(xml_parts)

                # Pre-truncation: if XML exceeds 8000 chars, truncate safely
                from framework.memory.xml_truncate import truncate_xml_safe

                if len(xml_content) > 8000:
                    xml_content = truncate_xml_safe(
                        xml_content,
                        8000,
                        truncatable_paths=[
                            KnowledgeTag.YOUR_IDENTITY.value,
                            KnowledgeTag.USER_PROFILE.value,
                            KnowledgeTag.KNOWN_FACTS.value,
                        ],
                    )

                dir_line = ""
                if knowledge_dir:
                    dir_line = (
                        f"Directory: {xml_attr(str(knowledge_dir.resolve()))}\n\n"
                    )
                heading = (
                    "### Knowledge Files\n\n"
                    "Self-maintained files storing your personality, user preferences, "
                    'and learned facts. Files with `editable="true"` can be updated '
                    "via file tools to evolve your knowledge over time.\n"
                    f"{dir_line}\n"
                )
                sections.append(
                    _PromptSection(
                        content=heading + xml_content,
                        priority=100,
                    )
                )
        except Exception:
            logger.debug("Knowledge injection skipped", exc_info=True)

    async def _inject_archive(
        self,
        sections: list[_PromptSection],
        context: MemoryContext,
        memory_system: MemorySystem,
    ) -> None:
        try:
            await self._inject_md_archives(sections, memory_system, context)
        except Exception:
            logger.debug("Archive injection skipped", exc_info=True)

    async def _inject_md_archives(
        self,
        sections: list[_PromptSection],
        memory_system: Any,
        context: MemoryContext,
    ) -> None:
        """Inject archive summaries from DirArchiveStorage (MD files on disk)."""
        try:
            archive_dir = await memory_system.get_storage_path(context)
        except Exception:
            return

        if archive_dir is None:
            return

        from framework.memory.stores.dir_archive import DirArchiveStorage

        storage = DirArchiveStorage(archive_dir)

        try:
            archive_ids = await storage.list_archives(limit=self._archive_inject_count)
        except Exception:
            return

        if not archive_ids:
            return

        # Read context.md from each archive (ascending by archive_id: oldest first)
        records: list[str] = []
        for aid in sorted(archive_ids)[: self._archive_inject_count]:
            try:
                content = await storage.read_archive_file(aid, "context.md")
            except Exception:
                continue

            if not content or not content.strip():
                continue

            truncated = len(content) > self._archive_inject_max_chars
            display = content[: self._archive_inject_max_chars] + "..." if truncated else content

            full_path = self._archive_file_path(archive_dir, aid)
            st = ArchiveTag.SUMMARY.value
            records.append(
                f'<{st} number="{aid}" file="{xml_attr(full_path)}">\n{xml_text(display)}\n</{st}>'
            )

        if not records:
            return

        heading = (
            "### Earlier Conversation Summaries\n\n"
            "Short summaries of older conversations. Higher number = more recent. "
            "Read the `context.md` file at each path for the full details.\n\n"
        )
        ct = ArchiveTag.CONTAINER.value
        xml = f"<{ct}>\n" + "\n".join(records) + f"\n</{ct}>"
        sections.append(_PromptSection(content=heading + xml, priority=70))

    def _archive_file_path(self, archive_dir: Path, archive_id: int) -> str:
        """Return absolute path to archive context.md for injection XML."""
        return str((archive_dir / str(archive_id) / "context.md").resolve())

    def _inject_pruned_catalog(
        self,
        sections: list[_PromptSection],
        context: MemoryContext,
    ) -> None:
        if self._pruned_manager is None:
            return
        session_id: str = context.session_id or ""
        xml = self._pruned_manager.get_injection_xml(session_id=session_id)
        if xml:
            sections.append(_PromptSection(content=xml, priority=85))

    async def _inject_provider_blocks(
        self,
        sections: list[_PromptSection],
        memory_system: MemorySystem,
    ) -> None:
        for provider in memory_system.get_providers():
            try:
                block = provider.system_prompt_block()
                if block:
                    sections.append(
                        _PromptSection(
                            content=block,
                            priority=60,
                        )
                    )
            except Exception:
                logger.debug("Provider block failed for %s", provider.name, exc_info=True)

    async def _inject_provider_prefetch(
        self,
        sections: list[_PromptSection],
        context: MemoryContext,
        memory_system: MemorySystem,
        query: str,
    ) -> None:
        if not query:
            return
        try:
            prefetch = await memory_system.prefetch_memories(query, context)
            if prefetch:
                sections.append(
                    _PromptSection(
                        content=f"<related_facts>\n{xml_text(prefetch)}\n</related_facts>",
                        priority=50,
                    )
                )
        except Exception:
            logger.debug("Provider prefetch failed", exc_info=True)

    def _trim_by_priority(self, sections: list[_PromptSection]) -> list[_PromptSection]:
        """Sort by priority descending and optionally trim to token budget."""
        sorted_sections = sorted(sections, key=lambda s: s.priority, reverse=True)
        max_tokens = self._budget.max_system_prompt_tokens
        if max_tokens is None or max_tokens <= 0:
            return sorted_sections

        kept: list[_PromptSection] = []
        running = 0
        for sec in sorted_sections:
            tokens = estimate_text_tokens(sec.content)
            if running + tokens <= max_tokens:
                kept.append(sec)
                running += tokens
            else:
                trimmed = self._trim_section_by_paragraphs(sec, max_tokens - running)
                if trimmed:
                    kept.append(trimmed)
                    running += estimate_text_tokens(trimmed.content)
        return kept

    @staticmethod
    def _trim_section_by_paragraphs(
        section: _PromptSection, max_chars: int
    ) -> _PromptSection | None:
        """Trim a single section by dropping paragraphs from the end."""
        if len(section.content) <= max_chars:
            return section
        paragraphs = section.content.split("\n\n")
        if not paragraphs:
            return None
        kept = [paragraphs[0]]
        for para in paragraphs[1:]:
            candidate = "\n\n".join(kept + [para])
            if len(candidate) <= max_chars:
                kept.append(para)
            else:
                break
        if not kept:
            return None
        trimmed_content = "\n\n".join(kept)
        if trimmed_content == section.content:
            return section
        return _PromptSection(
            content=trimmed_content,
            priority=section.priority,
        )
