"""Deterministic eval-agent assembly over the framework's production seams."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final, assert_never
from urllib.parse import urlparse

from bot.eval.memory_harness import (
    DreamRunSummary,
    MemoryRuntimeServices,
    build_memory_runtime_services,
    run_dream_until_exhausted,
)
from bot.eval.task_spec import EvalToolset
from bot.service.pool.declaration import boot_scope_spec
from bot.workspace.handle import WorkspaceHandle, WorkspaceHandleRootProvider
from modex_agent.core.capabilities import ModelCapabilities
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.message import ChatMessage, ContentFormat, ContentPart, ImageUrlPart, TextPart
from modex_agent.core.provider import CallbackStreamProvider, LLMProvider
from modex_agent.core.tool_manager import (
    Tool,
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
from modex_agent.plugins.assembly.single_agent import (
    SingleAgentAssembled,
    SingleAgentInfra,
    assemble_declared_single_agent,
)
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.runtime.models import JsonValue
from modex_agent.runtime.services import AgentRuntimeServices
from modex_agent.runtime.store import InMemoryTurnStateStore
from modex_agent.scope.loader import load_scope_declaration
from modex_agent.scope.overlay import (
    AgentOverlay,
    PoolOverlay,
    ScopeOverlay,
    apply_scope_overlay,
)
from modex_agent.tools.presets import ToolPreset
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
_MODEL_PRICES_PATH: Final = Path(__file__).resolve().parents[2] / "config" / "model_prices.yml"
_BOT_PROJECT: Final = Path(__file__).resolve().parents[2]
_REACT_HARNESS_DECLARATION: Final = (
    _BOT_PROJECT / "config" / "scopes" / "eval" / "agents" / "react-harness.yml"
)


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


class _ModelPinningProvider(CallbackStreamProvider):
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
        if isinstance(self._inner, CallbackStreamProvider):
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


class _WorkspaceTokenNormalizer(Tool):
    """Replace absolute eval workspace paths in text tool results with a token."""

    def __init__(self, inner: Tool, workspace: Path) -> None:
        super().__init__(
            name=inner.name,
            description=inner.description,
            parameters=inner.parameters,
            config=inner.config,
        )
        self._inner = inner
        self._workspace = str(workspace.resolve())

    async def execute(self, **kwargs: Any) -> Any:
        result = await self._inner.execute(**kwargs)
        if isinstance(result, str):
            return result.replace(self._workspace, _WORKSPACE_TOKEN)
        if type(result) is not ToolResult:
            return result
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

    def get_dynamic_schema(self) -> dict[str, Any]:
        return self._inner.get_dynamic_schema()

    def is_available(self, caps: ModelCapabilities | None) -> bool:
        return self._inner.is_available(caps)

    def result_metadata(
        self,
        result: Any,
    ) -> tuple[ContentFormat | None, list[str] | None]:
        return self._inner.result_metadata(result)


async def assemble_harness_agent(
    *,
    workspace: Path,
    data_dir: Path,
    provider: LLMProvider,
    toolset: EvalToolset,
    deny_tools: list[str],
    runtime_services: AgentRuntimeServices,
    governance_enabled: bool,
) -> SingleAgentAssembled:
    component_registry = ComponentRegistry()
    with PluginRegistrationContext(component_registry) as registration:
        DefaultPlugin().register(registration)
    declaration = load_scope_declaration(_REACT_HARNESS_DECLARATION)
    overlay = ScopeOverlay(
        pools={
            "react-harness": PoolOverlay(
                agents={
                    "react": AgentOverlay(
                        toolset=ToolPreset(toolset.value),
                        tools=(
                            [f"-{name}" for name in deny_tools]
                            if deny_tools
                            else None
                        ),
                    )
                }
            )
        }
    )
    scope_boot = boot_scope_spec(
        apply_scope_overlay(declaration, overlay),
        project_dir=_BOT_PROJECT,
        data_dir=data_dir,
        graphs_dirs=(),
        default_llm_provider="default",
        registry=component_registry,
    )
    hooks = (
        tuple(spec.hook for spec in runtime_services.hooks.hook_specs)
        if runtime_services.hooks is not None
        else ()
    )
    assembled = await assemble_declared_single_agent(
        scope_boot.compilation.agents[0],
        SingleAgentInfra(
            llm_provider=provider,
            safety=RuntimeSafetyPolicy(),
            root_provider=WorkspaceHandleRootProvider(
                WorkspaceHandle(target=workspace, data_root=data_dir)
            ),
            tool_wrapper=lambda tool: _WorkspaceTokenNormalizer(tool, workspace),
            extra_hooks=hooks,
            governance_enabled=governance_enabled,
        ),
        project_dir=_BOT_PROJECT,
        data_dir=data_dir,
        component_registry=component_registry,
    )
    pipeline = assembled.instance.pipeline
    if pipeline is not None and pipeline._turn_context_builder is not None:
        runtime_services.governance = pipeline._turn_context_builder.governance
    return assembled


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
    *,
    model: str | None = None,
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
        model=model,
        provider_name="eval",
        request_params=None,
        score_injector=score_injector,
        store=trace_store,
        pricebook_yml_path=_MODEL_PRICES_PATH,
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
        governance=None,
        turn_store=InMemoryTurnStateStore(),
        trace_store=trace_store,
    )


def build_trace_only_services(
    trace_dir: Path,
    *,
    model: str | None = None,
) -> AgentRuntimeServices:
    """Build trace-only services for clean-mode eval — no governance/loop/checkpoint.

    Observability (OTLP export + score injection) is driven by env vars,
    same as ``build_runtime_services``. Trace hooks are registered; runtime
    governance/loop/checkpoint/turn_store are not.
    """
    config, score_injector = _eval_observability()
    trace_store = build_trace_stores(config, trace_dir)
    hook_specs = build_trace_hooks(
        config=config,
        model=model,
        provider_name="eval",
        request_params=None,
        score_injector=score_injector,
        store=trace_store,
        pricebook_yml_path=_MODEL_PRICES_PATH,
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
    return base


__all__ = [
    "_WorkspaceTokenNormalizer",
    "DreamRunSummary",
    "MemoryRuntimeServices",
    "build_memory_runtime_services",
    "build_runtime_services",
    "assemble_harness_agent",
    "build_trace_only_services",
    "run_dream_until_exhausted",
    "static_system_prompt",
    "wrap_provider",
]
