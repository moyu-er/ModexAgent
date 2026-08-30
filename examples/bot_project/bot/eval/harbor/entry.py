"""Self-contained Harbor entry using the canonical ``OTEL_TRACES_ENDPOINT``."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import anyio
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from bot.service.pool.declaration import boot_scope_spec
from bot.workspace.handle import WorkspaceHandle, WorkspaceHandleRootProvider
from modex_agent.agents.react.agent import ReActEvent
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.constants import ReasoningEffort
from modex_agent.core.context import ContextState
from modex_agent.core.emitter import AgentResult, ContentEmitter
from modex_agent.core.history import ListMessageHistory
from modex_agent.core.llm_request import LLMRequest
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import LLMProvider
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.stream_events import LLMStreamEvent
from modex_agent.core.types import LLMResponse, MessageRole
from modex_agent.ioc.configs.llm import LLMConfig
from modex_agent.ioc.configs.observability import (
    ObservabilityConfig,
    PromptCaptureMode,
    TraceBackend,
    TraceSpanMode,
)
from modex_agent.ioc.factories.llm import create_llm_provider
from modex_agent.plugins.assembly.single_agent import (
    SingleAgentAssembled,
    SingleAgentInfra,
    assemble_declared_single_agent,
)
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import JsonValue, TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.scope.loader import load_scope_declaration
from modex_agent.scope.overlay import AgentOverlay, PoolOverlay, ScopeOverlay, apply_scope_overlay
from modex_agent.tools.presets import ToolPreset
from modex_agent.trace.experiment_attrs import ExperimentLinkage, attach_experiment_attrs
from modex_agent.trace.factory import build_trace_hooks
from modex_agent.trace.otel_store import OtelSpanTraceStore
from modex_agent.trace.semconv import GenAiAttr, SpanName
from modex_agent.trace.store import SpanModel

logger = logging.getLogger(__name__)
DEFAULT_INPUT: Final = Path("/root")
DEFAULT_OUTPUT: Final = Path("/logs/agent")
DEFAULT_PROMPT: Final = "Complete the task using the available tools."
_BOT_PROJECT: Final = Path(__file__).resolve().parents[3]
_REACT_HARNESS_DECLARATION: Final = (
    _BOT_PROJECT / "config" / "scopes" / "eval" / "agents" / "react-harness.yml"
)


class HarborToolset(StrEnum):
    NONE = "none"
    READ_ONLY = "read_only"
    READ_WRITE = "read_write"
    FULL = "full"


def _resolve_task_workspace(environment: Mapping[str, str]) -> Path:
    """Container workspace root: ``MODEX_TASK_WORKSPACE`` override, else cwd.

    The entry process starts in the container image's declared WORKDIR (python
    images → /app, others → whatever the image says), so the image is the
    source of truth for where bare task filenames resolve — nothing is
    hardcoded here.
    """
    override = environment.get("MODEX_TASK_WORKSPACE")
    return Path(override) if override else Path.cwd()


def _parse_max_context_tokens(raw: str) -> int:
    try:
        return int(raw)
    except ValueError as error:
        raise ValueError(f"MODEX_MAX_CONTEXT_TOKENS={raw!r} is not an integer") from error


def _parse_max_output_tokens(raw: str) -> int:
    try:
        return int(raw)
    except ValueError as error:
        raise ValueError(f"MODEX_MAX_OUTPUT_TOKENS={raw!r} is not an integer") from error


class EntryConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    model: str = Field(min_length=1)
    api_key: str | None = None
    base_url: str | None = None
    otel_traces_endpoint: str | None = None
    langfuse_basic_auth: str | None = None
    experiment: ExperimentLinkage
    memory_namespace: str = Field(min_length=1)
    toolset: HarborToolset = HarborToolset.FULL
    denied_tools: tuple[str, ...] = ()
    task_input_dir: Path = DEFAULT_INPUT
    task_name: str | None = None
    task_workspace: Path
    instruction_path: Path
    output_dir: Path = DEFAULT_OUTPUT
    max_iterations: int = Field(default=25, gt=0)
    system_prompt: str = Field(default=DEFAULT_PROMPT, min_length=1)
    temperature: float = Field(default=0.7, ge=0, le=2)
    reasoning_effort: ReasoningEffort = ReasoningEffort.NONE
    max_context_tokens: int = 200000
    max_output_tokens: int = 50000

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> EntryConfig:
        input_dir = Path(environment.get("MODEX_TASK_INPUT_DIR") or DEFAULT_INPUT)
        denied = tuple(name.strip() for name in environment.get("MODEX_DENY_TOOLS", "").split(",") if name.strip())
        raw_max_context_tokens = environment.get("MODEX_MAX_CONTEXT_TOKENS")
        raw_max_output_tokens = environment.get("MODEX_MAX_OUTPUT_TOKENS")
        return cls(
            model=environment.get("LLM_MODEL", ""), api_key=environment.get("LLM_API_KEY") or None,
            base_url=environment.get("LLM_BASE_URL") or None,
            otel_traces_endpoint=environment.get("OTEL_TRACES_ENDPOINT") or None,
            langfuse_basic_auth=environment.get("LANGFUSE_BASIC_AUTH") or None,
            experiment=ExperimentLinkage(
                experiment_id=environment.get("MODEX_EXPERIMENT_ID", ""),
                experiment_name=environment.get("MODEX_EXPERIMENT_NAME", ""),
                dataset_id=environment.get("MODEX_EXPERIMENT_DATASET_ID", ""),
                item_id=environment.get("MODEX_EXPERIMENT_ITEM_ID", ""),
            ),
            memory_namespace=environment.get("MODEX_MEMORY_NS", ""),
            toolset=HarborToolset(environment.get("MODEX_TOOLSET", HarborToolset.FULL.value)), denied_tools=denied,
            task_input_dir=input_dir,
            task_name=environment.get("MODEX_TASK_NAME") or None,
            task_workspace=_resolve_task_workspace(environment),
            instruction_path=Path(environment.get("MODEX_TASK_INSTRUCTION_PATH") or input_dir / "instruction.txt"),
            output_dir=Path(environment.get("MODEX_AGENT_OUTPUT_DIR") or DEFAULT_OUTPUT),
            max_iterations=int(environment.get("MODEX_MAX_ITERATIONS", "25")),
            system_prompt=environment.get("MODEX_SYSTEM_PROMPT") or DEFAULT_PROMPT,
            temperature=float(environment.get("MODEX_TEMPERATURE", "0.7")),
            reasoning_effort=TypeAdapter(ReasoningEffort).validate_python(environment.get("MODEX_REASONING_EFFORT", ReasoningEffort.NONE.value)),
            max_context_tokens=(
                _parse_max_context_tokens(raw_max_context_tokens)
                if raw_max_context_tokens
                else 200000
            ),
            max_output_tokens=(
                _parse_max_output_tokens(raw_max_output_tokens)
                if raw_max_output_tokens
                else 50000
            ),
        )


class UsageArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    dropped_span_count: int = 0


class TaskResultArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    trace_id: str
    output: str = ""
    stop_reason: str | None = None
    error: str | None = None
    dropped_span_count: int = 0
    memory_namespace: str


type SpanExporter = Callable[[SpanModel], Awaitable[None]]
type TurnExecutor = Callable[..., Awaitable[AgentResult]]


class EntryDependencies:
    def __init__(self, provider: LLMProvider, span_exporter: SpanExporter | None = None, turn_executor: TurnExecutor | None = None) -> None:
        self.provider = provider
        self.span_exporter = span_exporter
        self.turn_executor = turn_executor


class _Emitter(ContentEmitter[ReActEvent]):
    async def emit_delta(self, delta: str) -> None:
        _ = delta

    async def emit_complete(self, result: AgentResult) -> None:
        _ = result

    async def emit_error(self, error: str) -> None:
        _ = error


class _UsageProvider(LLMProvider):
    """Pass-through provider wrapper.

    Usage key normalization previously lived here; it now happens in
    ``TokenUsage``'s validator at ``LLMResponse`` construction. The wrapper
    survives as the BARE-mode provider seam: stream() delegates verbatim,
    chat() keeps the direct delegation face.
    """

    def __init__(self, delegate: LLMProvider) -> None:
        super().__init__()
        self._delegate = delegate

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        async for event in self._delegate.stream(request):
            yield event

    async def chat(self, messages: list[ChatMessage], model: str | None = None, temperature: float = 0.7,
                   max_output_tokens: int | None = None, tools: list[dict[str, Any]] | None = None,
                   **kwargs: JsonValue) -> LLMResponse:
        return await self._delegate.chat(messages, model, temperature, max_output_tokens, tools, **kwargs)

    def get_default_model(self) -> str:
        return self._delegate.get_default_model()


class _TraceStore(OtelSpanTraceStore):
    def __init__(self, config: EntryConfig, exporter: SpanExporter | None) -> None:
        super().__init__(config.output_dir / "trace", backend=TraceBackend.OTEL_HTTP,
                         otlp_endpoint=config.otel_traces_endpoint if exporter is None else None,
                         otlp_headers={"Authorization": f"Basic {config.langfuse_basic_auth}"}
                         if config.langfuse_basic_auth
                         else None,
                         otlp_service_name="modex_harbor_agent")
        self._linkage, self._model, self._exporter = config.experiment, config.model, exporter
        self._task_name = config.task_name
        self._endpoint_configured = config.otel_traces_endpoint is not None
        self._missing_endpoint_drops = 0
        self._usage = UsageArtifact(model=config.model)
        if not self._endpoint_configured:
            logger.warning("OTEL_TRACES_ENDPOINT is missing; tracing is degraded and spans will be dropped")

    @property
    def dropped_span_count(self) -> int:
        return self._missing_endpoint_drops + self.dropped_spans

    @property
    def usage(self) -> UsageArtifact:
        return self._usage.model_copy(update={"dropped_span_count": self.dropped_span_count})

    async def save_span(self, span: SpanModel) -> None:
        mapped = self._map_span(span)
        if self._exporter is not None:
            await self._exporter(mapped)
        elif self._endpoint_configured:
            await super().save_span(mapped)
        else:
            self._missing_endpoint_drops += 1

    def _map_span(self, span: SpanModel) -> SpanModel:
        if span.name == SpanName.INVOKE_AGENT:
            mapped = attach_experiment_attrs(span, self._linkage)
            if self._task_name is not None:
                # Langfuse names a trace after its root observation's span
                # name, so the task name rides the span name for trace-list
                # visibility.
                mapped = mapped.model_copy(
                    update={"name": f"{SpanName.INVOKE_AGENT.value}/{self._task_name}"}
                )
            return mapped
        if span.name != SpanName.CHAT:
            return span
        attrs = dict(span.attributes)
        keys = (GenAiAttr.USAGE_INPUT_TOKENS, GenAiAttr.USAGE_OUTPUT_TOKENS,
                GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS, GenAiAttr.USAGE_CACHE_CREATION_INPUT_TOKENS)
        attrs.setdefault(GenAiAttr.REQUEST_MODEL, self._model)
        for key in keys:
            attrs.setdefault(key, 0)
        self._usage = UsageArtifact(model=self._model, input_tokens=self._usage.input_tokens + int(attrs[keys[0]]),
                                    output_tokens=self._usage.output_tokens + int(attrs[keys[1]]),
                                    cache_read_tokens=self._usage.cache_read_tokens + int(attrs[keys[2]]),
                                    cache_write_tokens=self._usage.cache_write_tokens + int(attrs[keys[3]]))
        return span.model_copy(update={"attributes": attrs})


def _session_id(config: EntryConfig) -> str:
    """``harbor_<task>_<item>`` when the task is named; ``<item>.harbor`` otherwise.

    The prefix stays a single ``.``-free segment (``agent_of`` reads the agent
    name as the second dot-separated component; a multi-dot prefix would make
    ``agent_of`` and ``SessionInfo.from_str`` disagree).
    """
    if config.task_name:
        return f"harbor_{config.task_name}_{config.experiment.item_id}"
    return f"{config.experiment.item_id}.harbor"


def _write_artifacts(config: EntryConfig, instruction: str, result: TaskResultArtifact, usage: UsageArtifact) -> None:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    (config.output_dir / "instruction-rendered.txt").write_text(instruction, encoding="utf-8")
    (config.output_dir / "result.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    (config.output_dir / "usage.json").write_text(usage.model_dump_json(indent=2), encoding="utf-8")
    (config.output_dir / "trajectory.jsonl").write_text(result.model_dump_json() + "\n", encoding="utf-8")
    summary = f"# Result\n\n{result.output}" if result.error is None else f"# Error\n\n{result.error}"
    (config.output_dir / "summary.md").write_text(summary + "\n", encoding="utf-8")


async def execute_entry(config: EntryConfig, dependencies: EntryDependencies) -> TaskResultArtifact:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    trace_id = uuid.uuid4().hex
    trace_record = {"trace_id": trace_id, "turn": 1, "experiment_id": config.experiment.experiment_id,
                    "item_id": config.experiment.item_id, "task_name": config.task_name}
    import json
    (config.output_dir / "trace-ids.jsonl").write_text(json.dumps(trace_record) + "\n", encoding="utf-8")
    store = _TraceStore(config, dependencies.span_exporter)
    hook_specs = build_trace_hooks(ObservabilityConfig(trace_backend=TraceBackend.OTEL_HTTP,
                                    trace_spans=TraceSpanMode.STANDARD, prompt_capture=PromptCaptureMode.FULL),
                                    model=config.model, provider_name=config.model.partition("/")[0],
                                    request_params=None, score_injector=None, store=store)
    instruction, agent_result, failure = "", None, None
    assembled: SingleAgentAssembled | None = None
    try:
        instruction = config.instruction_path.read_text(encoding="utf-8")
        declaration = load_scope_declaration(_REACT_HARNESS_DECLARATION)
        overlay = ScopeOverlay(
            pools={
                "react-harness": PoolOverlay(
                    agents={
                        "react": AgentOverlay(
                            toolset=ToolPreset(config.toolset.value),
                            tools=[f"-{name}" for name in config.denied_tools] or None,
                        )
                    }
                )
            }
        )
        scope_boot = boot_scope_spec(
            apply_scope_overlay(declaration, overlay),
            project_dir=_BOT_PROJECT,
            data_dir=config.output_dir / "assembly",
            graphs_dirs=(),
            default_llm_provider="default",
        )
        component_registry = ComponentRegistry()
        with PluginRegistrationContext(component_registry) as registration:
            DefaultPlugin().register(registration)
        assembled = await assemble_declared_single_agent(
            scope_boot.compilation.agents[0],
            SingleAgentInfra(
                llm_provider=_UsageProvider(dependencies.provider),
                safety=RuntimeSafetyPolicy(),
                root_provider=WorkspaceHandleRootProvider(
                    WorkspaceHandle(
                        target=config.task_workspace,
                        data_root=config.output_dir / "assembly",
                    )
                ),
                extra_hooks=tuple(spec.hook for spec in hook_specs),
                governance_enabled=False,
            ),
            project_dir=_BOT_PROJECT,
            data_dir=config.output_dir / "assembly",
            component_registry=component_registry,
        )
        session = SessionInfo(session_id=_session_id(config), agent_name="harbor")
        identity = TurnIdentity(
            agent_id="harbor",
            session=session,
            turn_id=uuid.uuid4().hex,
        )
        state = ReActTurnState(
            identity=identity,
            agent_kind=AgentKind.REACT,
            phase=TurnPhase.CREATED,
            custom={TurnCustomKey.TRACE_ID: trace_id},
        )
        pipeline = assembled.instance.pipeline
        assert pipeline is not None
        builder = pipeline._turn_context_builder
        assert builder is not None
        context, _ = builder.build_runtime_and_context(
            session,
            ContextState(
                system_prompt=config.system_prompt,
                history=ListMessageHistory(
                    [ChatMessage(role=MessageRole.USER, content=instruction)]
                ),
            ),
            assembled.context_manager,
            workspace=config.task_workspace,
        )
        context.max_iterations = config.max_iterations
        context.runtime = AgentRuntime(
            services=AgentRuntimeServices(
                hooks=pipeline.hook_runner,
                trace_store=store,
            ),
            state=state,
        )
        context.identity = identity
        context.current_input = instruction
        executor = dependencies.turn_executor
        agent_result = await (executor(context, _Emitter()) if executor is not None
                              else assembled.instance.pipeline.agent.run(context, _Emitter()))
    except Exception as exc:
        logger.exception("Harbor agent turn failed", extra={"trace_id": trace_id})
        failure = str(exc)
    finally:
        if assembled is not None:
            await assembled.instance.stop()
            await assembled.memory_system.close()
        store.close()
    if agent_result is not None and agent_result.error is not None:
        failure = agent_result.error
    outcome = TaskResultArtifact(trace_id=trace_id, output=agent_result.content or "" if agent_result else "",
                                 stop_reason=str(agent_result.stop_reason) if agent_result else None,
                                 error=failure, dropped_span_count=store.dropped_span_count,
                                 memory_namespace=config.memory_namespace)
    _write_artifacts(config, instruction, outcome, store.usage)
    return outcome


def _bare_provider_factory(
    model: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    temperature: float = 0.7,
    reasoning_effort: ReasoningEffort = ReasoningEffort.NONE,
) -> LLMProvider:
    """Build the BARE-mode provider through the framework factory.

    ``api_key``/``base_url`` keep ``None`` defaults to preserve
    ``mode_runner``'s call shape; ``LLMConfig`` spells "unset" as the
    empty string.
    """
    return create_llm_provider(
        LLMConfig(
            model=model,
            api_key=api_key or "",
            base_url=base_url or "",
            temperature=temperature,
            reasoning_effort=reasoning_effort,
        )
    )


async def _run_from_environment() -> None:
    import os

    from bot.eval.harbor.mode_runner import run_from_environment

    await run_from_environment(os.environ, execute_entry, _bare_provider_factory)


def main() -> None:
    anyio.run(_run_from_environment)


if __name__ == "__main__":
    main()
