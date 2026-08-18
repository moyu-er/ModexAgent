"""Deterministic eval-agent assembly over the framework's production seams."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final, assert_never
from urllib.parse import urlparse

from bot.config.memory_defaults import main_agent_memory
from bot.eval.task_spec import EvalToolset
from modex_agent.core.capabilities import ModelCapabilities
from modex_agent.core.message import ChatMessage, ContentPart, ImageUrlPart, TextPart
from modex_agent.core.provider import LLMProvider, StreamingLLMProvider
from modex_agent.core.tool_manager import (
    InMemoryToolManager,
    Tool,
    ToolConfig,
    ToolExecutionContext,
    ToolManager,
    ToolResult,
)
from modex_agent.core.types import LLMResponse
from modex_agent.hook import HookRunner, HookSpec
from modex_agent.hook.builtin import LoopDetectionHook
from modex_agent.hook.builtin.checkpoint import CheckpointHook
from modex_agent.ioc.configs.observability import (
    ObservabilityConfig,
    PromptCaptureMode,
    TraceBackend,
    TraceSpanMode,
)
from modex_agent.ioc.factories.governance import create_governance
from modex_agent.multi_agent.template import build_preset_tool_manager
from modex_agent.runtime.models import JsonValue
from modex_agent.runtime.services import AgentRuntimeServices
from modex_agent.runtime.store import InMemoryTurnStateStore
from modex_agent.tools.filter import FilteredToolManager
from modex_agent.tools.presets import ToolPreset
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider
from modex_agent.trace.cassette import (
    CassetteFlushHook,
    CassetteRecorder,
    CassetteReplayEngine,
)
from modex_agent.trace.factory import build_trace_hooks
from modex_agent.trace.otel_store import build_trace_stores
from modex_agent.trace.score_injector import L2ScoreInjector

logger = logging.getLogger(__name__)

_WORKSPACE_TOKEN: Final = "<workspace>"
_STATIC_WORKSPACE_SENTENCE: Final = "Use the available tools to operate on the workspace."


def _eval_observability() -> tuple[ObservabilityConfig, L2ScoreInjector | None]:
    """Build observability config and score injector from env vars.

    Mirrors the ``${ENV}`` interpolation in ``bot_config.yml`` so that eval
    harness (a separate process) behaves identically to the bot runtime when
    the same env vars are set.

    - ``OTEL_FORMAT`` → trace_backend (file/otel_http/off; default: otel_http)
    - ``OTEL_TRACES_ENDPOINT`` → OTLP endpoint, typically the collector
      (default: http://localhost:4318/v1/traces)
    - ``LANGFUSE_HOST`` → score injection ingestion URL (direct to Langfuse)
    - ``LANGFUSE_BASIC_AUTH`` → OTLP headers + score injection auth

    With Langfuse creds present, the default collector path includes OTLP export
    and score injection. ``file`` remains the explicit legacy fallback and
    ``off`` disables tracing.
    """
    raw_backend = os.environ.get("OTEL_FORMAT", "otel_http").lower()
    try:
        trace_backend = TraceBackend(raw_backend)
    except ValueError:
        logger.warning("Unknown OTEL_FORMAT=%s, defaulting to file", raw_backend)
        trace_backend = TraceBackend.FILE

    langfuse_host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000")
    basic_auth = os.environ.get("LANGFUSE_BASIC_AUTH", "")

    otel_headers: dict[str, str] | None = None
    if basic_auth:
        otel_headers = {
            "Authorization": f"Basic {basic_auth}",
            "x-langfuse-ingestion-version": "4",
        }

    otel_endpoint = (
        os.environ.get("OTEL_TRACES_ENDPOINT", "http://localhost:4318/v1/traces")
        if trace_backend == TraceBackend.OTEL_HTTP
        else None
    )

    config = ObservabilityConfig(
        trace_backend=trace_backend,
        otel_endpoint=otel_endpoint,
        eval_ingestion_url=f"{langfuse_host}/api/public/ingestion",
        otel_headers=otel_headers,
        prompt_capture=PromptCaptureMode.FULL,
        trace_spans=TraceSpanMode.FULL,
        environment=os.environ.get("LANGFUSE_ENVIRONMENT", "default"),
        version=os.environ.get("LANGFUSE_VERSION"),
        tags=os.environ.get("LANGFUSE_TAGS", "").split(",") if os.environ.get("LANGFUSE_TAGS") else [],
    )

    return config, _build_score_injector(
        langfuse_host,
        basic_auth,
        trace_backend,
        eval_ingestion_url=config.eval_ingestion_url,
    )


def _build_score_injector(
    langfuse_host: str,
    basic_auth: str,
    trace_backend: TraceBackend,
    eval_ingestion_url: str | None = None,
) -> L2ScoreInjector | None:
    """Create L2ScoreInjector when Langfuse is reachable and OTLP is enabled."""
    if trace_backend != TraceBackend.OTEL_HTTP or not langfuse_host or not basic_auth:
        return None
    try:
        parsed = urlparse(langfuse_host)
        ingestion_url = eval_ingestion_url or (
            f"{parsed.scheme}://{parsed.netloc}/api/public/ingestion"
        )
        return L2ScoreInjector(
            ingestion_url=ingestion_url,
            headers={
                "Authorization": f"Basic {basic_auth}",
                "x-langfuse-ingestion-version": "4",
            },
        )
    except Exception:
        logger.warning("L2ScoreInjector creation failed; score injection disabled.", exc_info=True)
        return None


class _FixedWorkspaceRootProvider(WorkspaceRootProvider):
    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace.resolve()

    def current(self) -> Path:
        return self._workspace


class _ModelPinningProvider(StreamingLLMProvider):
    def __init__(self, inner: LLMProvider, model: str) -> None:
        super().__init__()
        self._inner = inner
        self._model = model

    def get_default_model(self) -> str:
        return self._model

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
        pinned_model = self._model if model is None else model
        if isinstance(self._inner, StreamingLLMProvider):
            return await self._inner.chat_stream(
                messages=messages,
                model=pinned_model,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                tools=tools,
                on_content_delta=on_content_delta,
                on_reasoning_delta=on_reasoning_delta,
                **kwargs,
            )
        return await self._inner.chat(
            messages=messages,
            model=pinned_model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            tools=tools,
            **kwargs,
        )


def build_tool_manager(
    workspace: Path,
    toolset: EvalToolset,
    deny_tools: list[str],
) -> ToolManager:
    """Build a workspace-bound framework preset with caller-selected denials."""
    match toolset:
        case EvalToolset.NONE:
            return InMemoryToolManager()
        case EvalToolset.READ_ONLY | EvalToolset.READ_WRITE | EvalToolset.FULL:
            preset = ToolPreset(toolset.value)
        case unreachable:
            assert_never(unreachable)

    root_provider = _FixedWorkspaceRootProvider(workspace)
    base = build_preset_tool_manager(root_provider, preset)

    denied = set(deny_tools)
    allowed = [name for name in base.list_tools() if name not in denied]
    return FilteredToolManager(
        base=base,
        allowed_tools=allowed,
        denied_tools=None,
    )


class _WorkspaceTokenNormalizer(ToolManager):
    """Replace absolute eval workspace paths in text tool results with a token."""

    def __init__(self, inner: ToolManager, workspace: Path) -> None:
        super().__init__()
        self._inner = inner
        self._workspace = str(workspace.resolve())

    def register(self, tool: Tool, config: ToolConfig | None = None) -> None:
        self._inner.register(tool, config)

    def unregister(self, tool_name: str) -> bool:
        return self._inner.unregister(tool_name)

    def get_tool(self, tool_name: str) -> Tool | None:
        return self._inner.get_tool(tool_name)

    def list_tools(self) -> list[str]:
        return self._inner.list_tools()

    def is_registered(self, tool_name: str) -> bool:
        return self._inner.is_registered(tool_name)

    def get_tool_descriptions(
        self,
        caps: ModelCapabilities | None = None,
    ) -> list[dict[str, Any]]:
        return self._inner.get_tool_descriptions(caps)

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        ctx: ToolExecutionContext | None = None,
    ) -> ToolResult:
        result = await self._inner.execute(tool_name, arguments, ctx=ctx)
        normalized: list[ContentPart] = []
        for part in result.content:
            match part:
                case TextPart(text=text):
                    normalized.append(
                        part.model_copy(
                            update={"text": text.replace(self._workspace, _WORKSPACE_TOKEN)}
                        )
                    )
                case ImageUrlPart():
                    normalized.append(part)
                case unreachable:
                    assert_never(unreachable)
        return result.model_copy(update={"content": normalized})


def wrap_provider(
    provider: LLMProvider,
    cassette: CassetteRecorder | CassetteReplayEngine,
) -> LLMProvider:
    """THE cassette wrap point: providers only; tools are never cassette-wrapped."""
    wrapped = cassette.wrap_provider(provider)
    return _ModelPinningProvider(wrapped, provider.get_default_model())


def build_runtime_services(
    trace_dir: Path,
    recorder: CassetteRecorder | None = None,
) -> AgentRuntimeServices:
    """Build trace, governance, loop detection, and checkpoint services for evals.

    Observability (OTLP export + score injection) is driven by env vars
    ``OTEL_FORMAT``, ``LANGFUSE_HOST``, ``LANGFUSE_BASIC_AUTH`` — identical to
    the bot runtime's ``bot_config.yml`` interpolation. The collector path is
    the default; set ``OTEL_FORMAT=file`` for the legacy JSONL fallback.
    """
    config, score_injector = _eval_observability()
    trace_store = build_trace_stores(config, trace_dir)
    hook_specs = build_trace_hooks(
        config=config,
        model=None,
        provider_name="eval",
        request_params=None,
        score_injector=score_injector,
        store=trace_store,
    )
    hook_specs.extend(
        [
            HookSpec(hook=LoopDetectionHook()),
            HookSpec(hook=CheckpointHook()),
        ]
    )
    if recorder is not None:
        hook_specs.append(HookSpec(hook=CassetteFlushHook(recorder)))

    return AgentRuntimeServices(
        hooks=HookRunner(hook_specs),
        governance=create_governance(main_agent_memory()),
        turn_store=InMemoryTurnStateStore(),
        trace_store=trace_store,
    )


def build_trace_only_services(trace_dir: Path) -> AgentRuntimeServices:
    """Build trace-only services for clean-mode eval — no governance/loop/checkpoint.

    Observability (OTLP export + score injection) is driven by env vars,
    same as ``build_runtime_services``. Trace hooks are registered; runtime
    governance/loop/checkpoint/turn_store are not.
    """
    config, score_injector = _eval_observability()
    trace_store = build_trace_stores(config, trace_dir)
    hook_specs = build_trace_hooks(
        config=config,
        model=None,
        provider_name="eval",
        request_params=None,
        score_injector=score_injector,
        store=trace_store,
    )
    return AgentRuntimeServices(
        hooks=HookRunner(hook_specs),
        governance=None,
        turn_store=None,
        trace_store=trace_store,
    )


def static_system_prompt(base: str) -> str:
    """Build cassette-stable instructions without runtime path or time data.

    RuntimeProvider injects hourly timestamps and evals use random temporary
    workspace paths. Both are excluded because they break cassette
    ``llm_call_key`` content addressing across otherwise identical runs.
    """
    return f"{base}\n\n{_STATIC_WORKSPACE_SENTENCE}"


__all__ = [
    "_WorkspaceTokenNormalizer",
    "build_runtime_services",
    "build_tool_manager",
    "build_trace_only_services",
    "static_system_prompt",
    "wrap_provider",
]
