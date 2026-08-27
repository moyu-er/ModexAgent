from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from bot.eval.agent_harness import static_system_prompt
from bot.eval.replay import (
    FingerprintValues,
    GoldenCase,
    GoldenMeta,
    GoldenReplayConfig,
    GoldenReplayRunner,
    ToolStats,
    merge_cassettes,
)
from bot.eval.task_output import EvalTaskOutput, TurnRecord
from bot.eval.task_spec import EvalItemSpec

from modex_agent.core.constants import StopReason
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.core.types import LLMResponse, MessageRole
from modex_agent.trace.cassette import (
    CassetteCategory,
    CassetteEntry,
    CassetteManifest,
    CassetteReplayEngine,
    llm_call_key,
)


class _OfflineProvider(CallbackStreamProvider):
    def get_default_model(self) -> str:
        return "fixture-model"

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_content_delta: Callable[[str], Any] | None = None,
        on_reasoning_delta: Callable[[str], Any] | None = None,
        **kwargs: object,
    ) -> LLMResponse:
        raise AssertionError("golden replay must never call a live provider")


def _payload(content: str) -> dict[str, Any]:
    return {
        "request": {},
        "response": {
            "content": content,
            "tool_calls": [],
            "reasoning_content": None,
            "finish_reason": "stop",
            "usage": {},
            "error": None,
        },
        "latency_ms": 1.0,
    }


