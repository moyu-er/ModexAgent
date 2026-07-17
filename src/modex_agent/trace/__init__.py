"""framework.trace — Unified OTel span trace system for all agents."""

from modex_agent.trace.cassette import (
    CassetteCategory,
    CassetteEntry,
    CassetteManifest,
    CassetteRecorder,
    CassetteReplayEngine,
    apply_cassette_wrapping,
)
from modex_agent.trace.hooks import TraceCollectorHook
from modex_agent.trace.otel_store import (
    OtelSpanTraceStore,
    build_trace_stores,
)
from modex_agent.trace.store import JsonlSpanQuery, SpanModel, SpanStatus, TraceQuery

__all__ = [
    "CassetteCategory",
    "CassetteEntry",
    "CassetteManifest",
    "CassetteRecorder",
    "CassetteReplayEngine",
    "JsonlSpanQuery",
    "OtelSpanTraceStore",
    "SpanModel",
    "SpanStatus",
    "TraceCollectorHook",
    "TraceQuery",
    "apply_cassette_wrapping",
    "build_trace_stores",
]
