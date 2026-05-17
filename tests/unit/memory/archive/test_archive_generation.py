from __future__ import annotations

from framework.memory.archive_generation import DualLLMArchiveGenerationStrategy
from framework.memory.archive_models import ArchiveChannel
from framework.memory.core.models import CompressionReason
from framework.memory.core.scope import MemoryContext


class FakeSummarizer:
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


async def test_dual_strategy_generates_context_and_knowledge_writes() -> None:
    summarizer = FakeSummarizer()
    strategy = DualLLMArchiveGenerationStrategy(summarizer=summarizer)

    result = await strategy.generate(
        [{"role": "user", "content": "remember I prefer concise answers"}],
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
        [{"role": "user", "content": "hello"}],
        MemoryContext(session_id="s1"),
        CompressionReason.MANUAL,
    )

    assert result.writes == ()


async def test_dual_strategy_requires_complete_archive_pair() -> None:
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
        [{"role": "user", "content": "remember this"}],
        MemoryContext(session_id="s1"),
        CompressionReason.MESSAGE_COUNT,
    )

    assert result.writes == ()
