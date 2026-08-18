"""OTel-compatible span trace store — backend-gated persistence + async OTLP export.

:class:`OtelSpanTraceStore` persists :class:`SpanModel` values (OTel-compatible
span JSON) in one of two modes selected by :class:`TraceBackend`:

- ``FILE`` (default): append-only ``spans.jsonl`` per session — the local
  store for agent self-read. Never touches the network, regardless of
  ``otlp_endpoint``.
- ``OTEL_HTTP``: no local jsonl, and the store is WRITE-ONLY —
  :meth:`save_span` appends to a bounded export queue drained by a single
  daemon sender thread that POSTs each span via JSON OTLP
  (``_emit_span_via_json_otlp``, 3 s client timeout). The read methods
  :meth:`list_by_session` / :meth:`list_by_trace_id` raise
  ``NotImplementedError`` in this mode; read traces back through
  :class:`modex_agent.trace.langfuse_query.LangfuseTraceQuery` instead. The
  hot path never touches the network and never raises — a slow or down
  collector can stall the sender, never the agent.

The ``[observability]`` extra (``opentelemetry-sdk`` +
``opentelemetry-exporter-otlp-proto-http``) is required only when OTLP
export is active.  Without it, the module loads and operates in file-only
mode.

Trace hooks construct :class:`SpanModel` values directly and call
:meth:`save_span`; this store no longer takes :class:`OperationRecord`.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from modex_agent.ioc.configs.observability import ObservabilityConfig, TraceBackend
from modex_agent.trace.semconv import GenAiAttr, SpanKind, SpanStatusCode
from modex_agent.trace.store import SpanModel, TraceQuery

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer as OtelTracer

logger = logging.getLogger(__name__)

_SPANS_FILENAME = "spans.jsonl"
_SENDER_JOIN_TIMEOUT_S = 2.0
_WARN_WINDOW_S = 5.0


# ── OtelSpanTraceStore ────────────────────────────────────────────────


class OtelSpanTraceStore(TraceQuery):
    """Persists OTel-compatible span JSON per the selected :class:`TraceBackend`.

    ``FILE`` file layout::

        {base_dir}/{session_id}/spans.jsonl

    Each line is a JSON-encoded :class:`SpanModel`. IO errors propagate to
    the caller; no OTLP client is ever created in this mode.

    ``OTEL_HTTP`` is write-only: :meth:`save_span` hands each span to a
    bounded export queue (``export_queue_size``) drained by a daemon sender
    thread; when the queue is full the oldest span is dropped and counted
    (:attr:`dropped_spans`); sender-side export failures drop the span and
    count it too. Warnings are rate-limited to one per failure kind per
    5 s window. Reads are not supported — :meth:`list_by_session` and
    :meth:`list_by_trace_id` raise ``NotImplementedError``; use
    ``LangfuseTraceQuery`` to read traces back (cross-process by design).

    Direct JSON OTLP preserves our trace_id, span_id, and parent_span_id
    exactly as written in SpanModel, so Langfuse receives a correct trace
    tree.
    """

    def __init__(
        self,
        base_dir: Path,
        *,
        backend: TraceBackend = TraceBackend.FILE,
        retain_reasoning_content: bool = True,
        tracer: OtelTracer | None = None,
        otlp_endpoint: str | None = None,
        otlp_headers: dict[str, str] | None = None,
        otlp_service_name: str = "modex_agent",
        export_queue_size: int = 10_000,
    ) -> None:
        self._base_dir = base_dir
        self._backend = backend
        self._retain_reasoning_content = retain_reasoning_content
        self._tracer: OtelTracer | None = tracer
        self._otlp_endpoint = otlp_endpoint
        self._otlp_headers = otlp_headers or {}
        self._otlp_service_name = otlp_service_name

        self._export_queue: queue.Queue[SpanModel] = queue.Queue(maxsize=export_queue_size)
        self._stats_lock = threading.Lock()
        self._dropped_spans = 0
        self._exported_spans = 0
        self._warn_times: dict[str, float] = {}
        self._stop_event = threading.Event()
        self._closed = False

        # Owned by the sender thread; created lazily on first export so a
        # pre-save injection (tests) or a failed construction never leaks.
        self._otlp_client: httpx.Client | None = None
        self._sender_thread: threading.Thread | None = None
        if backend == TraceBackend.OTEL_HTTP and otlp_endpoint is not None:
            self._sender_thread = threading.Thread(
                target=self._sender_loop,
                name=f"otel-span-sender-{id(self):x}",
                daemon=True,
            )
            self._sender_thread.start()

    @property
    def retain_reasoning_content(self) -> bool:
        return self._retain_reasoning_content

    @property
    def backend(self) -> TraceBackend:
        return self._backend

    @property
    def dropped_spans(self) -> int:
        """Total spans dropped: export-queue overflow (oldest evicted) plus sender-side export failures."""
        with self._stats_lock:
            return self._dropped_spans

    @property
    def exported_spans(self) -> int:
        """Spans handed to the OTLP endpoint without exception."""
        with self._stats_lock:
            return self._exported_spans

    def _session_path(self, session_id: str) -> Path:
        return self._base_dir / session_id / _SPANS_FILENAME

    def _count_drop(self) -> None:
        with self._stats_lock:
            self._dropped_spans += 1

    def _warn_rate_limited(self, kind: str, message: str, *args: object) -> None:
        now = time.monotonic()
        with self._stats_lock:
            last = self._warn_times.get(kind, 0.0)
            if now - last < _WARN_WINDOW_S:
                return
            self._warn_times[kind] = now
        logger.warning(message, *args)

    async def save_span(self, span: SpanModel) -> None:
        """Persist a single :class:`SpanModel` (µs-scale hot path).

        ``FILE`` backend: appends one JSON line to
        ``{base_dir}/{session_id}/spans.jsonl``; IO errors propagate.

        ``OTEL_HTTP`` backend: appends to the bounded export queue only;
        the daemon sender thread performs the OTLP POST asynchronously.
        This path never raises and never touches the network — export-queue
        overflow evicts the oldest queued span and increments
        :attr:`dropped_spans`.

        When ``retain_reasoning_content`` is ``False``, the
        ``gen_ai.output.reasoning_content`` attribute is stripped before
        persisting.
        """
        span_to_write = span
        if not self._retain_reasoning_content:
            stripped = {k: v for k, v in span.attributes.items() if k != GenAiAttr.OUTPUT_REASONING_CONTENT}
            span_to_write = span.model_copy(update={"attributes": stripped})

        session_id = str(span_to_write.attributes.get(GenAiAttr.CONVERSATION_ID.value, "unknown"))

        if self._backend == TraceBackend.FILE:
            path = self._session_path(session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(span_to_write.model_dump(mode="json"), ensure_ascii=False)
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            return

        try:
            self._export_queue.put_nowait(span_to_write)
        except queue.Full:
            # Drop-oldest is not atomic: between the failed put and the
            # eviction get the sender may drain the queue — get_nowait then
            # raises Empty, meaning space already exists and nothing was
            # dropped (no count). The retried put can only hit Full again if
            # another producer refilled that window; then the NEW span is
            # dropped and counted instead.
            try:
                self._export_queue.get_nowait()
            except queue.Empty:
                pass
            else:
                self._count_drop()
                self._warn_rate_limited(
                    "queue-full",
                    "OTLP export queue full (%s items) — dropped oldest span",
                    self._export_queue.maxsize,
                )
            try:
                self._export_queue.put_nowait(span_to_write)
            except queue.Full:
                self._count_drop()
                self._warn_rate_limited(
                    "queue-full-new",
                    "OTLP export queue full (%s items) — dropped new span",
                    self._export_queue.maxsize,
                )

    def _sender_loop(self) -> None:
        try:
            while True:
                try:
                    span = self._export_queue.get(timeout=0.1)
                except queue.Empty:
                    if self._stop_event.is_set():
                        return
                    continue
                self._send_one(span)
                self._export_queue.task_done()
        finally:
            # The sender owns the client lifecycle: it created the client
            # (lazily, possibly as a replacement mid-drain after close()
            # timed out), so it closes it on loop exit — exactly once.
            self._close_client_once()

    def _send_one(self, span: SpanModel) -> None:
        endpoint = self._otlp_endpoint
        if endpoint is None:
            return
        client = self._ensure_client()
        if client is None:
            self._count_drop()
            return
        try:
            _emit_span_via_json_otlp(
                client,
                endpoint,
                self._otlp_headers,
                self._otlp_service_name,
                span,
            )
        except Exception as exc:
            self._count_drop()
            self._warn_rate_limited(
                f"export-failed:{type(exc).__name__}",
                "OTLP export failed for span %s (%s: %s) — span dropped",
                span.name,
                type(exc).__name__,
                exc,
            )
            return
        with self._stats_lock:
            self._exported_spans += 1

    def _ensure_client(self) -> httpx.Client | None:
        if self._otlp_client is None:
            try:
                self._otlp_client = httpx.Client(timeout=httpx.Timeout(3.0))
            except Exception as exc:
                self._warn_rate_limited(
                    "client-create-failed",
                    "Failed to create httpx.Client for OTLP export (%s) — span dropped",
                    exc,
                )
        return self._otlp_client

    def _close_client_once(self) -> None:
        """Close the OTLP client exactly once; concurrent callers are no-ops.

        The swap-to-None under the lock is the exactly-once claim: whichever
        closer (sender-loop exit or ``close()`` backstop) wins the swap does
        the closing; the other sees ``None``.
        """
        with self._stats_lock:
            client = self._otlp_client
            if client is None:
                return
            self._otlp_client = None
        try:
            client.close()
        except Exception:
            logger.debug("OTLP client close failed", exc_info=True)

    def close(self) -> None:
        """Graceful shutdown: signal stop, join the sender (≤ 2 s), close the OTLP client.

        The client lifecycle belongs to the sender thread: it lazily creates
        the client (drain-time replacement included) and closes it when its
        loop exits. If the sender is still alive after the join (e.g. stuck
        in a 3 s POST), this method logs a warning and returns WITHOUT
        touching the client — the sender closes it on exit. When the sender
        is dead (or never started), any remaining client is closed here
        (backstop; the sender will usually have closed it itself already).
        Idempotent.
        """
        if self._closed:
            return
        self._closed = True
        self._stop_event.set()
        sender = self._sender_thread
        if sender is not None:
            sender.join(timeout=_SENDER_JOIN_TIMEOUT_S)
            if sender.is_alive():
                logger.warning(
                    "OTLP sender still draining after %.1fs join — sender owns "
                    "client cleanup and will close it when its loop exits",
                    _SENDER_JOIN_TIMEOUT_S,
                )
                return
        self._close_client_once()

    async def list_by_session(self, session_id: str) -> list[SpanModel]:
        """Return spans for *session_id* (``FILE`` only).

        ``FILE``: all spans from ``spans.jsonl`` in file order.
        ``OTEL_HTTP``: raises ``NotImplementedError`` — the store is
        write-only; read traces back via ``LangfuseTraceQuery``.
        """
        if self._backend == TraceBackend.FILE:
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
        raise NotImplementedError("OTEL_HTTP store is write-only; use LangfuseTraceQuery")

    async def list_by_trace_id(self, trace_id: str) -> list[SpanModel]:
        """Return all spans for *trace_id* across all sessions (``FILE`` only).

        ``FILE``: scans every session dir's ``spans.jsonl``.
        ``OTEL_HTTP``: raises ``NotImplementedError`` — the store is
        write-only; read traces back via ``LangfuseTraceQuery``.
        """
        if self._backend == TraceBackend.FILE:
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
                            logger.warning("Skipping malformed span line: %s", str(d)[:120])
            return out
        raise NotImplementedError("OTEL_HTTP store is write-only; use LangfuseTraceQuery")


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
    - ``OTEL_HTTP``: returns an :class:`OtelSpanTraceStore` in write-only
      sender mode — no local JSONL, no read buffers; spans go to the
      bounded export queue drained by the daemon sender thread that
      exports via OTLP. Requires the
      ``[observability]`` extra AND non-empty ``otel_endpoint`` +
      ``otel_headers``; when either is missing the factory falls back to a
      FILE-mode store (jsonl) with a warning.

    Args:
        config: The observability configuration.
        base_dir: Base directory for file-based trace stores
                  (``{base_dir}/{session_id}/``).

    Returns:
        An :class:`OtelSpanTraceStore` or ``None`` when disabled.
    """
    if config.trace_backend == TraceBackend.OFF:
        return None

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
                "Falling back to file-only trace backend.",
                exc,
            )

    # OTLP export is gated SOLELY on trace_backend==OTEL_HTTP. FILE must never
    # touch the network, even when otel_endpoint/otel_headers are set (they are
    # ignored). Previously this OR'd with ``otel_endpoint is not None``, which
    # leaked OTLP export into FILE mode whenever an endpoint was configured —
    # and bot_config.yml's default makes otel_endpoint always non-null.
    otlp_active = config.trace_backend == TraceBackend.OTEL_HTTP and otlp_headers is not None
    return OtelSpanTraceStore(
        base_dir=base_dir,
        backend=TraceBackend.OTEL_HTTP if otlp_active else TraceBackend.FILE,
        retain_reasoning_content=config.retain_reasoning_content,
        tracer=tracer,
        otlp_endpoint=config.otel_endpoint if otlp_active else None,
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
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

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
        if isinstance(value, bool | str | bytes | int | float):
            sanitized[key] = value
        elif isinstance(value, list | tuple):
            if all(isinstance(v, bool | str | bytes | int | float) for v in value):
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
        logger.warning("OTLP endpoint returned HTTP %s", response.status_code)


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
    if isinstance(value, list | tuple):
        return {
            "arrayValue": {
                "values": [_to_otlp_value(v) for v in value],
            }
        }
    # dict or other — JSON-serialize
    import json

    return {"stringValue": json.dumps(value, ensure_ascii=False, default=str)}