def _write_cassette(directory: Path, entries: list[tuple[str, dict[str, Any]]]) -> Path:
    directory.mkdir(parents=True)
    manifest_entries = [
        CassetteEntry(
            category=CassetteCategory.LLM_CALL,
            key=key,
            data=payload,
            timestamp=1.0,
        )
        for key, payload in entries
    ]
    manifest = CassetteManifest(
        trace_id=directory.name,
        entries=manifest_entries,
        created_at=1.0,
    )
    (directory / "index.json").write_text(
        manifest.model_dump_json(indent=2),
        encoding="utf-8",
    )
    for key, payload in entries:
        (directory / f"{key}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return directory


def _request_key(content: str) -> str:
    message = ChatMessage(role=MessageRole.USER, content=content)
    return llm_call_key([message.to_dict()], None, None, None, None, {})


def _write_case(directory: Path, *, baseline: bool, assertions: list[dict[str, str]]) -> GoldenCase:
    directory.mkdir(parents=True)
    item = {
        "id": directory.name,
        "turns": [{"user": "fixture turn"}],
        "toolset": "none",
        "world_assertions": assertions,
    }
    (directory / "item.json").write_text(json.dumps(item), encoding="utf-8")
    prompt = static_system_prompt("Fixture prompt")
    meta = GoldenMeta(
        model="fixture-model",
        temperature=0.7,
        tool_names=[],
        tool_schema_sha256=hashlib.sha256(b"[]").hexdigest(),
        prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        platform=sys.platform,
        recorded_at="2026-08-15T00:00:00+00:00",
        baseline=baseline,
    )
    (directory / "meta.json").write_text(meta.model_dump_json(), encoding="utf-8")
    _write_cassette(directory / "cassette" / "trace", [])
    return GoldenCase(name=directory.name, dir=directory)


def test_fingerprint_mismatch_raises_with_field_diff(tmp_path: Path) -> None:
    case = _write_case(tmp_path / "case", baseline=True, assertions=[])
    constructed = FingerprintValues(
        model="different-model",
        temperature=0.7,
        tool_names=[],
        tool_schema_sha256=hashlib.sha256(b"[]").hexdigest(),
        prompt_sha256=GoldenMeta.model_validate_json(
            (case.dir / "meta.json").read_text(encoding="utf-8")
        ).prompt_sha256,
        platform=sys.platform,
    )

    with pytest.raises(ValueError, match=r"model:.*fixture-model.*different-model"):
        GoldenReplayRunner.check_fingerprint(case, constructed)


async def test_merge_cassettes_unions_replayable_answers(tmp_path: Path) -> None:
    first_key = _request_key("first")
    second_key = _request_key("second")
    first = _write_cassette(tmp_path / "first", [(first_key, _payload("one"))])
    second = _write_cassette(tmp_path / "second", [(second_key, _payload("two"))])

    merged = merge_cassettes([first, second])
    engine = CassetteReplayEngine(merged)
    manifest = engine.load()
    provider = engine.wrap_provider(_OfflineProvider())

    first_result = await provider.chat(
        messages=[ChatMessage(role=MessageRole.USER, content="first")]
    )
    second_result = await provider.chat(
        messages=[ChatMessage(role=MessageRole.USER, content="second")]
    )
    assert first_result.content == "one"
    assert second_result.content == "two"
    assert len(manifest.entries) == 2


def test_merge_cassettes_rejects_differing_duplicate_payloads(tmp_path: Path) -> None:
    key = _request_key("duplicate")
    first = _write_cassette(tmp_path / "first", [(key, _payload("one"))])
    second = _write_cassette(tmp_path / "second", [(key, _payload("two"))])

    with pytest.raises(ValueError, match=key) as exc_info:
        merge_cassettes([first, second])

    assert str(first / f"{key}.json") in str(exc_info.value)
    assert str(second / f"{key}.json") in str(exc_info.value)


def test_merge_cassettes_dedupes_identical_duplicate_payloads(tmp_path: Path) -> None:
    key = _request_key("duplicate")
    payload = _payload("same")
    first = _write_cassette(tmp_path / "first", [(key, payload)])
    second = _write_cassette(tmp_path / "second", [(key, payload)])

    merged = merge_cassettes([first, second])
    engine = CassetteReplayEngine(merged)
    manifest = engine.load()

    assert len(manifest.entries) == 1
    assert len(list(merged.glob(f"{key}.json"))) == 1


async def test_run_suite_merges_cassettes_across_cases(tmp_path: Path) -> None:
    first = _write_case(tmp_path / "first", baseline=True, assertions=[])
    second = _write_case(tmp_path / "second", baseline=True, assertions=[])
    key = _request_key("shared")
    _write_cassette(first.dir / "cassette" / "extra", [(key, _payload("one"))])
    _write_cassette(second.dir / "cassette" / "extra", [(key, _payload("two"))])
    runner = GoldenReplayRunner(
        GoldenReplayConfig(
            model="fixture-model",
            temperature=0.7,
            system_prompt="Fixture prompt",
        )
    )

    with pytest.raises(ValueError, match=key):
        await runner.run_suite(tmp_path)


async def test_run_case_fails_when_react_swallows_cassette_miss(tmp_path: Path) -> None:
    case = _write_case(
        tmp_path / "miss",
        baseline=False,
        assertions=[{"kind": "file_absent", "path": "missing.txt"}],
    )

    runner = GoldenReplayRunner(
        GoldenReplayConfig(
            model="fixture-model",
            temperature=0.7,
            system_prompt="Fixture prompt",
        )
    )

    result = await runner.run_case(case)

    assert result.cassette_misses > 0
    assert result.turn_outcomes[0].stop_reason == StopReason.ERROR
    assert result.passed is False


async def test_run_case_rejects_vacuous_oracle_without_baseline(tmp_path: Path) -> None:
    case = _write_case(tmp_path / "vacuous", baseline=False, assertions=[])

    async def clean_result(spec: EvalItemSpec) -> EvalTaskOutput:
        return EvalTaskOutput(
            output="done",
            stop_reason=StopReason.COMPLETED,
            error=None,
            world_results=[],
            tool_stats=ToolStats(total=0, errors=0, success_rate=1.0, source="messages"),
            turns_executed=1,
            stop_mismatches=[],
            turn_records=[
                TurnRecord(stop_reason=StopReason.COMPLETED, error=None, content="done")
            ],
        )

    runner = GoldenReplayRunner(
        GoldenReplayConfig(
            model="fixture-model",
            temperature=0.7,
            system_prompt="Fixture prompt",
        ),
        runner_factory=clean_result,
    )

    result = await runner.run_case(case)

    assert result.cassette_misses == 0
    assert result.passed is False
