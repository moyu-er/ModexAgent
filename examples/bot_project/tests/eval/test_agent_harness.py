from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import bot.eval.agent_harness as agent_harness
import pytest
from bot.eval.agent_harness import (
    _build_score_injector,
    _eval_observability,
    _WorkspaceTokenNormalizer,
    build_runtime_services,
    build_trace_only_services,
    static_system_prompt,
    wrap_provider,
)

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.capabilities import ModelCapabilities
from modex_agent.core.llm_struct import FinishReason, LLMResponse
from modex_agent.core.message import ChatMessage, ImageUrl, ImageUrlPart, MessageRole, TextPart
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import (
    Tool,
    ToolConfig,
    ToolExecutionContext,
    ToolManager,
    ToolResult,
)
from modex_agent.ioc.configs.observability import TraceBackend
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import JsonValue, TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.runtime.store import InMemoryTurnStateStore
from modex_agent.tools.manager import InMemoryToolManager
from modex_agent.trace.cassette import (
    CassetteCategory,
    CassetteFlushHook,
    CassetteRecorder,
    CassetteReplayEngine,
)
from modex_agent.trace.chat_span_hook import ChatSpanHook
from modex_agent.trace.semconv import GenAiAttr


class _ScriptedToolManager(ToolManager):
    def __init__(self, result: ToolResult) -> None:
        super().__init__()
        self.result = result
        self.descriptions: list[dict[str, Any]] = [
            {"type": "function", "function": {"name": "fixture"}}
        ]

    def register(self, tool: Tool, config: ToolConfig | None = None) -> None:
        raise AssertionError("register should be delegated but is not used by this test")

    def unregister(self, tool_name: str) -> bool:
        return False

    def get_tool(self, tool_name: str) -> Tool | None:
        return None

    def list_tools(self) -> list[str]:
        return ["fixture"]

    def is_registered(self, tool_name: str) -> bool:
        return tool_name == "fixture"

    def get_tool_descriptions(self, caps: ModelCapabilities | None = None) -> list[dict[str, Any]]:
        return self.descriptions

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        ctx: ToolExecutionContext | None = None,
    ) -> ToolResult:
        return self.result


class _ScriptedProvider(CallbackStreamProvider):
    def __init__(self, response: LLMResponse) -> None:
        super().__init__()
        self._response = response
        self.models: list[str | None] = []

    def get_default_model(self) -> str:
        return "fixture-model"

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_content_delta: Callable[[str], Any] | None = None,
        on_reasoning_delta: Callable[[str], Any] | None = None,
        **kwargs: JsonValue,
    ) -> LLMResponse:
        self.models.append(model)
        return self._response


class _RaisingProvider(_ScriptedProvider):
    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_content_delta: Callable[[str], Any] | None = None,
        on_reasoning_delta: Callable[[str], Any] | None = None,
        **kwargs: JsonValue,
    ) -> LLMResponse:
        raise AssertionError("replay must not call the wrapped provider")


def test_declared_harness_assembly_seam_is_available() -> None:
    # Given / When
    assembly_seam = getattr(agent_harness, "assemble_harness_agent", None)

    # Then
    assert callable(assembly_seam)


async def test_workspace_token_normalizer_rewrites_all_text_parts_only(
    tmp_path: Path,
) -> None:
    workspace = tmp_path.resolve()
    image = ImageUrlPart(image_url=ImageUrl(url="https://example.invalid/image.png"))
    inner_result = ToolResult(
        tool_name="fixture",
        content=[
            TextPart(text=f"before {workspace} middle {workspace} after"),
            image,
        ],
    )
    inner = MagicMock(spec=Tool)
    inner.name = "fixture"
    inner.description = "fixture"
    inner.parameters = {"type": "object", "properties": {}}
    inner.config = ToolConfig()
    inner.execute = AsyncMock(return_value=inner_result)
    normalizer = _WorkspaceTokenNormalizer(inner, workspace)

    result = await normalizer.execute()

    assert str(workspace) not in result.message_content()
    assert result.message_content() == "before <workspace> middle <workspace> after"
    assert result.content[1] == image


