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

import httpx

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
    exports via JSON OTLP HTTP POST.

    File layout::

        {base_dir}/{session_id}/spans.jsonl

    Each line is a JSON-encoded :class:`SpanModel`.

    When ``otlp_endpoint`` is provided, :meth:`save_span` also exports each
    span via direct JSON OTLP HTTP POST (not via the OTel SDK). This bypasses
    the SDK's context-propagation model, which is incompatible with our
    "write SpanModel first, forward later" architecture — the SDK generates
    its own trace_id/span_id, losing our parent-child relationships.

    Direct JSON OTLP preserves our trace_id, span_id, and parent_span_id
    exactly as written in SpanModel, so Langfuse receives a correct trace
    tree. A failure in OTLP export is logged and does not prevent the
    JSONL write.
    """

    def __init__(
        self,
        base_dir: Path,
        *,
        retain_reasoning_content: bool = True,
        tracer: OtelTracer | None = None,
        otlp_endpoint: str | None = None,
        otlp_headers: dict[str, str] | None = None,
        otlp_service_name: str = "modex_agent",
    ) -> None:
        self._base_dir = base_dir
        self._retain_reasoning_content = retain_reasoning_content
        self._tracer: OtelTracer | None = tracer
        self._otlp_endpoint = otlp_endpoint
        self._otlp_headers = otlp_headers or {}
        self._otlp_service_name = otlp_service_name
        self._otlp_client: httpx.Client | None = None
        if otlp_endpoint:
            try:
                self._otlp_client = httpx.Client(timeout=httpx.Timeout(10.0))
            except Exception:
                logger.warning("Failed to create httpx.Client for OTLP export")

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

        session_id = str(span_to_write.attributes.get(GenAiAttr.CONVERSATION_ID.value, "unknown"))
        path = self._session_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(span_to_write.model_dump(mode="json"), ensure_ascii=False)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

        if self._otlp_endpoint and self._otlp_client is not None:
            try:
                _emit_span_via_json_otlp(
                    self._otlp_client,
                    self._otlp_endpoint,
                    self._otlp_headers,
                    self._otlp_service_name,
                    span_to_write,
                )
            except Exception:
                logger.warning(
                    "OtelSpanTraceStore: JSON OTLP export failed",
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
    - ``FILE`` (default): returns an :class:`OtelSpanTraceStore` that writes
      local JSONL only — no network traffic, regardless of ``otel_endpoint``
      / ``otel_headers``. Those fields are ignored in this mode.
    - ``OTEL_HTTP``: returns an :class:`OtelSpanTraceStore` that writes local
      JSONL AND exports via OTLP when ``otel_endpoint`` + ``otel_headers`` are
      both set. Requires the ``[observability]`` extra when OTLP export is
      active; falls back to file-only if the extra is missing or headers are
      empty.

    Args:
        config: The observability configuration.
        base_dir: Base directory for file-based trace stores
                  (``{base_dir}/{session_id}/``).

    Returns:
        An :class:`OtelSpanTraceStore` or ``None`` when disabled.
    """
    if config.trace_backend == TraceBackend.OFF:
        return None

    # OTLP export is gated SOLELY on trace_backend==OTEL_HTTP. FILE must never
    # touch the network, even when otel_endpoint/otel_headers are set (they are
    # ignored). Previously this OR'd with ``otel_endpoint is not None``, which
    # leaked OTLP export into FILE mode whenever an endpoint was configured —
    # and bot_config.yml's default makes otel_endpoint always non-null.
    needs_otlp_extra = config.trace_backend == TraceBackend.OTEL_HTTP
    tracer: OtelTracer | None = None
    otlp_headers: dict[str, str] | None = None
    if needs_otlp_extra:
        try:
            _require_observability_extra(config)
            if config.otel_endpoint is not None:
                headers = config.otel_headers
                if headers is not None:
                    headers = {k: v for k, v in headers.items() if v}
                    if not headers:
                        headers = None
                if headers is None and config.otel_headers is not None:
                    import logging

                    logging.getLogger(__name__).warning(
                        "OTLP export disabled (otel_headers configured but all "
                        "values are empty — check LANGFUSE_* env vars). "
                        "Falling back to file-only trace backend."
                    )
                else:
                    otlp_headers = headers
                    tracer = _build_otlp_tracer(config)
        except ImportError as exc:
            import logging

            logging.getLogger(__name__).warning(
                "OTLP export disabled (observability extra missing): %s. "
                "Falling back to file-only trace backend.",
                exc,
            )
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "OTLP export disabled (tracer build failed): %s. "
                "Falling back to file-only trace backend. Bot will continue "
                "without remote trace export.",
                exc,
                exc_info=True,
            )

    return OtelSpanTraceStore(
        base_dir=base_dir,
        retain_reasoning_content=config.retain_reasoning_content,
        tracer=tracer,
        otlp_endpoint=config.otel_endpoint if otlp_headers is not None else None,
        otlp_headers=otlp_headers or {},
        otlp_service_name=config.otel_service_name,
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

    The provider is also registered globally via ``trace.set_tracer_provider``
    so that ``trace.get_tracer()`` calls in other modules (e.g.
    ``external/agent.py`` for subprocess CLIENT spans) use the same
    provider and export via the same OTLP endpoint.  ``set_tracer_provider``
    is idempotent — the first call wins; subsequent calls log a warning and
    are no-ops, so multi-pool builds sharing one endpoint are safe.
    """
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[import-not-found]
        OTLPSpanExporter,
    )
    from opentelemetry.sdk.resources import Resource  # type: ignore[import-not-found]
    from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import-not-found]
    from opentelemetry.sdk.trace.export import BatchSpanProcessor  # type: ignore[import-not-found]

    resource = Resource.create({"service.name": config.otel_service_name})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(
        endpoint=config.otel_endpoint,
        headers=config.otel_headers,
    )
    processor = BatchSpanProcessor(exporter)
    provider.add_span_processor(processor)
    # G11 fix: register globally so external/agent.py's trace.get_tracer()
    # returns a tracer from this provider, not the no-op default. Best-effort —
    # set_tracer_provider can only be called once per process; subsequent calls
    # are no-ops (logged as warnings by the OTel SDK). This is fine because all
    # pools share the same OTLP endpoint and service name.
    try:
        from opentelemetry.trace import get_tracer_provider, set_tracer_provider

        current = get_tracer_provider()
        if hasattr(current, "__class__") and current.__class__.__name__ == "TracerProvider":
            # Already set by a previous pool — skip silently.
            pass
        else:
            set_tracer_provider(provider)
    except (ImportError, AttributeError):
        pass
    return provider.get_tracer("modex_agent")


def _sanitize_attrs_for_otel(attrs: dict[str, object]) -> dict[str, object]:
    """Convert attributes to OTel SDK-compatible types.

    OTel span attributes accept only scalars (bool/str/bytes/int/float) or
    sequences of scalars. Nested dicts/lists-of-dicts (e.g.
    ``gen_ai.input.messages``) are JSON-serialized to strings. ``None``
    values are dropped (OTel SDK rejects them).
    """
    import json

    sanitized: dict[str, object] = {}
    for key, value in attrs.items():
        if value is None:
            continue
        if isinstance(value, (bool, str, bytes, int, float)):
            sanitized[key] = value
        elif isinstance(value, (list, tuple)):
            if all(isinstance(v, (bool, str, bytes, int, float)) for v in value):
                sanitized[key] = list(value)
            else:
                sanitized[key] = json.dumps(value, ensure_ascii=False, default=str)
        elif isinstance(value, dict):
            sanitized[key] = json.dumps(value, ensure_ascii=False, default=str)
        else:
            sanitized[key] = json.dumps(value, ensure_ascii=False, default=str)
    return sanitized


def _emit_span_via_json_otlp(
    client: httpx.Client,
    endpoint: str,
    headers: dict[str, str],
    service_name: str,
    span: SpanModel,
) -> None:
    """Export a span via direct JSON OTLP HTTP POST."""
    import json

    trace_id = span.trace_id.ljust(32, "0")[:32]
    span_id = span.span_id.ljust(16, "0")[:16]
    parent_span_id = None
    if span.parent_span_id is not None:
        parent_span_id = span.parent_span_id.ljust(16, "0")[:16]

    start_time_ns = str(int(span.start_time * 1_000_000_000))
    end_time_ns = str(int((span.end_time or span.start_time) * 1_000_000_000))

    otel_attrs = _sanitize_attrs_for_otel(span.attributes)

    otlp_span: dict[str, object] = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": span.name,
        "kind": (
            "SPAN_KIND_CLIENT"
            if span.kind == SpanKind.CLIENT.value
            else "SPAN_KIND_INTERNAL"
        ),
        "startTimeUnixNano": start_time_ns,
        "endTimeUnixNano": end_time_ns,
        "attributes": [
            {"key": k, "value": _to_otlp_value(v)} for k, v in otel_attrs.items()
        ],
        "status": {
            "code": (
                "STATUS_CODE_ERROR"
                if span.status.code == SpanStatusCode.ERROR
                else "STATUS_CODE_OK"
                if span.status.code == SpanStatusCode.OK
                else "STATUS_CODE_UNSET"
            ),
        },
    }
    if parent_span_id is not None:
        otlp_span["parentSpanId"] = parent_span_id
    if span.status.message:
        status: dict[str, object] = otlp_span["status"]  # type: ignore[assignment]
        status["message"] = span.status.message

    payload = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": service_name}},
                    ],
                },
                "scopeSpans": [{"spans": [otlp_span]}],
            }
        ]
    }

    response = client.post(
        endpoint,
        json=payload,
        headers={**headers, "Content-Type": "application/json"},
    )
    if response.status_code >= 400:
        logger.warning(
            "JSON OTLP export returned %s: %s",
            response.status_code,
            response.text[:200],
        )


def _to_otlp_value(value: object) -> dict[str, object]:
    """Convert a Python value to an OTLP JSON AnyValue."""
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, bytes):
        import base64
        return {"bytesValue": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (list, tuple)):
        return {
            "arrayValue": {
                "values": [_to_otlp_value(v) for v in value],
            }
        }
    # dict or other — JSON-serialize
    import json

    return {"stringValue": json.dumps(value, ensure_ascii=False, default=str)}
