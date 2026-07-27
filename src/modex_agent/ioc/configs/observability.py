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
            'For Langfuse: {"Authorization": "Basic <base64(pk:sk)>"} or '
            '{"x-langfuse-public-key": "pk-...", "x-langfuse-secret-key": "sk-..."}.'
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
    prompt_capture: str = Field(
        default="summary",
        description="Prompt capture strategy for trace spans. Currently: 'summary'.",
    )
