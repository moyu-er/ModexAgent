"""OTel-compatible span trace store — JSONL writer + OTLP export.

:class:`OtelSpanTraceStore` writes :class:`SpanModel` values (OTel-compatible
span JSON) to ``spans.jsonl`` and — when an OTel SDK ``Tracer`` is provided —
also emits each span via the SDK for remote OTLP export.  The JSONL file is
the primary local store for agent self-read; the OTel SDK path is independent
— a failure in one export path does not affect the other.

The ``[observability]`` extra (``opentelemetry-sdk`` +
``opentelemetry-exporter-otlp-proto-http``) is required only when OTLP
export is active.  Without it, the module loads and operates in file-only
mode.

The hook (:class:`~modex_agent.trace.hooks.TraceCollectorHook`) constructs
:class:`SpanModel` values directly and calls :meth:`save_span`; this store no
longer takes :class:`OperationRecord` (removed in T8).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.ioc.configs.observability import ObservabilityConfig, TraceBackend
from modex_agent.trace.semconv import GenAiAttr, SpanKind, SpanStatusCode
from modex_agent.trace.store import SpanModel, TraceQuery

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer as OtelTracer  # type: ignore[import-not-found]

logger = logging.getLogger(__name__)

_SPANS_FILENAME = "spans.jsonl"


# ── OtelSpanTraceStore ────────────────────────────────────────────────


class OtelSpanTraceStore(TraceQuery):
    """Writes OTel-compatible span JSON to ``spans.jsonl`` and (optionally)
    emits via the OTel SDK.

    File layout::

        {base_dir}/{session_id}/spans.jsonl

    Each line is a JSON-encoded :class:`SpanModel`.

    When ``tracer`` is provided (OTel SDK available + ``otel_endpoint``
    configured), :meth:`save_span` also emits each span via the OTel SDK for
    OTLP remote export.  The OTel SDK path is independent of the JSONL
    write — a failure in OTel emission is logged and does not prevent the
    JSONL write.
    """

    def __init__(
        self,
        base_dir: Path,
        *,
        retain_reasoning_content: bool = True,
        tracer: OtelTracer | None = None,
    ) -> None:
        self._base_dir = base_dir
        self._retain_reasoning_content = retain_reasoning_content
        self._tracer: OtelTracer | None = tracer

    @property
    def retain_reasoning_content(self) -> bool:
        return self._retain_reasoning_content

    def _session_path(self, session_id: str) -> Path:
        return self._base_dir / session_id / _SPANS_FILENAME

    async def save_span(self, span: SpanModel) -> None:
        """Persist a single :class:`SpanModel`.

        Writes one JSON line to ``{base_dir}/{session_id}/spans.jsonl`` and,
        when an OTel SDK ``Tracer`` is configured, emits the span via the SDK
        for OTLP export.  OTel SDK failures are logged and do not prevent the
        local JSONL write.

        When ``retain_reasoning_content`` is ``False``, the
        ``gen_ai.output.reasoning_content`` attribute is stripped before
        writing or emitting.
        """
        span_to_write = span
        if not self._retain_reasoning_content:
            stripped = {k: v for k, v in span.attributes.items() if k != GenAiAttr.OUTPUT_REASONING_CONTENT}
            span_to_write = span.model_copy(update={"attributes": stripped})

        session_id = str(span_to_write.attributes.get("gen_ai.session.id", "unknown"))
        path = self._session_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(span_to_write.model_dump(mode="json"), ensure_ascii=False)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

        if self._tracer is not None:
            try:
                _emit_span_via_otel_sdk(self._tracer, span_to_write)
            except Exception:
                logger.warning(
                    "OtelSpanTraceStore: OTel SDK emission failed",
                    exc_info=True,
                )

    async def list_by_session(self, session_id: str) -> list[SpanModel]:
        """Return all spans for *session_id* from ``spans.jsonl``."""
        from modex_agent.utils.file_io import read_jsonl_robust

        path = self._session_path(session_id)
        data = read_jsonl_robust(path)
        out: list[SpanModel] = []
        for d in data:
            try:
                out.append(SpanModel.model_validate(d))
            except Exception:
                logger.warning("Skipping malformed span line: %s", str(d)[:120])
        return out

    async def list_by_trace_id(self, trace_id: str) -> list[SpanModel]:
        """Return all spans for *trace_id* across all session dirs."""
        from modex_agent.utils.file_io import read_jsonl_robust

        if not self._base_dir.exists():
            return []
        out: list[SpanModel] = []
        for session_dir in self._base_dir.iterdir():
            if not session_dir.is_dir():
                continue
            path = session_dir / _SPANS_FILENAME
            data = read_jsonl_robust(path)
            for d in data:
                if d.get("trace_id") == trace_id:
                    try:
                        out.append(SpanModel.model_validate(d))
                    except Exception:
                        logger.warning(
                            "Skipping malformed span line: %s", str(d)[:120]
                        )
        return out


# ── Factory ───────────────────────────────────────────────────────────


def build_trace_stores(
    config: ObservabilityConfig,
    base_dir: Path,
) -> OtelSpanTraceStore | None:
    """Build the appropriate trace store based on ``config``.

    - ``OFF``: returns ``None`` (no trace store, zero overhead).
    - ``FILE`` (default) / ``OTEL_HTTP``: returns an
      :class:`OtelSpanTraceStore`.  When ``otel_endpoint`` is set, the store
      also receives an OTel SDK ``Tracer`` wired to a ``BatchSpanProcessor``
      → ``OTLPSpanExporter`` for remote OTLP export.  Requires the
      ``[observability]`` extra when OTLP export is active.

    Args:
        config: The observability configuration.
        base_dir: Base directory for file-based trace stores
                  (``{base_dir}/{session_id}/``).

    Returns:
        An :class:`OtelSpanTraceStore` or ``None`` when disabled.
    """
    if config.trace_backend == TraceBackend.OFF:
        return None

    needs_otlp_extra = (
        config.trace_backend == TraceBackend.OTEL_HTTP or config.otel_endpoint is not None
    )
    tracer: OtelTracer | None = None
    if needs_otlp_extra:
        _require_observability_extra(config)
        if config.otel_endpoint is not None:
            tracer = _build_otlp_tracer(config)

    return OtelSpanTraceStore(
        base_dir=base_dir,
        retain_reasoning_content=config.retain_reasoning_content,
        tracer=tracer,
    )


def _require_observability_extra(config: ObservabilityConfig) -> None:
    """Raise a clear ImportError if the ``[observability]`` extra is missing.

    Triggered when ``trace_backend=otel_http`` or ``otel_endpoint`` is set.
    The extra provides ``opentelemetry-sdk`` and
    ``opentelemetry-exporter-otlp-proto-http``.
    """
    import importlib

    required_modules = [
        "opentelemetry.sdk.trace",
        "opentelemetry.sdk.resources",
        "opentelemetry.sdk.trace.export",
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
    ]
    for mod_name in required_modules:
        try:
            importlib.import_module(mod_name)
        except ImportError as exc:
            raise ImportError(
                "OTLP trace export (trace_backend=otel_http or otel_endpoint set) "
                "requires the [observability] extra. "
                'Install it with:  uv pip install -e ".[observability]"  '
                "(or  pip install modex-agent[observability])"
            ) from exc


def _build_otlp_tracer(config: ObservabilityConfig) -> OtelTracer:
    """Build an OTel ``Tracer`` wired to a ``BatchSpanProcessor`` → ``OTLPSpanExporter``.

    The ``TracerProvider`` gets a ``Resource(service.name=otel_service_name)``
    and a single ``BatchSpanProcessor`` wrapping an ``OTLPSpanExporter``
    pointed at ``config.otel_endpoint``.  Spans created via the returned
    tracer are batched and exported asynchronously by the SDK.
    """
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[import-not-found]
        OTLPSpanExporter,
    )
    from opentelemetry.sdk.resources import Resource  # type: ignore[import-not-found]
    from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import-not-found]
    from opentelemetry.sdk.trace.export import BatchSpanProcessor  # type: ignore[import-not-found]

    resource = Resource.create({"service.name": config.otel_service_name})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=config.otel_endpoint)
    processor = BatchSpanProcessor(exporter)
    provider.add_span_processor(processor)
    return provider.get_tracer("modex_agent")


def _emit_span_via_otel_sdk(tracer: OtelTracer, span: SpanModel) -> None:
    """Emit a span via the OTel SDK for OTLP export.

    Maps the :class:`SpanModel` to an OTel SDK span (``tracer.start_span``
    → ``set_status`` → ``end``).  The SDK's ``SpanProcessor`` chain handles
    batching and export.  All imports are lazy so the module loads without
    the ``[observability]`` extra.
    """
    from opentelemetry.trace import (
        SpanKind as OtelSpanKind,
    )
    from opentelemetry.trace import (
        Status as OtelStatus,
    )
    from opentelemetry.trace import (
        StatusCode as OtelStatusCode,
    )

    otel_kind = OtelSpanKind.CLIENT if span.kind == SpanKind.CLIENT.value else OtelSpanKind.INTERNAL

    if span.status.code == SpanStatusCode.ERROR:
        otel_status_code = OtelStatusCode.ERROR
    elif span.status.code == SpanStatusCode.OK:
        otel_status_code = OtelStatusCode.OK
    else:
        otel_status_code = OtelStatusCode.UNSET

    otel_status = OtelStatus(otel_status_code, description=span.status.message)

    start_time_ns = int(span.start_time * 1_000_000_000)

    otel_span = tracer.start_span(
        span.name,
        kind=otel_kind,
        attributes=span.attributes,
        start_time=start_time_ns,
    )
    otel_span.set_status(otel_status)

    end_time_ns: int | None = None
    if span.end_time is not None:
        end_time_ns = int(span.end_time * 1_000_000_000)
    otel_span.end(end_time=end_time_ns)
