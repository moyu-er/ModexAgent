from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from bot.eval.harbor.entry import (
    EntryConfig,
    EntryDependencies,
    HarborToolset,
    TaskResultArtifact,
    _TraceStore,
    execute_entry,
)

from modex_agent.agents.react.agent import ReActEvent
from modex_agent.core.agent import AgentContext
from modex_agent.core.emitter import AgentResult, ContentEmitter, StopReason
from modex_agent.core.llm_struct import FinishReason, LLMResponse
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.runtime.models import JsonValue
from modex_agent.tools.terminal.persistent_bash import persistent_bash_supported
from modex_agent.trace.experiment_attrs import ExperimentAttribute
from modex_agent.trace.semconv import GenAiAttr, SpanName
from modex_agent.trace.store import SpanModel


class ScriptedProvider(CallbackStreamProvider):
    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_content_delta=None,
        on_reasoning_delta=None,
        **kwargs: JsonValue,
    ) -> LLMResponse:
        _ = messages, model, temperature, max_output_tokens, tools, kwargs
        return LLMResponse(
            content="scripted answer",
            finish_reason=FinishReason.STOP,
            usage={
                "prompt_tokens": 14,
                "completion_tokens": 7,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 2,
            },
        )

    def get_default_model(self) -> str:
        return "openai/scripted-model"


class RecordingExporter:
    def __init__(self) -> None:
        self.spans: list[SpanModel] = []

    async def export(self, span: SpanModel) -> None:
        self.spans.append(span)


def _environment(tmp_path: Path, *, endpoint: str = "http://collector:4318/v1/traces") -> Mapping[str, str]:
    input_dir = tmp_path / "task"
    input_dir.mkdir()
    (input_dir / "instruction.txt").write_text("Create the requested artifact.", encoding="utf-8")
    return {
        "LLM_MODEL": "openai/scripted-model",
        "LLM_API_KEY": "test-key",
        "LLM_BASE_URL": "http://provider.invalid/v1",
        "OTEL_TRACES_ENDPOINT": endpoint,
        "MODEX_EXPERIMENT_ID": "exp-id",
        "MODEX_EXPERIMENT_NAME": "terminal-bench.run-1",
        "MODEX_EXPERIMENT_DATASET_ID": "dataset-id",
        "MODEX_EXPERIMENT_ITEM_ID": "item-id",
        "MODEX_MEMORY_NS": "memory-arm",
        "MODEX_TOOLSET": "none",
        "MODEX_DENY_TOOLS": "bash,terminal",
        "MODEX_TASK_INPUT_DIR": str(input_dir),
        "MODEX_TASK_NAME": "regex-log",
        "MODEX_AGENT_OUTPUT_DIR": str(tmp_path / "agent-logs"),
        "MODEX_MAX_ITERATIONS": "4",
    }


def test_entry_config_parses_container_environment_without_io(tmp_path: Path) -> None:
    environment = _environment(tmp_path)

    config = EntryConfig.from_environment(environment)

    assert config.otel_traces_endpoint == "http://collector:4318/v1/traces"
    assert config.experiment.experiment_id == "exp-id"
    assert config.experiment.experiment_name == "terminal-bench.run-1"
    assert config.experiment.dataset_id == "dataset-id"
    assert config.experiment.item_id == "item-id"
    assert config.memory_namespace == "memory-arm"
    assert config.toolset is HarborToolset.NONE
    assert config.denied_tools == ("bash", "terminal")
    assert config.task_name == "regex-log"
    assert config.instruction_path == config.task_input_dir / "instruction.txt"
    assert config.output_dir == tmp_path / "agent-logs"
    assert config.max_iterations == 4


def test_entry_config_resolves_task_workspace_from_cwd_and_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_cwd = tmp_path / "app"
    fake_cwd.mkdir()
    monkeypatch.setattr(Path, "cwd", lambda: fake_cwd)
    environment = dict(_environment(tmp_path))

    defaulted = EntryConfig.from_environment(dict(environment, MODEX_TASK_NAME=""))
    assert defaulted.task_name is None
    assert defaulted.task_workspace == fake_cwd

    overridden = EntryConfig.from_environment(
        dict(environment, MODEX_TASK_WORKSPACE=str(tmp_path / "workdir"))
    )
    assert overridden.task_workspace == tmp_path / "workdir"


