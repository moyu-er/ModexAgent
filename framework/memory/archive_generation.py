from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Union

from framework.memory.archive_input import DefaultArchiveInputPolicy, MessageMapping
from framework.memory.archive_models import (
    ArchiveChannel,
    ArchiveGenerationResult,
    ArchiveWrite,
)
from framework.memory.core.models import CompressionReason
from framework.memory.core.scope import MemoryContext
from framework.memory.utils import estimate_text_tokens, normalize_memory_summary


@dataclass(frozen=True)
class ArchiveInputMessage:
    """Lightweight, role-filtered representation of a chat message for archival.

    Non-essential fields are stripped per role:
    - assistant: tool_calls are preserved (needed for tool chain detection).
    - tool: keeps tool_call_id for correlation; drops name.
    - user: drops all metadata.
    """

    role: str
    content: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[dict[str, object], ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ArchiveInputMessage:
        role = str(data.get("role", ""))
        content = data.get("content")
        content_str = content if isinstance(content, str) else None

        tool_call_id: str | None = None
        if role == "tool":
            raw_id = data.get("tool_call_id")
            tool_call_id = raw_id if isinstance(raw_id, str) else None

        # Preserve tool_calls for assistant messages
        raw_tool_calls = data.get("tool_calls")
        tool_calls: tuple[dict[str, object], ...] = ()
        if isinstance(raw_tool_calls, list):
            tool_calls = tuple(tc for tc in raw_tool_calls if isinstance(tc, dict))

        return cls(role=role, content=content_str, tool_call_id=tool_call_id, tool_calls=tool_calls)


class ArchiveGenerationStrategy(ABC):
    @abstractmethod
    async def generate(
        self,
        messages: Sequence[ArchiveInputMessage],
        context: MemoryContext,
        reason: CompressionReason,
    ) -> ArchiveGenerationResult:
        raise NotImplementedError


class SummarizerLike(ABC):
    @abstractmethod
    async def summarize(
        self,
        text: str,
        *,
        prompt: str | None = None,
        max_tokens: int = 500,
        temperature: float = 0.3,
    ) -> str:
        raise NotImplementedError


class DualLLMArchiveGenerationStrategy(ArchiveGenerationStrategy):
    def __init__(
        self,
        *,
        summarizer: SummarizerLike,
        input_policy: DefaultArchiveInputPolicy | None = None,
        context_max_tokens: int = 800,
        knowledge_max_tokens: int = 600,
        max_segment_tokens: int = 12000,
    ) -> None:
        self._summarizer = summarizer
        self._input_policy = input_policy or DefaultArchiveInputPolicy()
        self._context_max_tokens = context_max_tokens
        self._knowledge_max_tokens = knowledge_max_tokens
        self._max_segment_tokens = max_segment_tokens

    async def generate(
        self,
        messages: Sequence[Union[ArchiveInputMessage, MessageMapping]],
        context: MemoryContext,
        reason: CompressionReason,
    ) -> ArchiveGenerationResult:
        normalized = self._normalize_messages(messages)
        # Keep original mappings alongside normalized messages so tool_calls
        # are preserved for the input_policy path that formats tool chains.
        original_mappings = self._to_mappings(messages)
        segments = self._segment(normalized)
        merged_indices = self._sliding_window_merge_indexed(normalized, segments)

        from framework.agents.summarizer.agent import SummarizerAgent

        context_parts: list[str] = []
        knowledge_parts: list[str] = []

        for indices in merged_indices:
            seg_mappings = [original_mappings[i] for i in indices]
            inputs = self._input_policy.build_inputs(seg_mappings, context, reason)

            context_summary = await self._summarizer.summarize(
                self._prompt_input(inputs.context_transcript, reason),
                prompt=SummarizerAgent.PROMPT_CONTEXT_ARCHIVE,
                max_tokens=self._context_max_tokens,
            )
            normalized_context = normalize_memory_summary(context_summary)
            if normalized_context:
                context_parts.append(normalized_context)

            knowledge_summary = await self._summarizer.summarize(
                self._prompt_input(inputs.knowledge_transcript, reason),
                prompt=SummarizerAgent.PROMPT_KNOWLEDGE_ARCHIVE,
                max_tokens=self._knowledge_max_tokens,
                temperature=0.2,
            )
            normalized_knowledge = normalize_memory_summary(knowledge_summary)
            if normalized_knowledge:
                knowledge_parts.append(normalized_knowledge)

        all_inputs = self._input_policy.build_inputs(original_mappings, context, reason)

        if not context_parts or not knowledge_parts:
            return ArchiveGenerationResult(writes=(), inputs=all_inputs)

        joined_context = "\n---\n".join(context_parts)
        joined_knowledge = "\n---\n".join(knowledge_parts)

        return ArchiveGenerationResult(
            writes=(
                ArchiveWrite(
                    channel=ArchiveChannel.CONTEXT,
                    summary=joined_context,
                    metadata={
                        "reason": reason.value,
                        "source": "compression",
                        "generation_strategy": "dual_llm",
                        "prompt": "context_archive",
                    },
                ),
                ArchiveWrite(
                    channel=ArchiveChannel.KNOWLEDGE,
                    summary=joined_knowledge,
                    metadata={
                        "reason": reason.value,
                        "source": "compression",
                        "generation_strategy": "dual_llm",
                        "prompt": "knowledge_archive",
                    },
                ),
            ),
            inputs=all_inputs,
        )

    # ------------------------------------------------------------------
    # Segmentation helpers
    # ------------------------------------------------------------------

    def _segment(self, messages: Sequence[ArchiveInputMessage]) -> list[list[ArchiveInputMessage]]:
        """Split messages into segments at user-turn boundaries.

        A new segment starts at each user message.  If there are no user
        messages the entire sequence forms a single segment.
        """
        if not messages:
            return []

        segments: list[list[ArchiveInputMessage]] = []
        current: list[ArchiveInputMessage] = []

        for msg in messages:
            if msg.role == "user" and current:
                segments.append(current)
                current = []
            current.append(msg)

        if current:
            segments.append(current)

        return segments

    def _sliding_window_merge_indexed(
        self,
        messages: Sequence[ArchiveInputMessage],
        segments: list[list[ArchiveInputMessage]],
    ) -> list[list[int]]:
        """Merge adjacent segments so each merged group stays within token budget.

        Returns lists of original indices into *messages* for each merged group.
        """
        if not segments:
            return []

        # Build index mapping: each message → its position in the original list
        index = 0
        indexed_segments: list[list[tuple[int, ArchiveInputMessage]]] = []
        for seg in segments:
            indexed_seg: list[tuple[int, ArchiveInputMessage]] = []
            for msg in seg:
                indexed_seg.append((index, msg))
                index += 1
            indexed_segments.append(indexed_seg)

        merged: list[list[int]] = []
        current: list[int] = []
        current_tokens = 0

        for indexed_seg in indexed_segments:
            seg_tokens = sum(estimate_text_tokens(m.content or "") for _, m in indexed_seg)

            if current and (current_tokens + seg_tokens > self._max_segment_tokens):
                merged.append(current)
                current = [i for i, _ in indexed_seg]
                current_tokens = seg_tokens
            else:
                current.extend(i for i, _ in indexed_seg)
                current_tokens += seg_tokens

        if current:
            merged.append(current)

        return merged

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_messages(
        messages: Sequence[Union[ArchiveInputMessage, MessageMapping]],
    ) -> list[ArchiveInputMessage]:
        """Convert mixed input to a uniform list of ArchiveInputMessage.

        Supports backward compatibility with callers that still pass raw dicts.
        """
        result: list[ArchiveInputMessage] = []
        for msg in messages:
            if isinstance(msg, ArchiveInputMessage):
                result.append(msg)
            elif isinstance(msg, Mapping):
                result.append(ArchiveInputMessage.from_dict(dict(msg)))
            else:
                raise TypeError(
                    f"Expected ArchiveInputMessage or Mapping, got {type(msg).__name__}"
                )
        return result

    @staticmethod
    def _to_mappings(
        messages: Sequence[Union[ArchiveInputMessage, MessageMapping]],
    ) -> list[MessageMapping]:
        """Normalize input to raw mappings, preserving tool_calls for build_inputs."""
        result: list[MessageMapping] = []
        for msg in messages:
            if isinstance(msg, Mapping):
                result.append(msg)
            elif isinstance(msg, ArchiveInputMessage):
                mapping: dict[str, object] = {"role": msg.role}
                if msg.content is not None:
                    mapping["content"] = msg.content
                if msg.tool_call_id is not None:
                    mapping["tool_call_id"] = msg.tool_call_id
                if msg.tool_calls:
                    mapping["tool_calls"] = list(msg.tool_calls)
                result.append(mapping)  # type: ignore[arg-type]
            else:
                raise TypeError(
                    f"Expected ArchiveInputMessage or Mapping, got {type(msg).__name__}"
                )
        return result

    @staticmethod
    def _prompt_input(transcript: str, reason: CompressionReason) -> str:
        return f"## Compression Reason\n{reason.value}\n\n## Transcript\n{transcript.strip()}"
