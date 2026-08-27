from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Final, assert_never

from modex_agent.agents.summarizer.consolidator import CoreMemoryConsolidator
from modex_agent.agents.summarizer.session_compactor import SessionCompactorAgent
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.core.types import LLMResponse, MessageRole, ToolCall
from modex_agent.memory.hooks import LlmUsage

_MODEL: Final = "scripted-model"


class _ProviderError(RuntimeError):
    pass


class _ScriptedProvider(CallbackStreamProvider):
    def __init__(self, responses: Sequence[LLMResponse | _ProviderError]) -> None:
        super().__init__()
        self._responses = iter(responses)

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict] | None = None,
        on_content_delta=None,
        on_reasoning_delta=None,
        **kwargs,
    ) -> LLMResponse:
        del messages, model, temperature, max_output_tokens, tools, kwargs
        item = next(self._responses)
        match item:
            case LLMResponse():
                return item
            case _ProviderError():
                raise item
            case unreachable:
                assert_never(unreachable)

    def get_default_model(self) -> str:
        return _MODEL


async def test_compact_preserves_summary_content_in_outcome() -> None:
    # Given
    compactor = SessionCompactorAgent(
        _ScriptedProvider([LLMResponse(content="## Objective\nPinned summary")])
    )

    # When
    outcome = await compactor.compact(
        [{"role": MessageRole.USER, "content": "Remember this"}],
        session_id="baseline-compact",
    )

    # Then
    assert outcome.summary == "## Objective\nPinned summary## Objective\nPinned summary"


async def test_consolidate_preserves_changed_flag_in_outcome(
    tmp_path: Path,
) -> None:
    # Given
    archive_base = tmp_path / "archive"
    archive_dir = archive_base / "1"
    archive_dir.mkdir(parents=True)
    (archive_dir / "knowledge.md").write_text("A durable fact", encoding="utf-8")
    core_memory_dir = tmp_path / "core"
    core_memory_dir.mkdir()
    consolidator = CoreMemoryConsolidator(
        _ScriptedProvider([LLMResponse(content="No update needed")])
    )

    # When
    outcome = await consolidator.consolidate(
        archive_ids=[1],
        archive_base=archive_base,
        core_memory_dir=core_memory_dir,
    )

    # Then
    assert outcome.changed is True


async def test_consolidate_accumulates_usage_across_retry_runs(tmp_path: Path) -> None:
    # Given
    archive_base, core_memory_dir = _memory_dirs(tmp_path)
    provider = _ScriptedProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(tool_name="missing_tool", arguments={}, call_id="one")],
                usage={
                    "prompt_tokens": 40,
                    "completion_tokens": 20,
                    "cache_read_input_tokens": 30,
                    "cache_creation_input_tokens": 40,
                },
            ),
            _ProviderError("fail after first response"),
            LLMResponse(
                content="retry succeeded",
                usage={
                    "input_tokens": 1,
                    "output_tokens": 2,
                    "cache_read_input_tokens": 3,
                    "cache_creation_input_tokens": 4,
                },
            ),
        ]
    )

    # When
    outcome = await CoreMemoryConsolidator(provider).consolidate(
        archive_ids=[1],
        archive_base=archive_base,
        core_memory_dir=core_memory_dir,
    )

    # Then
    assert outcome.changed is True
    assert outcome.usage == LlmUsage(
        model=_MODEL,
        calls=2,
        input_tokens=11,
        output_tokens=22,
        cache_read_tokens=33,
        cache_write_tokens=44,
    )


async def test_compact_provider_failure_preserves_empty_summary_semantics() -> None:
    # Given
    compactor = SessionCompactorAgent(_ScriptedProvider([_ProviderError("offline")]))

    # When
    outcome = await compactor.compact(
        [{"role": MessageRole.USER, "content": "remember"}],
        session_id="failed-compact",
    )

    # Then
    assert outcome.summary == ""
    assert outcome.usage is None


async def test_consolidate_provider_failure_preserves_false_semantics(tmp_path: Path) -> None:
    # Given
    archive_base, core_memory_dir = _memory_dirs(tmp_path)
    provider = _ScriptedProvider(
        [_ProviderError("offline"), _ProviderError("still offline")]
    )

    # When
    outcome = await CoreMemoryConsolidator(provider).consolidate(
        archive_ids=[1],
        archive_base=archive_base,
        core_memory_dir=core_memory_dir,
    )

    # Then
    assert outcome.changed is False
    assert outcome.usage is None


def _memory_dirs(tmp_path: Path) -> tuple[Path, Path]:
    archive_base = tmp_path / "archive"
    archive_dir = archive_base / "1"
    archive_dir.mkdir(parents=True)
    (archive_dir / "knowledge.md").write_text("A durable fact", encoding="utf-8")
    core_memory_dir = tmp_path / "core"
    core_memory_dir.mkdir()
    return archive_base, core_memory_dir