async def test_wrap_provider_records_and_replays_provider_calls(tmp_path: Path) -> None:
    response = LLMResponse(
        content="recorded response",
        finish_reason=FinishReason.STOP,
    )
    recorder = CassetteRecorder(tmp_path)
    messages = [ChatMessage(role=MessageRole.USER, content="fixture request")]

    recording_provider = _ScriptedProvider(response)
    recorded = await wrap_provider(recording_provider, recorder).chat(
        messages=messages,
        temperature=0.25,
    )

    assert recorded == response
    assert len(recorder.entries) == 1
    assert recorder.entries[0].category is CassetteCategory.LLM_CALL
    assert recorder.entries[0].data["request"]["model"] == "fixture-model"
    assert recording_provider.models == ["fixture-model"]

    cassette_dir = recorder.save("trace-provider-wrap")
    replay = CassetteReplayEngine(cassette_dir)
    replay.load()
    replayed = await wrap_provider(_RaisingProvider(response), replay).chat(
        messages=messages,
        temperature=0.25,
    )

    assert replayed == response


def test_build_runtime_services_registers_production_services(tmp_path: Path) -> None:
    services = build_runtime_services(tmp_path / "traces")

    assert services.hooks is not None
    assert services.governance is None
    assert isinstance(services.turn_store, InMemoryTurnStateStore)
    assert services.trace_store is not None
    hook_names = {spec.hook.name for spec in services.hooks.hook_specs}
    assert {"loop_detection", "checkpoint"} <= hook_names
    assert "cassette_flush" not in hook_names


def test_build_runtime_services_adds_flush_hook_for_recorder(tmp_path: Path) -> None:
    recorder = CassetteRecorder(tmp_path / "cassettes")
    services = build_runtime_services(tmp_path / "traces", recorder)

    assert services.hooks is not None
    flush_hooks = [
        spec.hook for spec in services.hooks.hook_specs if isinstance(spec.hook, CassetteFlushHook)
    ]
    assert len(flush_hooks) == 1


def test_build_trace_only_services_excludes_governance_and_runtime_hooks(tmp_path: Path) -> None:
    """build_trace_only_services must register ONLY trace hooks — no governance, loop, checkpoint, or turn_store."""
    services = build_trace_only_services(tmp_path / "traces")

    assert services.hooks is not None
    assert services.governance is None, "clean mode must not have governance"
    assert services.turn_store is None, "clean mode must not have turn_store"
    assert services.trace_store is not None, "trace store must exist for span writing"
    hook_names = {spec.hook.name for spec in services.hooks.hook_specs}
    assert "loop_detection" not in hook_names, "clean mode must not have LoopDetectionHook"
    assert "checkpoint" not in hook_names, "clean mode must not have CheckpointHook"
    assert "cassette_flush" not in hook_names, "clean mode must not have CassetteFlushHook"
    trace_hook_names = {
        "RootSpanHook", "ChatSpanHook", "ToolSpanHook",
        "HandoffSpanHook", "ApprovalSpanHook",
        "AgentStartSpanHook", "IterationSpanHook",
    }
    assert trace_hook_names <= hook_names, f"trace hooks missing: {trace_hook_names - hook_names}"


