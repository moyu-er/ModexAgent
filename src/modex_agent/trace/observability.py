"""Observability configuration."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class TraceBackend(StrEnum):
    """Trace export backend."""

    OFF = "off"
    FILE = "file"
    OTEL_HTTP = "otel_http"


class CassetteScope(StrEnum):
    """Cassette recording scope."""

    DEFAULT = "default"
    FULL = "full"


class PromptCaptureMode(StrEnum):
    """Prompt capture strategy for trace span attributes."""

    OFF = "off"
    HASH = "hash"
    SUMMARY = "summary"
    FULL = "full"


class TraceSpanMode(StrEnum):
    """Trace span verbosity level."""

    MINIMAL = "minimal"
    STANDARD = "standard"
    FULL = "full"


class ObservabilityConfig(BaseModel):
    """Observability configuration. None = no logging/tracing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Existing (retained)
    run_logging: bool = True
    level: str = "INFO"
    # Trace Path (A)
    trace_backend: TraceBackend = Field(
        default=TraceBackend.FILE, description="Trace export backend."
    )
    otel_endpoint: str | None = Field(
        default=None, description="OTLP HTTP endpoint for remote trace export."
    )
    otel_service_name: str = Field(default="modex_agent", description="OpenTelemetry service name.")
    otel_headers: dict[str, str] | None = Field(
        default=None,
        description=(
            "Optional HTTP headers for OTLP export (e.g. Langfuse auth). "
            'For Langfuse: {"Authorization": "Basic <base64(pk:sk)>", '
            '"x-langfuse-ingestion-version": "4"}.'
        ),
    )
    retain_reasoning_content: bool = Field(
        default=True, description="Retain reasoning_content in trace records."
    )
    # Repro Path (B1/B2)
    checkpoint_per_iteration: bool = Field(
        default=True,
        description="Persist a turn checkpoint after each ReAct iteration.",
    )
    cassette_enabled: bool = Field(
        default=False, description="Enable cassette recording for reproducibility."
    )
    cassette_scope: CassetteScope = Field(
        default=CassetteScope.DEFAULT, description="Cassette recording scope."
    )
    # Training Data Derivation
    training_relevant: bool = Field(
        default=False,
        description="Mark this session as relevant for training data derivation.",
    )
    training_max_iterations: int = Field(
        default=20,
        description="Maximum ReAct iterations for training-relevant sessions.",
    )
    training_max_tokens: int = Field(
        default=100000,
        description="Maximum token budget for training-relevant sessions.",
    )
    prompt_capture: PromptCaptureMode = Field(
        default=PromptCaptureMode.SUMMARY,
        description="Prompt capture strategy for trace span attributes.",
    )
    trace_spans: TraceSpanMode = Field(
        default=TraceSpanMode.STANDARD,
        description="Trace span verbosity level.",
    )
    capture_tools: bool = Field(
        default=False,
        description="Capture tool call details (arguments and results) in trace spans.",
    )
    eval_score_injection: bool = Field(
        default=False,
        description=(
            "Inject trajectory metrics (tool_success_rate, tool_call_count, "
            "error_tool_count, iteration_count, llm_call_count, "
            "total_input_tokens, total_output_tokens, total_reasoning_tokens, "
            "api_latency_avg_s, cache_hit_rate, response_token_ratio, "
            "has_reasoning) to Langfuse on each turn finish. "
            "Requires trace_backend=otel_http and a reachable Langfuse instance."
        ),
    )
    eval_ingestion_url: str | None = Field(
        default=None,
        description=(
            "Explicit Langfuse ingestion URL for score injection; "
            "None derives from otel_endpoint."
        ),
    )
    environment: str = Field(
        default="default",
        description=(
            "Langfuse environment for trace segmentation (dev/staging/production). "
            "Mapped to langfuse.environment on every span."
        ),
    )
    version: str | None = Field(
        default=None,
        description=(
            "Application or prompt version for A/B testing and trace grouping. "
            "Mapped to langfuse.version on every span when set."
        ),
    )
    tags: list[str] = Field(
        default_factory=list,
        description=(
            "Custom trace tags for filtering in Langfuse. "
            "Mapped to langfuse.trace.tags on every span when non-empty."
        ),
    )
