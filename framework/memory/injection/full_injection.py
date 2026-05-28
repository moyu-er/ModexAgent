from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from framework.memory.archive_models import ArchiveChannel
from framework.memory.core.models import (
    InjectionResult,
    MemoryBudget,
)
from framework.memory.core.scope import MemoryContext
from framework.memory.core.system import InjectableMemorySystem, MemorySystem
from framework.memory.injection.policy import MemoryInjectionPolicy
from framework.memory.utils import estimate_text_tokens, normalize_memory_summary

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PromptSection:
    """Internal: section content with priority for sorting during assembly."""
    content: str
    priority: int = 0


class FullInjectionPolicy(MemoryInjectionPolicy):
    """Main agent policy — knowledge + archive + providers + session.

    Injection order (deterministic, priority-ordered):
    1. Bootstrap files: SOUL, USER → priority=100
    2. Knowledge: MEMORY.md → priority=90
    3. Archive recent: search or get_recent → priority=70
    4. Provider static blocks → priority=60
    5. Provider prefetch → priority=50
    6. Session visible messages → messages field of result
    """

    def __init__(
        self,
        *,
        budget: MemoryBudget | None = None,
        max_history_entries: int = 20,
    ) -> None:
        self._budget = budget or MemoryBudget()
        self._max_history = max_history_entries

    async def assemble(
        self,
        *,
        context: MemoryContext,
        memory_system: MemorySystem,
        query: str = "",
    ) -> InjectionResult:
        if not isinstance(memory_system, InjectableMemorySystem):
            raise TypeError(
                f"memory_system must implement InjectableMemorySystem, got {type(memory_system).__name__}"
            )
        sections: list[_PromptSection] = []
        injectable = memory_system

        await self._inject_knowledge(sections, context, injectable, query)
        await self._inject_archive(sections, context, injectable, query)
        await self._inject_provider_blocks(sections, injectable)
        await self._inject_provider_prefetch(sections, context, injectable, query)

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

    async def _inject_knowledge(
        self,
        sections: list[_PromptSection],
        context: MemoryContext,
        memory_system: InjectableMemorySystem,
        query: str,
    ) -> None:
        try:
            knowledge = await memory_system.retrieve_knowledge(context, query=query)
            if knowledge.soul:
                sections.append(_PromptSection(
                    content=f"{knowledge.soul}",
                    priority=100,
                ))
            if knowledge.user:
                sections.append(_PromptSection(
                    content=f"{knowledge.user}",
                    priority=100,
                ))
            if knowledge.memory:
                sections.append(_PromptSection(
                    content=f"{knowledge.memory}",
                    priority=90,
                ))

            knowledge_dir = await memory_system.get_knowledge_directory(context)
            if knowledge_dir is not None:
                sections.append(_PromptSection(
                    content=(
                        f"## Knowledge Directory\n\n"
                        f"Path: `{knowledge_dir}`\n\n"
                        f"Files:\n"
                        f"- SOUL.md — your personality, principles, and behavioral rules\n"
                        f"- USER.md — user profile (name, preferences, tech level, work context). "
                        f"Update proactively as you learn about the user.\n"
                        f"- MEMORY.md — persistent notes and facts across sessions\n"
                    ),
                    priority=95,
                ))
        except Exception:
            logger.debug("Knowledge injection skipped", exc_info=True)

    async def _inject_archive(
        self,
        sections: list[_PromptSection],
        context: MemoryContext,
        memory_system: InjectableMemorySystem,
        query: str,
    ) -> None:
        try:
            entries = await memory_system.get_history_entries(
                context,
                limit=self._max_history,
                query=query,
                channel=ArchiveChannel.CONTEXT,
            )
            if entries:
                blocks: list[str] = []
                for idx, e in enumerate(entries, start=1):
                    summary = normalize_memory_summary(e.get("summary"))
                    if summary is None:
                        continue
                    if e.get("metadata", {}).get("source") == "empty":
                        continue
                    if e.get("metadata", {}).get("semantic_count") == 0:
                        continue
                    created_at = e.get("created_at")
                    time_str = ""
                    if isinstance(created_at, str):
                        time_str = f" {created_at.replace('T', ' ')[:16]}"
                    elif isinstance(created_at, datetime):
                        time_str = f" {created_at.strftime('%Y-%m-%d %H:%M')}"
                    blocks.append(f"--- [Historical Record {idx}]{time_str} ---\n{summary}")
                if blocks:
                    sections.append(_PromptSection(
                        content="## Historical Context Summaries\n\n" + "\n\n".join(blocks),
                        priority=70,
                    ))
        except Exception:
            logger.debug("Archive injection skipped", exc_info=True)

    async def _inject_provider_blocks(
        self,
        sections: list[_PromptSection],
        memory_system: InjectableMemorySystem,
    ) -> None:
        for provider in memory_system.get_providers():
            try:
                block = provider.system_prompt_block()
                if block:
                    sections.append(_PromptSection(
                        content=block,
                        priority=60,
                    ))
            except Exception:
                logger.debug("Provider block failed for %s", provider.name, exc_info=True)

    async def _inject_provider_prefetch(
        self,
        sections: list[_PromptSection],
        context: MemoryContext,
        memory_system: InjectableMemorySystem,
        query: str,
    ) -> None:
        if not query:
            return
        try:
            prefetch = await memory_system.prefetch_memories(query, context)
            if prefetch:
                sections.append(_PromptSection(
                    content=f"<memory-context>\n{prefetch}\n</memory-context>",
                    priority=50,
                ))
        except Exception:
            logger.debug("Provider prefetch failed", exc_info=True)

    def _trim_by_priority(
        self, sections: list[_PromptSection]
    ) -> list[_PromptSection]:
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
