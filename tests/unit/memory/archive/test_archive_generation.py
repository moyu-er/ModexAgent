from __future__ import annotations

from abc import ABC
from collections.abc import Sequence
from dataclasses import dataclass

import pytest

from framework.memory.archive_generation import (
    ArchiveGenerationStrategy,
    ArchiveInputMessage,
    DualLLMArchiveGenerationStrategy,
    SummarizerLike,
)
from framework.memory.archive_models import (
    ArchiveChannel,
    ArchiveGenerationInputs,
    ArchiveGenerationResult,
    ArchiveInputStats,
)
from framework.memory.core.models import CompressionReason
from framework.memory.core.scope import MemoryContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeSummarizer(SummarizerLike):
    """Minimal async summarizer stub that records calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    async def summarize(
        self,
        text: str,
        *,
        prompt: str | None = None,
        max_tokens: int = 500,
        temperature: float = 0.3,
    ) -> str:
        _ = temperature
        self.calls.append((text, prompt or "", max_tokens))
        if "Context Archive" in (prompt or ""):
            return "## Situation\n- context summary"
        return "## User Facts\n- knowledge summary"


# ---------------------------------------------------------------------------
# ArchiveGenerationStrategy ABC tests
# ---------------------------------------------------------------------------


def test_archive_generation_strategy_is_abc() -> None:
    assert issubclass(ArchiveGenerationStrategy, ABC)


def test_archive_generation_strategy_cannot_instantiate_directly() -> None:
    with pytest.raises(TypeError):
        ArchiveGenerationStrategy()  # type: ignore[abstract]


def test_archive_generation_strategy_subclass_must_implement_generate() -> None:
    class IncompleteStrategy(ArchiveGenerationStrategy):
        pass

    with pytest.raises(TypeError):
        IncompleteStrategy()  # type: ignore[abstract]


def test_archive_generation_strategy_subclass_works() -> None:
    class CompleteStrategy(ArchiveGenerationStrategy):
        async def generate(
            self,
            messages: Sequence[ArchiveInputMessage],
            context: MemoryContext,
            reason: CompressionReason,
        ) -> ArchiveGenerationResult:
            from framework.memory.archive_models import ArchiveGenerationInputs, ArchiveInputStats
            return ArchiveGenerationResult(
                writes=(),
                inputs=ArchiveGenerationInputs(
                    context_transcript="", knowledge_transcript="",
                    stats=ArchiveInputStats(0, 0, 0, 0, 0),
                ),
            )

    strategy = CompleteStrategy()
    assert isinstance(strategy, ArchiveGenerationStrategy)


# ---------------------------------------------------------------------------
# SummarizerLike ABC tests
# ---------------------------------------------------------------------------


def test_summarizer_like_is_abc() -> None:
    assert issubclass(SummarizerLike, ABC)


def test_summarizer_like_cannot_instantiate_directly() -> None:
    with pytest.raises(TypeError):
        SummarizerLike()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# ArchiveInputMessage tests
# ---------------------------------------------------------------------------


def test_archive_input_message_from_chat_message_user() -> None:
    msg = ArchiveInputMessage.from_dict({
        "role": "user",
        "content": "Hello world",
        "metadata": {"timestamp": 123},
    })
    assert msg.role == "user"
    assert msg.content == "Hello world"
    assert msg.tool_call_id is None


def test_archive_input_message_from_chat_message_assistant_strips_tool_calls() -> None:
    msg = ArchiveInputMessage.from_dict({
        "role": "assistant",
        "content": "Let me check.",
        "tool_calls": [{"id": "call_1", "function": {"name": "search", "arguments": "{}"}}],
    })
    assert msg.role == "assistant"
    assert msg.content == "Let me check."
    assert msg.tool_call_id is None
    # tool_calls is now preserved for tool chain detection
    assert len(msg.tool_calls) == 1
    assert msg.tool_calls[0]["id"] == "call_1"


def test_archive_input_message_from_chat_message_tool() -> None:
    msg = ArchiveInputMessage.from_dict({
        "role": "tool",
        "content": "Result found",
        "tool_call_id": "call_abc123",
        "name": "search",
    })
    assert msg.role == "tool"
    assert msg.content == "Result found"
    assert msg.tool_call_id == "call_abc123"


def test_archive_input_message_user_strips_metadata() -> None:
    msg = ArchiveInputMessage.from_dict({
        "role": "user",
        "content": "hi",
        "metadata": {"timestamp": 123},
        "extra_field": "gone",
    })
    assert msg.role == "user"
    assert msg.content == "hi"
    assert msg.tool_call_id is None


def test_archive_input_message_preserves_tool_calls() -> None:
    """ArchiveInputMessage.from_dict must preserve tool_calls from assistant messages."""
    msg = {
        "role": "assistant",
        "content": "I will search for files.",
        "tool_calls": [
            {
                "id": "call_abc",
                "function": {
                    "name": "bash",
                    "arguments": '{"command": "find . -name \\"*.py\\""}',
                },
            }
        ],
    }
    result = ArchiveInputMessage.from_dict(msg)
    assert result.tool_calls == tuple(msg["tool_calls"])


def test_archive_input_message_tool_calls_empty_when_absent() -> None:
    """ArchiveInputMessage.from_dict returns empty tuple when no tool_calls."""
    msg = {"role": "user", "content": "hello"}
    result = ArchiveInputMessage.from_dict(msg)
    assert result.tool_calls == ()


# ---------------------------------------------------------------------------
# DualLLMArchiveGenerationStrategy — existing behaviour (backward compat)
# ---------------------------------------------------------------------------


async def test_dual_strategy_generates_context_and_knowledge_writes() -> None:
    summarizer = FakeSummarizer()
    strategy = DualLLMArchiveGenerationStrategy(summarizer=summarizer)

    result = await strategy.generate(
        [ArchiveInputMessage(role="user", content="remember I prefer concise answers")],
        MemoryContext(session_id="s1"),
        CompressionReason.MESSAGE_COUNT,
    )

    assert [write.channel for write in result.writes] == [
        ArchiveChannel.CONTEXT,
        ArchiveChannel.KNOWLEDGE,
    ]
    assert result.writes[0].summary.startswith("## Situation")
    assert result.writes[1].summary.startswith("## User Facts")
    assert summarizer.calls[0][2] == 800
    assert summarizer.calls[1][2] == 600


async def test_dual_strategy_skips_nothing_outputs() -> None:
    class NothingSummarizer(FakeSummarizer):
        async def summarize(
            self,
            text: str,
            *,
            prompt: str | None = None,
            max_tokens: int = 500,
            temperature: float = 0.3,
        ) -> str:
            _ = text, prompt, max_tokens, temperature
            return "(nothing)"

    strategy = DualLLMArchiveGenerationStrategy(summarizer=NothingSummarizer())

    result = await strategy.generate(
        [ArchiveInputMessage(role="user", content="hello")],
        MemoryContext(session_id="s1"),
        CompressionReason.MANUAL,
    )

    assert result.writes == ()


async def test_dual_strategy_partial_success_one_channel() -> None:
    class PartialSummarizer(FakeSummarizer):
        async def summarize(
            self,
            text: str,
            *,
            prompt: str | None = None,
            max_tokens: int = 500,
            temperature: float = 0.3,
        ) -> str:
            _ = text, max_tokens, temperature
            if "Context Archive" in (prompt or ""):
                return "## Situation\n- context summary"
            return "(nothing)"

    strategy = DualLLMArchiveGenerationStrategy(summarizer=PartialSummarizer())

    result = await strategy.generate(
        [ArchiveInputMessage(role="user", content="remember this")],
        MemoryContext(session_id="s1"),
        CompressionReason.MESSAGE_COUNT,
    )

    assert len(result.writes) == 1
    assert result.writes[0].channel == ArchiveChannel.CONTEXT


# ---------------------------------------------------------------------------
# Sliding window tests
# ---------------------------------------------------------------------------


def _make_messages(count: int, prefix: str = "msg") -> list[ArchiveInputMessage]:
    """Generate a sequence of alternating user/assistant messages."""
    result: list[ArchiveInputMessage] = []
    for i in range(count):
        role = "user" if i % 2 == 0 else "assistant"
        # Each message is ~20 chars; with 4 chars/token ~5 tokens per message.
        result.append(ArchiveInputMessage(role=role, content=f"{prefix}_{i} " * 4))
    return result


async def test_sliding_window_single_segment() -> None:
    """When messages fit within budget, only one LLM call per channel (2 total)."""
    summarizer = FakeSummarizer()
    strategy = DualLLMArchiveGenerationStrategy(
        summarizer=summarizer,
        max_segment_tokens=50000,  # very large budget
    )

    msgs = _make_messages(4)
    result = await strategy.generate(msgs, MemoryContext(), CompressionReason.MESSAGE_COUNT)

    assert len(result.writes) == 2
    # 2 summarizer calls: one for CONTEXT, one for KNOWLEDGE
    assert len(summarizer.calls) == 2


async def test_sliding_window_multiple_segments() -> None:
    """When messages exceed budget, multiple segments produce joined results."""
    summarizer = FakeSummarizer()
    strategy = DualLLMArchiveGenerationStrategy(
        summarizer=summarizer,
        max_segment_tokens=5,  # tiny budget forces segmentation
    )

    msgs = _make_messages(10)
    result = await strategy.generate(msgs, MemoryContext(), CompressionReason.MESSAGE_COUNT)

    assert len(result.writes) == 2
    # With 10 messages and tiny budget, should have more than 2 calls
    assert len(summarizer.calls) > 2


# ---------------------------------------------------------------------------
# Independent channel generation tests (Task 2 fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_channels_independent_success() -> None:
    """Context and knowledge archives are generated independently."""
    summarizer = FakeSummarizer()
    strategy = DualLLMArchiveGenerationStrategy(summarizer=summarizer)

    messages = [
        ArchiveInputMessage(role="user", content="hello"),
        ArchiveInputMessage(role="assistant", content="hi there"),
    ]

    # Force context to succeed but knowledge to fail
    original = summarizer.summarize

    async def mock_summarize(
        text: str,
        *,
        prompt: str | None = None,
        max_tokens: int = 500,
        temperature: float = 0.3,
    ) -> str:
        if "Context Archive" in (prompt or ""):
            return "## Situation\n- context summary"
        return ""  # Knowledge returns empty

    summarizer.summarize = mock_summarize  # type: ignore[method-assign]

    result = await strategy.generate(
        messages,
        MemoryContext(session_id="s1"),
        CompressionReason.MESSAGE_COUNT,
    )

    assert len(result.writes) == 1
    assert result.writes[0].channel == ArchiveChannel.CONTEXT


@pytest.mark.asyncio
async def test_archive_both_channels_empty_returns_no_writes() -> None:
    """When both channels produce empty content, no writes are generated."""
    summarizer = FakeSummarizer()
    strategy = DualLLMArchiveGenerationStrategy(summarizer=summarizer)

    messages = [
        ArchiveInputMessage(role="user", content="hello"),
    ]

    async def mock_summarize(
        text: str,
        *,
        prompt: str | None = None,
        max_tokens: int = 500,
        temperature: float = 0.3,
    ) -> str:
        return ""  # Both channels return empty

    summarizer.summarize = mock_summarize  # type: ignore[method-assign]

    result = await strategy.generate(
        messages,
        MemoryContext(session_id="s1"),
        CompressionReason.MESSAGE_COUNT,
    )

    assert len(result.writes) == 0


# ---------------------------------------------------------------------------
# MAJOR 1: Archive generation uses PromptRegistry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dual_llm_strategy_uses_prompt_registry_when_provided() -> None:
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from framework.memory.prompts import PromptRegistry

    with TemporaryDirectory() as tmpdir:
        prompts_dir = Path(tmpdir)
        (prompts_dir / "archive").mkdir()
        (prompts_dir / "archive" / "context_archive_system.md").write_text("REGISTRY_CTX_SYSTEM")
        (prompts_dir / "archive" / "knowledge_archive_system.md").write_text("REGISTRY_KN_SYSTEM")

        registry = PromptRegistry(prompts_dir)
        summarizer = FakeSummarizer()
        strategy = DualLLMArchiveGenerationStrategy(
            summarizer=summarizer,
            prompts=registry,
        )

        messages = [
            ArchiveInputMessage(role="user", content="hello"),
            ArchiveInputMessage(role="assistant", content="hi there"),
        ]

        result = await strategy.generate(
            messages,
            MemoryContext(session_id="s1"),
            CompressionReason.MESSAGE_COUNT,
        )

        assert len(summarizer.calls) >= 1
        for _, prompt_text, _ in summarizer.calls:
            assert (
                "REGISTRY_CTX_SYSTEM" in prompt_text
                or "REGISTRY_KN_SYSTEM" in prompt_text
                or "Context Archive" in prompt_text
                or "Knowledge Archive" in prompt_text
            )
