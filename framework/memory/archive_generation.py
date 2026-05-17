from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from framework.memory.archive_input import DefaultArchiveInputPolicy, MessageMapping
from framework.memory.archive_models import (
    ArchiveChannel,
    ArchiveGenerationResult,
    ArchiveWrite,
)
from framework.memory.core.models import CompressionReason
from framework.memory.core.scope import MemoryContext
from framework.memory.utils import normalize_memory_summary


class ArchiveGenerationStrategy(Protocol):
    async def generate(
        self,
        messages: Sequence[MessageMapping],
        context: MemoryContext,
        reason: CompressionReason,
    ) -> ArchiveGenerationResult:
        raise NotImplementedError


class SummarizerLike(Protocol):
    async def summarize(
        self,
        text: str,
        *,
        prompt: str | None = None,
        max_tokens: int = 500,
        temperature: float = 0.3,
    ) -> str:
        raise NotImplementedError


class DualLLMArchiveGenerationStrategy:
    def __init__(
        self,
        *,
        summarizer: SummarizerLike,
        input_policy: DefaultArchiveInputPolicy | None = None,
        context_max_tokens: int = 800,
        knowledge_max_tokens: int = 600,
    ) -> None:
        self._summarizer = summarizer
        self._input_policy = input_policy or DefaultArchiveInputPolicy()
        self._context_max_tokens = context_max_tokens
        self._knowledge_max_tokens = knowledge_max_tokens

    async def generate(
        self,
        messages: Sequence[MessageMapping],
        context: MemoryContext,
        reason: CompressionReason,
    ) -> ArchiveGenerationResult:
        inputs = self._input_policy.build_inputs(messages, context, reason)
        writes: list[ArchiveWrite] = []

        from framework.agents.summarizer.agent import SummarizerAgent

        context_summary = await self._summarizer.summarize(
            self._prompt_input(inputs.context_transcript, reason),
            prompt=SummarizerAgent.PROMPT_CONTEXT_ARCHIVE,
            max_tokens=self._context_max_tokens,
        )
        normalized_context = normalize_memory_summary(context_summary)
        if normalized_context is not None:
            writes.append(ArchiveWrite(
                channel=ArchiveChannel.CONTEXT,
                summary=normalized_context,
                metadata={
                    "reason": reason.value,
                    "source": "compression",
                    "generation_strategy": "dual_llm",
                    "prompt": "context_archive",
                },
            ))

        knowledge_summary = await self._summarizer.summarize(
            self._prompt_input(inputs.knowledge_transcript, reason),
            prompt=SummarizerAgent.PROMPT_KNOWLEDGE_ARCHIVE,
            max_tokens=self._knowledge_max_tokens,
            temperature=0.2,
        )
        normalized_knowledge = normalize_memory_summary(knowledge_summary)
        if normalized_knowledge is not None:
            writes.append(ArchiveWrite(
                channel=ArchiveChannel.KNOWLEDGE,
                summary=normalized_knowledge,
                metadata={
                    "reason": reason.value,
                    "source": "compression",
                    "generation_strategy": "dual_llm",
                    "prompt": "knowledge_archive",
                },
            ))

        return ArchiveGenerationResult(writes=tuple(writes), inputs=inputs)

    @staticmethod
    def _prompt_input(transcript: str, reason: CompressionReason) -> str:
        return f"## Compression Reason\n{reason.value}\n\n## Transcript\n{transcript.strip()}"