@pytest.mark.parametrize("service_builder", [build_runtime_services, build_trace_only_services])
@pytest.mark.parametrize("model", ["openai/step-3.7-flash", None])
async def test_service_builder_records_explicit_model_on_chat_span(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    service_builder: Callable[..., AgentRuntimeServices],
    model: str | None,
) -> None:
    # Given
    monkeypatch.setenv("OTEL_FORMAT", "file")
    session = SessionInfo.from_str("eval.model.react")
    identity = TurnIdentity(agent_id="react", session=session, turn_id="turn-1")
    state = ReActTurnState(
        identity=identity,
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    state.custom[TurnCustomKey.TRACE_ID] = "trace-model"
    state.custom[TurnCustomKey.ROOT_SPAN_ID] = "root-model"
    services = service_builder(tmp_path / "traces", model=model)
    context = AgentContext(
        system_prompt="eval",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=session,
        max_iterations=1,
        runtime=AgentRuntime(services=services, state=state),
        identity=identity,
        workspace=tmp_path,
    )
    assert services.hooks is not None
    chat_hook = next(
        spec.hook
        for spec in services.hooks.hook_specs
        if isinstance(spec.hook, ChatSpanHook)
    )
    request = [ChatMessage(role=MessageRole.USER, content="hello")]

    # When
    await chat_hook.before_llm(context, request)
    await chat_hook.after_llm_response(
        context,
        LLMResponse(content="done", finish_reason=FinishReason.STOP),
    )

    # Then
    assert services.trace_store is not None
    spans = await services.trace_store.list_by_session(session.session_id)
    assert len(spans) == 1
    assert spans[0].attributes.get(GenAiAttr.RESPONSE_MODEL) == model
    assert (GenAiAttr.RESPONSE_MODEL in spans[0].attributes) is (model is not None)


def test_static_system_prompt_is_path_and_time_independent() -> None:
    base = "Base evaluation instructions."

    first = static_system_prompt(base)
    second = static_system_prompt(base)

    assert base in first
    assert first == second
    assert re.search(r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2})?", first) is None
    assert re.search(r"(?:[A-Za-z]:[\\/]|/(?:[^/\s]+/)+)", first) is None


def test_eval_observability_splits_otel_endpoint_from_ingestion_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_FORMAT", "otel_http")
    monkeypatch.setenv("LANGFUSE_HOST", "https://lf.example.invalid")
    monkeypatch.setenv("LANGFUSE_BASIC_AUTH", "dGVzdA==")
    monkeypatch.delenv("OTEL_TRACES_ENDPOINT", raising=False)

    config, injector = _eval_observability()

    assert config.otel_endpoint == "http://localhost:4318/v1/traces"
    assert config.eval_ingestion_url == "https://lf.example.invalid/api/public/ingestion"
    assert injector is not None
    assert injector._ingestion_url == "https://lf.example.invalid/api/public/ingestion"


def test_eval_observability_otel_endpoint_env_overrides_collector_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_FORMAT", "otel_http")
    monkeypatch.setenv("LANGFUSE_HOST", "http://localhost:3000")
    monkeypatch.setenv("LANGFUSE_BASIC_AUTH", "dGVzdA==")
    monkeypatch.setenv("OTEL_TRACES_ENDPOINT", "http://collector.example.invalid:4318/v1/traces")

    config, injector = _eval_observability()

    assert config.otel_endpoint == "http://collector.example.invalid:4318/v1/traces"
    assert injector is not None
    assert injector._ingestion_url == "http://localhost:3000/api/public/ingestion"


def test_build_score_injector_derives_url_when_eval_ingestion_url_none() -> None:
    injector = _build_score_injector(
        "https://lf.example.invalid", "dGVzdA==", TraceBackend.OTEL_HTTP,
    )

    assert injector is not None
    assert injector._ingestion_url == "https://lf.example.invalid/api/public/ingestion"


def test_build_score_injector_prefers_explicit_eval_ingestion_url() -> None:
    injector = _build_score_injector(
        "https://lf.example.invalid",
        "dGVzdA==",
        TraceBackend.OTEL_HTTP,
        eval_ingestion_url="https://direct.example.invalid/api/public/ingestion",
    )

    assert injector is not None
    assert injector._ingestion_url == "https://direct.example.invalid/api/public/ingestion"


def test_trace_only_services_threads_bot_model_price_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    captured: dict[str, Path | None] = {}

    def fake_build_trace_hooks(**kwargs: Any) -> list[Any]:
        captured["pricebook_yml_path"] = kwargs["pricebook_yml_path"]
        return []

    monkeypatch.setattr(agent_harness, "build_trace_hooks", fake_build_trace_hooks)
    monkeypatch.setattr(agent_harness, "build_trace_stores", MagicMock(return_value=None))

    # When
    build_trace_only_services(tmp_path / "traces")

    # Then
    expected = Path(agent_harness.__file__).resolve().parents[2] / "config" / "model_prices.yml"
    assert captured["pricebook_yml_path"] == expected
