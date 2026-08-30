from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from unittest.mock import AsyncMock

from modex_agent.agents.summarizer.abc import CoreMemoryConsolidatorBase
from modex_agent.agents.summarizer.outcomes import (
    CompactionOutcome,
    ConsolidationOutcome,
)
from modex_agent.agents.summarizer.session_compactor import SessionCompactorAgent
from modex_agent.core.scope import MemoryContext
from modex_agent.memory.cleanup import cleanup_session
from modex_agent.memory.consolidation.dream_engine import DreamEngine
from modex_agent.memory.core.models import ArchiveEntry, CoreMemoryContents, UnprocessedResult
from modex_agent.memory.hooks import (
    CleanupFinishedHook,
    ConsolidationFinishedHook,
    ConsolidationFinishedPayload,
    LlmUsage,
    MemoryHookContext,
    MemoryHookRunner,
)
from modex_agent.memory.layers.factory import MemoryLayerFactory
from modex_agent.memory.registry import DefaultMemoryStoreRegistry
from modex_agent.memory.token_estimator import TokenEstimator


class _LengthEstimator(TokenEstimator):
    def estimate_text(self, text: str) -> int:
        return len(text)


class _StaticConsolidator(CoreMemoryConsolidatorBase):
    max_iterations = 2

    def __init__(self, outcome: ConsolidationOutcome) -> None:
        self._outcome = outcome

    async def consolidate(
        self,
        archive_ids: list[int],
        archive_base: Path,
        core_memory_dir: Path,
        *,
        max_iterations: int | None = None,
        invocation_id: str = "",
    ) -> ConsolidationOutcome:
        return self._outcome


class _RecordingConsolidationHook(ConsolidationFinishedHook):
    def __init__(self) -> None:
        self.payloads: list[ConsolidationFinishedPayload] = []

    async def on_consolidation_finished(self, ctx: MemoryHookContext) -> None:
        assert ctx.consolidation_finished is not None
        self.payloads.append(ctx.consolidation_finished)


class _UsageCompactor(SessionCompactorAgent):
    def __init__(self, usage: LlmUsage) -> None:
        self._usage = usage

    async def compact(
        self,
        messages: Sequence[dict[str, object]],
        previous_summary: str | None = None,
        *,
        session_id: str = "session-compactor",
    ) -> CompactionOutcome:
        return CompactionOutcome(
            summary="## Objective\n- keep telemetry honest",
            usage=self._usage,
        )

    @staticmethod
    def extract_topic(summary: str, max_chars: int = 200) -> str | None:
        return SessionCompactorAgent.extract_topic(summary, max_chars)


class _RecordingCleanupHook(CleanupFinishedHook):
    def __init__(self) -> None:
        self.contexts: list[MemoryHookContext] = []

    async def on_cleanup_finished(self, ctx: MemoryHookContext) -> None:
        self.contexts.append(ctx)


def _usage() -> LlmUsage:
    return LlmUsage(
        model="memory-model",
        calls=2,
        input_tokens=30,
        output_tokens=10,
        cache_read_tokens=4,
        cache_write_tokens=2,
    )


async def test_dream_run_dispatches_usage_entry_count_and_compression(
    tmp_path: Path,
) -> None:
    history = AsyncMock()
    history.get_unprocessed.return_value = UnprocessedResult(
        cursor=2,
        entries=[
            ArchiveEntry(summary="first", entry_id=1),
            ArchiveEntry(summary="second", entry_id=2),
        ],
    )
    history.get_storage_path.return_value = tmp_path / "archive"
    core = AsyncMock()
    core.get_storage_path.return_value = tmp_path / "core"
    core.get_all.side_effect = [
        CoreMemoryContents(memory="abcdefgh"),
        CoreMemoryContents(memory="abcd"),
    ]
    runner = MemoryHookRunner()
    recording = _RecordingConsolidationHook()
    runner.add(recording)
    usage = _usage()
    engine = DreamEngine(
        history_manager=history,
        long_term_manager=core,
        consolidator=_StaticConsolidator(
            ConsolidationOutcome(changed=True, usage=usage),
        ),
        hook_runner=runner,
        token_estimator=_LengthEstimator(),
    )

    changed = await engine.run(
        MemoryContext(session_id="dream-session", user_id="dream-user"),
    )

    assert changed is True
    [payload] = recording.payloads
    assert payload.session_id == "dream-session"
    assert payload.trigger == "dream"
    assert payload.changed is True
    assert payload.consumed_count == 2
    assert payload.before_tokens == 8
    assert payload.after_tokens == 4
    assert payload.compression_ratio == 0.5
    assert payload.usage == usage
    assert payload.duration_ms >= 0


async def test_cleanup_finished_carries_compaction_usage_and_duration(
    tmp_path: Path,
) -> None:
    registry = DefaultMemoryStoreRegistry(tmp_path)
    session = MemoryLayerFactory.single_user(registry=registry).session
    context = MemoryContext(session_id="cleanup-session", user_id="cleanup-user")
    for index in range(10):
        await session.add_messages(
            context,
            [{"role": "user", "content": f"message-{index}-abcdefghij"}],
        )
    usage = _usage()
    runner = MemoryHookRunner()
    recording = _RecordingCleanupHook()
    runner.add(recording)

    result = await cleanup_session(
        session=session,
        archive=None,
        context=context,
        compactor=_UsageCompactor(usage),
        max_context_tokens=50,
        max_token_ratio=0.8,
        keep_ratio=0.5,
        hook_runner=runner,
        token_estimator=_LengthEstimator(),
    )

    assert result.triggered is True
    assert result.usage == usage
    assert result.duration_ms >= 0  # duration can round to 0ms on fast machines — the propagation equality below is the contract
    [finished] = recording.contexts
    assert finished.cleanup_result is result
    cleanup_result = finished.cleanup_result
    assert cleanup_result is not None
    assert cleanup_result.usage == usage
    assert cleanup_result.duration_ms == result.duration_ms