@pytest.mark.asyncio
async def test_bare_tools_resolve_relative_paths_against_task_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_cwd = tmp_path / "app"
    fake_cwd.mkdir()
    monkeypatch.setattr(Path, "cwd", lambda: fake_cwd)
    config = EntryConfig.from_environment(
        dict(_environment(tmp_path), MODEX_TOOLSET="read_write")
    )

    async def write_marker(
        context: AgentContext,
        emitter: ContentEmitter[ReActEvent],
    ) -> AgentResult:
        _ = emitter
        write = context.tool_manager.get_tool("write")
        assert write is not None
        await write.execute(path="marker.txt", content="primers")
        return AgentResult(content="written", stop_reason=StopReason.COMPLETED)

    outcome = await execute_entry(
        config,
        EntryDependencies(
            provider=ScriptedProvider(),
            turn_executor=write_marker,
        ),
    )

    assert outcome.error is None
    assert (fake_cwd / "marker.txt").read_text(encoding="utf-8") == "primers"


@pytest.mark.asyncio
async def test_entry_turn_context_pins_tool_denial_and_iteration_budget(
    tmp_path: Path,
) -> None:
    environment = dict(
        _environment(tmp_path),
        MODEX_TOOLSET="read_write",
        MODEX_DENY_TOOLS="grep",
    )
    config = EntryConfig.from_environment(environment)

    async def capture_turn(
        context: AgentContext,
        emitter: ContentEmitter[ReActEvent],
    ) -> AgentResult:
        _ = emitter
        assert context.max_iterations == 4
        expected = ["read", "write", "edit", "ls", "glob", "bash"]
        if persistent_bash_supported():
            expected.append("bash_input")
        assert context.tool_manager.list_tools() == expected
        return AgentResult(content="captured", stop_reason=StopReason.COMPLETED)

    outcome = await execute_entry(
        config,
        EntryDependencies(
            provider=ScriptedProvider(),
            turn_executor=capture_turn,
        ),
    )

    assert outcome.error is None
    assert outcome.output == "captured"


