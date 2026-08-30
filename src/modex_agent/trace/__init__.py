"""framework.trace — Unified OTel span trace system for all agents."""

from modex_agent.ioc.configs.observability import PromptCaptureMode, TraceSpanMode
from modex_agent.trace.agent_start_hook import AgentStartSpanHook
from modex_agent.trace.approval_span_hook import ApprovalSpanHook
from modex_agent.trace.base_hook import BaseTraceHook
from modex_agent.trace.cassette import (
    CassetteCategory,
    CassetteEntry,
    CassetteManifest,
    CassetteRecorder,
    CassetteReplayEngine,
    apply_cassette_wrapping,
)
from modex_agent.trace.chat_span_hook import ChatSpanHook
from modex_agent.trace.experiment_attrs import (
    ExperimentAttribute,
    ExperimentLinkage,
    ExperimentLinkageError,
    attach_experiment_attrs,
    stable_experiment_id,
)
from modex_agent.trace.factory import build_trace_hooks
from modex_agent.trace.handoff_span_hook import HandoffSpanHook
from modex_agent.trace.iteration_span_hook import IterationSpanHook
from modex_agent.trace.langfuse_query import (
    LangfuseClient,
    LangfuseTraceQuery,
    ObservationData,
    SessionSummary,
)
from modex_agent.trace.memory_trace_hook import MemoryTelemetryCounters, MemoryTraceHook
from modex_agent.trace.otel_store import (
    OtelSpanTraceStore,
    build_trace_stores,
)
from modex_agent.trace.pricing import (
    PerModelUsage,
    PriceBook,
    PriceEntry,
    TurnCost,
    UsageBucket,
    UsageBuckets,
    compute_turn_cost,
    load_pricebook,
)
from modex_agent.trace.prompt_capture import (
    FullPromptCapture,
    HashPromptCapture,
    OffPromptCapture,
    PromptCaptureStrategy,
    SummaryPromptCapture,
    build_prompt_capture,
)
from modex_agent.trace.root_span_hook import RootSpanHook
from modex_agent.trace.score_injector import L2ScoreInjector
from modex_agent.trace.semconv import (
    GenAiAttr,
    LangfuseObservationLevel,
    LangfuseObservationType,
    SpanKind,
    SpanName,
    SpanStatusCode,
)
from modex_agent.trace.session_state import MetricCounters, TraceSessionState
from modex_agent.trace.store import JsonlSpanQuery, SpanModel, SpanStatus, TraceQuery
from modex_agent.trace.tool_span_hook import ToolSpanHook

__all__ = [
    "AgentStartSpanHook",
    "ApprovalSpanHook",
    "BaseTraceHook",
    "CassetteCategory",
    "CassetteEntry",
    "CassetteManifest",
    "CassetteRecorder",
    "CassetteReplayEngine",
    "ChatSpanHook",
    "ExperimentAttribute",
    "ExperimentLinkage",
    "ExperimentLinkageError",
    "FullPromptCapture",
    "GenAiAttr",
    "HandoffSpanHook",
    "HashPromptCapture",
    "IterationSpanHook",
    "JsonlSpanQuery",
    "L2ScoreInjector",
    "LangfuseClient",
    "LangfuseObservationLevel",
    "LangfuseObservationType",
    "LangfuseTraceQuery",
    "MetricCounters",
    "MemoryTelemetryCounters",
    "MemoryTraceHook",
    "ObservationData",
    "OffPromptCapture",
    "OtelSpanTraceStore",
    "PerModelUsage",
    "PriceBook",
    "PriceEntry",
    "PromptCaptureMode",
    "PromptCaptureStrategy",
    "RootSpanHook",
    "SessionSummary",
    "SpanKind",
    "SpanModel",
    "SpanName",
    "SpanStatus",
    "SpanStatusCode",
    "SummaryPromptCapture",
    "TraceQuery",
    "TraceSessionState",
    "TraceSpanMode",
    "ToolSpanHook",
    "TurnCost",
    "UsageBucket",
    "UsageBuckets",
    "apply_cassette_wrapping",
    "attach_experiment_attrs",
    "build_prompt_capture",
    "build_trace_hooks",
    "build_trace_stores",
    "compute_turn_cost",
    "load_pricebook",
    "stable_experiment_id",
]
