from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Final

from modex_agent.memory.core.models import (
    InjectionResult,
    MemoryBudget,
)
from modex_agent.memory.core.system import MemorySystem
from modex_agent.memory.hooks import (
    ContextAssembledPayload,
    MemoryHookContext,
    MemoryHookPoint,
    MemoryHookRunner,
    SectionProvenance,
)
from modex_agent.memory.injection.policy import MemoryInjectionPolicy
from modex_agent.memory.scope import MemoryContext
from modex_agent.memory.tags import CoreMemoryTag
from modex_agent.memory.token_estimator import CharTokenEstimator, TokenEstimator
from modex_agent.utils.xml import xml_attr, xml_text

logger = logging.getLogger(__name__)

_DISCLAIMER_SOURCE: Final = "disclaimer"
_CORE_MEMORY_SOURCE: Final = "core_memory"


@dataclass(frozen=True)
class _PromptSection:
    """Internal: section content with priority for sorting during assembly."""

    content: str
    source: str
    priority: int = 0


class FullInjectionPolicy(MemoryInjectionPolicy):
    """Main agent policy — budget-trimmed core memory assembly.

    Only assembles disclaimer + core memory (priority-sorted, token-budget-trimmed).
    All other content (archive, pruned, provider blocks, prefetch) is handled by
    dedicated SystemPromptProvider pipeline providers with version-based caching.
    """

    def __init__(
        self,
        *,
        budget: MemoryBudget | None = None,
        token_estimator: TokenEstimator | None = None,
        hook_runner: MemoryHookRunner | None = None,
    ) -> None:
        self._budget = budget or MemoryBudget()
        self._token_estimator: TokenEstimator = token_estimator or CharTokenEstimator()
        self._hook_runner = hook_runner

    async def assemble(
        self,
        *,
        context: MemoryContext,
        memory_system: MemorySystem,
        query: str = "",
    ) -> InjectionResult:
        started = time.monotonic()
        core_sections: list[_PromptSection] = []
        await self._inject_core_memory(core_sections, context, memory_system, query)

        sections: list[_PromptSection] = []
        if core_sections:
            self._inject_disclaimer(sections)
            sections.extend(core_sections)

        sections, provenance = self._trim_by_priority(sections)

        session_msgs = await memory_system.get_history(context)

        system_prompt = "\n\n".join(s.content for s in sections) if sections else ""
        result = InjectionResult(
            system_prompt=system_prompt,
            messages=list(session_msgs),
            provenance=provenance,
        )
        if self._hook_runner is not None:
            payload = ContextAssembledPayload(
                session_id=context.session_id or "",
                agent=context.agent_id or "",
                duration_ms=(time.monotonic() - started) * 1000,
                sections=provenance,
            )
            await self._hook_runner.dispatch(
                MemoryHookPoint.CONTEXT_ASSEMBLED,
                MemoryHookContext(
                    memory_context=context,
                    context_assembled=payload,
                ),
            )
        return result

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
                source=_DISCLAIMER_SOURCE,
                priority=110,
            )
        )

    async def _inject_core_memory(
        self,
        sections: list[_PromptSection],
        context: MemoryContext,
        memory_system: MemorySystem,
        query: str,
    ) -> None:
        """Inject core memory as natural XML with relative file names.

        The directory path is emitted once at the top of the section; each
        element carries only the filename (e.g. ``file="SOUL.md"``).
        """
        try:
            core_memory_contents = await memory_system.retrieve_core_memory(context, query=query)
            core_memory_dir = await memory_system.get_core_memory_directory(context)

            xml_parts: list[str] = []

            if core_memory_contents.soul:
                file_attr = ""
                if core_memory_dir:
                    file_attr = ' file="SOUL.md"'
                tag = CoreMemoryTag.YOUR_IDENTITY.value
                xml_parts.extend(
                    [
                        f'<{tag}{file_attr} editable="true"'
                        f' description="Who you are: personality, principles, and behavior rules">'
                        f"\n{xml_text(core_memory_contents.soul)}\n"
                        f"</{tag}>",
                    ]
                )

            if core_memory_contents.user:
                file_attr = ""
                if core_memory_dir:
                    file_attr = ' file="USER.md"'
                tag = CoreMemoryTag.USER_PROFILE.value
                xml_parts.extend(
                    [
                        f'<{tag}{file_attr} editable="true"'
                        f' description="Facts about the user: name, preferences, habits, communication style">'
                        f"\n{xml_text(core_memory_contents.user)}\n"
                        f"</{tag}>",
                    ]
                )

            if core_memory_contents.memory:
                file_attr = ""
                if core_memory_dir:
                    file_attr = ' file="MEMORY.md"'
                tag = CoreMemoryTag.KNOWN_FACTS.value
                xml_parts.extend(
                    [
                        f'<{tag}{file_attr} editable="false"'
                        f' description="Known facts about the project: conventions, decisions, verified solutions">'
                        f"\n{xml_text(core_memory_contents.memory)}\n"
                        f"</{tag}>",
                    ]
                )

            if xml_parts:
                xml_content = "\n".join(xml_parts)

                # Pre-truncation: if XML exceeds 8000 chars, truncate safely
                from modex_agent.memory.xml_truncate import truncate_xml_safe

                if len(xml_content) > 8000:
                    xml_content = truncate_xml_safe(
                        xml_content,
                        8000,
                        truncatable_paths=[
                            CoreMemoryTag.YOUR_IDENTITY.value,
                            CoreMemoryTag.USER_PROFILE.value,
                            CoreMemoryTag.KNOWN_FACTS.value,
                        ],
                    )

                dir_line = ""
                if core_memory_dir:
                    dir_line = (
                        f"Directory: {xml_attr(str(core_memory_dir.resolve()))}\n\n"
                    )
                heading = (
                    "### Core Memory Files\n\n"
                    "Self-maintained files storing your personality, user preferences, "
                    'and learned facts. Files with `editable="true"` can be updated '
                    "via file tools to evolve your core memory over time.\n"
                    f"{dir_line}\n"
                )
                sections.append(
                    _PromptSection(
                        content=heading + xml_content,
                        source=_CORE_MEMORY_SOURCE,
                        priority=100,
                    )
                )
        except Exception:
            logger.debug("Core memory injection skipped", exc_info=True)

    def _trim_by_priority(
        self,
        sections: list[_PromptSection],
    ) -> tuple[list[_PromptSection], list[SectionProvenance]]:
        """Sort by priority descending and optionally trim to token budget."""
        sorted_sections = sorted(sections, key=lambda s: s.priority, reverse=True)
        max_tokens = self._budget.max_system_prompt_tokens
        kept: list[_PromptSection] = []
        provenance: list[SectionProvenance] = []
        running = 0
        for sec in sorted_sections:
            retrieved_tokens = self._token_estimator.estimate_text(sec.content)
            if max_tokens is None or max_tokens <= 0 or running + retrieved_tokens <= max_tokens:
                injected_section = sec
            else:
                injected_section = self._trim_section_by_paragraphs(sec, max_tokens - running)

            injected_tokens = 0
            if injected_section is not None:
                kept.append(injected_section)
                injected_tokens = self._token_estimator.estimate_text(injected_section.content)
                running += injected_tokens
            provenance.append(
                SectionProvenance(
                    source=sec.source,
                    retrieved_tokens=retrieved_tokens,
                    injected_tokens=injected_tokens,
                    pruned_tokens=retrieved_tokens - injected_tokens,
                    priority=sec.priority,
                )
            )
        return kept, provenance

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
            source=section.source,
            priority=section.priority,
        )