@pytest.mark.asyncio
async def test_execute_entry_maps_root_linkage_chat_semantics_and_artifacts(tmp_path: Path) -> None:
    config = EntryConfig.from_environment(_environment(tmp_path))
    exporter = RecordingExporter()
    dependencies = EntryDependencies(
        provider=ScriptedProvider(),
        span_exporter=exporter.export,
    )

    outcome = await execute_entry(config, dependencies)

    root = next(span for span in exporter.spans if span.parent_span_id is None)
    assert root.name == "invoke_agent/regex-log"
    assert root.attributes[GenAiAttr.CONVERSATION_ID] == "harbor_regex-log_item-id"
    expected_experiment_attrs = {
        ExperimentAttribute.ID.value: "exp-id",
        ExperimentAttribute.NAME.value: "terminal-bench.run-1",
        ExperimentAttribute.DATASET_ID.value: "dataset-id",
        ExperimentAttribute.ITEM_ID.value: "item-id",
        ExperimentAttribute.ITEM_ROOT_OBSERVATION_ID.value: root.span_id,
    }
    assert {
        key: value
        for key, value in root.attributes.items()
        if key.startswith("langfuse.experiment.")
    } == expected_experiment_attrs

    chat = next(span for span in exporter.spans if span.name == SpanName.CHAT)
    assert chat.attributes[GenAiAttr.REQUEST_MODEL] == "openai/scripted-model"
    assert chat.attributes[GenAiAttr.USAGE_INPUT_TOKENS] == 11
    assert chat.attributes[GenAiAttr.USAGE_OUTPUT_TOKENS] == 7
    assert chat.attributes[GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS] == 3
    assert chat.attributes[GenAiAttr.USAGE_CACHE_CREATION_INPUT_TOKENS] == 2

    trace_records = [
        json.loads(line)
        for line in (config.output_dir / "trace-ids.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert trace_records == [
        {
            "trace_id": outcome.trace_id,
            "turn": 1,
            "experiment_id": "exp-id",
            "item_id": "item-id",
            "task_name": "regex-log",
        }
    ]
    result = TaskResultArtifact.model_validate_json(
        (config.output_dir / "result.json").read_text(encoding="utf-8")
    )
    assert result.error is None
    assert result.output == "scripted answer"
    assert result.dropped_span_count == 0
    assert (config.output_dir / "instruction-rendered.txt").is_file()
    assert (config.output_dir / "trajectory.jsonl").is_file()
    assert (config.output_dir / "usage.json").is_file()
    assert (config.output_dir / "summary.md").is_file()


@pytest.mark.asyncio
async def test_missing_otlp_endpoint_warns_counts_drops_and_keeps_artifacts(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = EntryConfig.from_environment(_environment(tmp_path, endpoint=""))

    with caplog.at_level(logging.WARNING):
        outcome = await execute_entry(
            config,
            EntryDependencies(provider=ScriptedProvider()),
        )

    result = TaskResultArtifact.model_validate_json(
        (config.output_dir / "result.json").read_text(encoding="utf-8")
    )
    assert outcome.error is None
    assert result.output == "scripted answer"
    assert result.dropped_span_count > 0
    assert "OTEL_TRACES_ENDPOINT is missing" in caplog.text
    assert (config.output_dir / "trace-ids.jsonl").is_file()
    assert (config.output_dir / "trajectory.jsonl").is_file()
    assert (config.output_dir / "usage.json").is_file()
    assert (config.output_dir / "summary.md").is_file()


@pytest.mark.asyncio
async def test_agent_turn_exception_records_failure_and_trace_mapping(tmp_path: Path) -> None:
    config = EntryConfig.from_environment(_environment(tmp_path))

    async def fail_turn(
        context: AgentContext,
        emitter: ContentEmitter[ReActEvent],
    ) -> AgentResult:
        _ = context, emitter
        raise RuntimeError("scripted turn failure")

    outcome = await execute_entry(
        config,
        EntryDependencies(
            provider=ScriptedProvider(),
            turn_executor=fail_turn,
        ),
    )

    result = TaskResultArtifact.model_validate_json(
        (config.output_dir / "result.json").read_text(encoding="utf-8")
    )
    assert outcome.error == "scripted turn failure"
    assert result.error == "scripted turn failure"
    assert result.trace_id == outcome.trace_id
    trace_record = json.loads(
        (config.output_dir / "trace-ids.jsonl").read_text(encoding="utf-8").strip()
    )
    assert trace_record["trace_id"] == outcome.trace_id
    assert (config.output_dir / "trajectory.jsonl").is_file()
    assert (config.output_dir / "usage.json").is_file()
    assert (config.output_dir / "summary.md").is_file()


def test_entry_config_parses_langfuse_basic_auth(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    absent = EntryConfig.from_environment(environment)
    assert absent.langfuse_basic_auth is None

    present = EntryConfig.from_environment(
        dict(environment, LANGFUSE_BASIC_AUTH="dGVzdC1iYXNpYy1hdXRo")
    )
    assert present.langfuse_basic_auth == "dGVzdC1iYXNpYy1hdXRo"


def test_trace_store_sends_otlp_authorization_header_when_basic_auth_present(
    tmp_path: Path,
) -> None:
    config = EntryConfig.from_environment(
        dict(_environment(tmp_path), LANGFUSE_BASIC_AUTH="dGVzdC1iYXNpYy1hdXRo")
    )

    store = _TraceStore(config, None)
    try:
        assert store._otlp_headers == {"Authorization": "Basic dGVzdC1iYXNpYy1hdXRo"}
    finally:
        store.close()


def test_trace_store_omits_otlp_headers_without_basic_auth(tmp_path: Path) -> None:
    config = EntryConfig.from_environment(_environment(tmp_path))

    store = _TraceStore(config, None)
    try:
        assert store._otlp_headers == {}
    finally:
        store.close()
