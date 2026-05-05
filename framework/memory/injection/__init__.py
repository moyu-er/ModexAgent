"""Memory injection policies for LLM context assembly.

Provides pluggable strategies for converting MemorySystem state into
structured context bundles.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from framework.core.context import ContextState
from framework.memory.core.models import (
    MemoryBudget,
    MemoryContextBundle,
    PromptSection,
)
from framework.memory.core.scope import MemoryContext
from framework.memory.core.system import InjectableMemorySystem, MemorySystem
from framework.memory.injection.filter import (
    InjectionFilterStrategy,
    NoopFilterStrategy,
    ToolMessageFilterStrategy,
)
from framework.memory.utils import estimate_text_tokens, normalize_memory_summary

logger = logging.getLogger(__name__)

__all__ = [
    "FullInjectionPolicy",
    "InjectionFilterStrategy",
    "MemoryInjectionPolicy",
    "NoopFilterStrategy",
    "RestrictedInjectionPolicy",
    "ToolMessageFilterStrategy",
]


class MemoryInjectionPolicy(ABC):
    """Convert MemorySystem state into a structured context bundle for LLM consumption."""

    @abstractmethod
    async def assemble(
        self,
        *,
        context: MemoryContext,
        memory_system: MemorySystem,
        query: str = "",
    ) -> MemoryContextBundle: ...


class RestrictedInjectionPolicy(MemoryInjectionPolicy):
    """Peer/subagent policy — session messages only, no knowledge/archive/providers."""

    def __init__(
        self,
        max_session_messages: int = 50,
        filter_strategy: InjectionFilterStrategy | None = None,
    ) -> None:
        self._max_messages = max_session_messages
        self._filter = filter_strategy or ToolMessageFilterStrategy()

    async def assemble(
        self,
        *,
        context: MemoryContext,
        memory_system: MemorySystem,
        query: str = "",
    ) -> MemoryContextBundle:
        messages = await memory_system.get_history(context, max_messages=self._max_messages)
        filtered = self._filter.filter(list(messages))
        return MemoryContextBundle(
            system_sections=[],
            messages=filtered,
        )


class FullInjectionPolicy(MemoryInjectionPolicy):
    """Main agent policy — knowledge + archive + providers + session.

    Injection order (deterministic, priority-ordered):
    1. Bootstrap files: SOUL, USER → PromptSection(priority=100)
    2. Knowledge: MEMORY.md → PromptSection(priority=90)
    3. Archive recent: search or get_recent → PromptSection(priority=70)
    4. Provider static blocks → PromptSection(priority=60, source="provider:{name}")
    5. Provider prefetch → PromptSection(priority=50, source="provider:{name}")
    6. Compression summary → PromptSection(priority=40)
    7. Auto-compact summary → PromptSection(priority=30)
    8. Session visible messages → messages field of bundle (after filter)
    """

    def __init__(
        self,
        *,
        budget: MemoryBudget | None = None,
        max_history_entries: int = 20,
        filter_strategy: InjectionFilterStrategy | None = None,
    ) -> None:
        self._budget = budget or MemoryBudget()
        self._max_history = max_history_entries
        self._filter = filter_strategy or ToolMessageFilterStrategy()

    async def assemble(
        self,
        *,
        context: MemoryContext,
        memory_system: MemorySystem,
        query: str = "",
    ) -> MemoryContextBundle:
        if not isinstance(memory_system, InjectableMemorySystem):
            raise TypeError(
                f"memory_system must implement InjectableMemorySystem, got {type(memory_system).__name__}"
            )
        sections: list[PromptSection] = []
        injectable = memory_system

        # 1. Knowledge (SOUL, USER, MEMORY)
        await self._inject_knowledge(sections, context, injectable, query)

        # 2. Archive
        await self._inject_archive(sections, context, injectable, query)

        # 3. Provider static
        await self._inject_provider_blocks(sections, injectable)

        # 4. Provider prefetch
        await self._inject_provider_prefetch(sections, context, injectable, query)

        # 5. Compression / auto-compact summaries
        await self._inject_compression_summaries(sections, context, injectable)

        # 6. Token budget trimming on sections
        sections, dropped = self._trim_by_priority(sections)

        # 7. Session messages after filter
        session_msgs = await memory_system.get_history(
            context, max_messages=self._budget.max_history_messages
        )
        filtered_msgs = self._filter.filter(list(session_msgs))

        return MemoryContextBundle(
            system_sections=sections,
            messages=filtered_msgs,
            dropped_sections=dropped,
        )

    # -- injection helpers ---------------------------------------------------

    async def _inject_knowledge(
        self,
        sections: list[PromptSection],
        context: MemoryContext,
        memory_system: InjectableMemorySystem,
        query: str,
    ) -> None:
        try:
            knowledge = await memory_system.retrieve_knowledge(context, query=query)
            if knowledge.soul:
                sections.append(PromptSection(
                    key="knowledge:soul", content=f"{knowledge.soul}",
                    priority=100, source="system",
                ))
            if knowledge.user:
                sections.append(PromptSection(
                    key="knowledge:user", content=f"{knowledge.user}",
                    priority=100, source="system",
                ))
            if knowledge.memory:
                sections.append(PromptSection(
                    key="knowledge:memory", content=f"{knowledge.memory}",
                    priority=90, source="system",
                ))
        except Exception:
            logger.debug("Knowledge injection skipped", exc_info=True)

    async def _inject_archive(
        self,
        sections: list[PromptSection],
        context: MemoryContext,
        memory_system: InjectableMemorySystem,
        query: str,
    ) -> None:
        try:
            entries = await memory_system.get_history_entries(
                context, limit=self._max_history, query=query
            )
            if entries:
                lines = [
                    f"- {summary}"
                    for e in entries
                    if (
                        (summary := normalize_memory_summary(e.get("summary")))
                        is not None
                        and e.get("metadata", {}).get("source") != "empty"
                        and e.get("metadata", {}).get("semantic_count") != 0
                    )
                ]
                if lines:
                    sections.append(PromptSection(
                        key="archive:recent",
                        content="## 历史对话摘要\n" + "\n".join(lines),
                        priority=70, source="system",
                    ))
        except Exception:
            logger.debug("Archive injection skipped", exc_info=True)

    async def _inject_provider_blocks(
        self,
        sections: list[PromptSection],
        memory_system: InjectableMemorySystem,
    ) -> None:
        for provider in memory_system.get_providers():
            try:
                block = provider.system_prompt_block()
                if block:
                    sections.append(PromptSection(
                        key=f"provider:{provider.name}",
                        content=block,
                        priority=60, source=f"provider:{provider.name}",
                    ))
            except Exception:
                logger.debug("Provider block failed for %s", provider.name, exc_info=True)

    async def _inject_provider_prefetch(
        self,
        sections: list[PromptSection],
        context: MemoryContext,
        memory_system: InjectableMemorySystem,
        query: str,
    ) -> None:
        if not query:
            return
        try:
            prefetch = await memory_system.prefetch_memories(query, context)
            if prefetch:
                sections.append(PromptSection(
                    key="provider:prefetch",
                    content=f"<memory-context>\n{prefetch}\n</memory-context>",
                    priority=50, source="provider:prefetch",
                ))
        except Exception:
            logger.debug("Provider prefetch failed", exc_info=True)

    async def _inject_compression_summaries(
        self,
        sections: list[PromptSection],
        context: MemoryContext,
        memory_system: InjectableMemorySystem,
    ) -> None:
        try:
            summary = await memory_system.get_compression_summary(context)
            normalized_summary = normalize_memory_summary(summary)
            if normalized_summary is not None:
                sections.append(PromptSection(
                    key="session:compression",
                    content=f"[Earlier conversation compressed] {normalized_summary}",
                    priority=40, source="system",
                ))
        except Exception:
            pass
        try:
            auto = await memory_system.get_auto_compact_summary(context)
            normalized_auto = normalize_memory_summary(auto)
            if normalized_auto is not None:
                sections.append(PromptSection(
                    key="session:auto_compact",
                    content=f"[Auto-compact summary] {normalized_auto}",
                    priority=30, source="system",
                ))
        except Exception:
            pass

    def _trim_by_priority(
        self, sections: list[PromptSection]
    ) -> tuple[list[PromptSection], list[dict[str, Any]]]:
        """Sort by priority descending and optionally trim to token budget.

        If ``self._budget.max_system_prompt_tokens`` is set, sections are
        dropped from lowest priority until the total is under budget.
        Dropped sections are returned for diagnostics.
        """
        sorted_sections = sorted(sections, key=lambda s: s.priority, reverse=True)
        dropped: list[dict[str, Any]] = []
        max_tokens = self._budget.max_system_prompt_tokens
        if max_tokens is None or max_tokens <= 0:
            return sorted_sections, dropped

        # Calculate per-section token estimates
        section_tokens: list[tuple[PromptSection, int]] = []
        for sec in sorted_sections:
            section_tokens.append((sec, estimate_text_tokens(sec.content)))

        total = sum(st for _, st in section_tokens)
        if total <= max_tokens:
            return sorted_sections, dropped

        # Drop from lowest priority until under budget
        kept: list[PromptSection] = []
        running = 0
        for sec, tokens in section_tokens:
            if running + tokens <= max_tokens:
                kept.append(sec)
                running += tokens
            else:
                # Try paragraph-level trim before dropping entirely
                trimmed = self._trim_section_by_paragraphs(sec, max_tokens - running)
                if trimmed:
                    kept.append(trimmed)
                    running += estimate_text_tokens(trimmed.content)
                    if trimmed.content != sec.content:
                        dropped.append({
                            "section_key": sec.key,
                            "source": sec.source,
                            "priority": sec.priority,
                            "estimated_size": tokens,
                            "trim_reason": "paragraph_trim",
                        })
                else:
                    dropped.append({
                        "section_key": sec.key,
                        "source": sec.source,
                        "priority": sec.priority,
                        "estimated_size": tokens,
                        "trim_reason": "budget_drop",
                    })
        return kept, dropped

    @staticmethod
    def _trim_section_by_paragraphs(
        section: PromptSection, max_chars: int
    ) -> PromptSection | None:
        """Trim a single section by dropping paragraphs from the end.

        Always keeps the first paragraph. Returns None if the first paragraph
        alone exceeds the limit.
        """
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
        return PromptSection(
            key=section.key,
            content=trimmed_content,
            priority=section.priority,
            source=section.source,
            metadata=dict(section.metadata),
        )


# -- context manager bridge ---------------------------------------------------


def bundle_to_context_state(
    bundle: MemoryContextBundle,
    memory_system: MemorySystem,
    context: MemoryContext,
    base_system_prompt: str = "",
) -> ContextState:
    """Convert a MemoryContextBundle into the framework ContextState.

    Used by MemorySystemContextManager as a bridge.
    """
    parts: list[str] = []
    if base_system_prompt:
        parts.append(base_system_prompt)
    for section in bundle.system_sections:
        parts.append(section.content)
    system_prompt = "\n\n---\n\n".join(parts) if parts else ""

    history = memory_system.create_message_history(
        context=context,
        initial_messages=bundle.messages,
    )
    return ContextState(system_prompt=system_prompt, history=history)
