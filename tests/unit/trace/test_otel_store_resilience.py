"""Resilience tests for non-blocking span emission (R2/R3/R5/R6 + FILE gate).

Covers the resilience matrix from `.omo/plans/otel-collector-migration.md`:

- R2: collector connection refused -> save_span stays µs-scale, warning logged,
  drops counted at the sender.
- R3: collector accepts but never responds (black hole) -> save_span wall-clock
  unaffected, sender times out at 3 s, drops, keeps draining.
- R5: export queue bounded -> oldest dropped + counted, memory bounded.
- SG1: OTEL_HTTP store is write-only (reads raise; FILE reads stay).
- FILE mode never touches the network even when an endpoint is set.
"""

from __future__ import annotations

import logging
import socket
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from modex_agent.ioc.configs.observability import TraceBackend
from modex_agent.trace.otel_store import OtelSpanTraceStore
from modex_agent.trace.semconv import GenAiAttr, SpanName
from modex_agent.trace.store import SpanModel
from modex_agent.utils.file_io import read_jsonl_robust

_LOGGER_NAME = "modex_agent.trace.otel_store"
_POLL_INTERVAL = 0.02
_ASYNC_DEADLINE = 15.0
_NETWORK_DEADLINE = 45.0


def _make_span(
    span_id: str,
    *,
    session_id: str = "sess-r",
    start_time: float = 1000.0,
) -> SpanModel:
    return SpanModel(
        trace_id="trace-r",
        span_id=span_id,
        name=SpanName.INVOKE_AGENT.value,
        start_time=start_time,
        attributes={
            GenAiAttr.AGENT_NAME: "react_main",
            GenAiAttr.CONVERSATION_ID: session_id,
        },
    )


def _wait_until(predicate, timeout: float, description: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(_POLL_INTERVAL)
    pytest.fail(f"timed out after {timeout}s waiting for {description}")


class TestCloseDrain:
    async def test_close_exports_first_span_when_sender_starts_during_close(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original_sender_loop = OtelSpanTraceStore._sender_loop

        def _sender_after_close(store: OtelSpanTraceStore) -> None:
            assert store._stop_event.wait(timeout=_ASYNC_DEADLINE)
            original_sender_loop(store)

        monkeypatch.setattr(OtelSpanTraceStore, "_sender_loop", _sender_after_close)
        client = MagicMock()
        response = MagicMock()
        response.status_code = 200
        client.post.return_value = response
        monkeypatch.setattr(
            "modex_agent.trace.otel_store.httpx.Client",
            MagicMock(return_value=client),
        )
        store = OtelSpanTraceStore(
            base_dir=tmp_path,
            backend=TraceBackend.OTEL_HTTP,
            otlp_endpoint="http://collector:4318/v1/traces",
        )

        await store.save_span(_make_span("first"))
        store.close()

        assert store.exported_spans == 1
        assert store.dropped_spans == 0


# ── R2: collector refused ─────────────────────────────────────────────


class TestR2HttpErrorStatus:
    async def test_http_error_response_counts_as_dropped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-2xx OTLP response (e.g. 502 from an interceptor) means the
        span was NOT delivered — it must be dropped AND counted, never
        silently consumed."""
        client = MagicMock()
        response = MagicMock()
        response.status_code = 502
        client.post.return_value = response
        monkeypatch.setattr(
            "modex_agent.trace.otel_store.httpx.Client",
            MagicMock(return_value=client),
        )
        store = OtelSpanTraceStore(
            base_dir=tmp_path,
            backend=TraceBackend.OTEL_HTTP,
            otlp_endpoint="http://collector:4318/v1/traces",
        )
        try:
            await store.save_span(_make_span("s0"))
            _wait_until(
                lambda: store.dropped_spans >= 1,
                timeout=_ASYNC_DEADLINE,
                description="dropped_spans >= 1 after HTTP 502 response",
            )
            assert store.exported_spans == 0
        finally:
            store.close()


class TestR2CollectorRefused:
    async def test_save_span_fast_warning_and_drop_counter(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = OtelSpanTraceStore(
            base_dir=tmp_path,
            backend=TraceBackend.OTEL_HTTP,
            otlp_endpoint="http://127.0.0.1:1/v1/traces",
        )
        try:
            with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
                start = time.perf_counter()
                for i in range(5):
                    await store.save_span(_make_span(f"s{i}"))
                elapsed = time.perf_counter() - start

                assert elapsed < 0.1, f"5 save_span calls took {elapsed:.3f}s"
                assert not (tmp_path / "sess-r" / "spans.jsonl").exists()

                # Each refused POST can take ~2 s on Windows (first RST
                # swallowed, SYN retransmit honored) — budget generously.
                _wait_until(
                    lambda: store.dropped_spans >= 5,
                    timeout=_NETWORK_DEADLINE,
                    description="dropped_spans >= 5 after refused exports",
                )

            warnings = [
                r.getMessage()
                for r in caplog.records
                if r.levelno >= logging.WARNING and "OTLP" in r.getMessage()
            ]
            assert warnings, "expected at least one OTLP warning record"
        finally:
            store.close()


# ── R3: collector black hole ──────────────────────────────────────────


class _BlackHoleServer:
    """TCP server that completes handshakes (kernel backlog) but never responds."""

    def __init__(self) -> None:
        self._server = socket.socket()
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(16)
        self.port = self._server.getsockname()[1]

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1/traces"

    def close(self) -> None:
        self._server.close()


class TestR3CollectorBlackHole:
    async def test_save_span_unaffected_sender_times_out_and_drains(self, tmp_path: Path) -> None:
        hole = _BlackHoleServer()
        store = OtelSpanTraceStore(
            base_dir=tmp_path,
            backend=TraceBackend.OTEL_HTTP,
            otlp_endpoint=hole.endpoint,
        )
        try:
            for i in range(3):
                per_span_start = time.perf_counter()
                await store.save_span(_make_span(f"s{i}"))
                per_span_elapsed = time.perf_counter() - per_span_start
                assert per_span_elapsed < 0.1, f"save_span blocked for {per_span_elapsed:.3f}s"

            _wait_until(
                lambda: store.dropped_spans >= 3,
                timeout=_NETWORK_DEADLINE,
                description="dropped_spans >= 3 after sender timeouts",
            )
            _wait_until(
                lambda: store._export_queue.empty(),
                timeout=_ASYNC_DEADLINE,
                description="export queue drained",
            )
        finally:
            store.close()
            hole.close()


# ── R5: bounded export queue ──────────────────────────────────────────


class TestR5BoundedQueue:
    async def test_queue_full_drops_oldest_and_counts(self, tmp_path: Path) -> None:
        post_started = threading.Event()
        post_release = threading.Event()

        blocked_client = MagicMock()
        blocked_response = MagicMock()
        blocked_response.status_code = 200

        def _blocked_post(*args: object, **kwargs: object) -> object:
            post_started.set()
            post_release.wait(timeout=_NETWORK_DEADLINE)
            return blocked_response

        blocked_client.post = _blocked_post

        store = OtelSpanTraceStore(
            base_dir=tmp_path,
            backend=TraceBackend.OTEL_HTTP,
            otlp_endpoint="http://127.0.0.1:1/v1/traces",
            export_queue_size=8,
        )
        try:
            store._otlp_client = blocked_client
            await store.save_span(_make_span("in-flight"))
            assert post_started.wait(timeout=_ASYNC_DEADLINE)

            for i in range(20):
                await store.save_span(_make_span(f"burst-{i}"))

            assert store.dropped_spans >= 12
            assert store._export_queue.qsize() <= 8
        finally:
            post_release.set()
            store.close()


# ── Drop-oldest atomicity (Finding 3: sender race in save_span) ──────


class TestDropOldestAtomicity:
    async def test_no_escape_and_counts_accurate_under_drain_race(self, tmp_path: Path) -> None:
        """Sender racing the eviction get must never leak queue.Empty, and
        every span must end up exactly once in exported + dropped."""
        store = OtelSpanTraceStore(
            base_dir=tmp_path,
            backend=TraceBackend.OTEL_HTTP,
            otlp_endpoint="http://127.0.0.1:1/v1/traces",
            export_queue_size=1,
        )
        response = MagicMock()
        response.status_code = 200
        response.text = "{}"
        client = MagicMock()
        client.post = MagicMock(return_value=response)
        store._otlp_client = client

        total = 3000
        old_switch_interval = sys.getswitchinterval()
        # Amplify GIL switching so the sender thread interleaves inside the
        # Full->get_nowait window instead of only between save_span calls.
        sys.setswitchinterval(1e-6)
        try:
            for i in range(total):
                await store.save_span(_make_span(f"r{i}"))
        finally:
            sys.setswitchinterval(old_switch_interval)

        try:
            _wait_until(
                lambda: store.exported_spans + store.dropped_spans == total,
                timeout=10.0,
                description="every span either exported or dropped",
            )
            assert client.post.call_count == store.exported_spans
        finally:
            store.close()


# ── R6: OTLP client ownership belongs to the sender thread ───────────


class TestR6ClientOwnership:
    async def test_slow_post_close_returns_sender_closes_client_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Reviewer probe: join timeout must not close the client under the
        sender's feet, and the surviving sender must close its own client
        (exactly once) when its loop exits — no leaked replacement."""
        constructed: list[MagicMock] = []

        def _fake_client_factory(*args: object, **kwargs: object) -> MagicMock:
            mock = MagicMock()
            constructed.append(mock)
            return mock

        monkeypatch.setattr("modex_agent.trace.otel_store.httpx.Client", _fake_client_factory)

        post_started = threading.Event()
        post_release = threading.Event()
        response = MagicMock()
        response.status_code = 200
        response.text = "{}"

        def _slow_post(*args: object, **kwargs: object) -> object:
            post_started.set()
            post_release.wait(timeout=30.0)
            return response

        client = MagicMock()
        client.post = MagicMock(side_effect=_slow_post)

        store = OtelSpanTraceStore(
            base_dir=tmp_path,
            backend=TraceBackend.OTEL_HTTP,
            otlp_endpoint="http://127.0.0.1:1/v1/traces",
        )
        try:
            store._otlp_client = client
            await store.save_span(_make_span("s1"))
            _wait_until(
                post_started.is_set,
                timeout=5.0,
                description="sender entered the slow POST",
            )

            with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
                close_start = time.monotonic()
                store.close()
                close_elapsed = time.monotonic() - close_start

            assert close_elapsed < 3.0
            assert client.close.call_count == 0
            drain_warnings = [
                record.getMessage()
                for record in caplog.records
                if record.levelno >= logging.WARNING
                and "sender still draining" in record.getMessage()
            ]
            assert drain_warnings

            post_release.set()
            sender = store._sender_thread
            assert sender is not None
            _wait_until(
                lambda: not sender.is_alive(),
                timeout=10.0,
                description="sender exit after finishing the POST",
            )

            assert client.close.call_count == 1
            assert store._otlp_client is None
            assert constructed == []
        finally:
            post_release.set()
            store.close()

    async def test_idle_sender_close_closes_client_once_idempotent(self, tmp_path: Path) -> None:
        store = OtelSpanTraceStore(
            base_dir=tmp_path,
            backend=TraceBackend.OTEL_HTTP,
            otlp_endpoint="http://127.0.0.1:1/v1/traces",
        )
        client = MagicMock()
        store._otlp_client = client
        try:
            store.close()

            _wait_until(
                lambda: client.close.call_count == 1,
                timeout=5.0,
                description="client closed exactly once after idle close",
            )
            assert store._otlp_client is None

            store.close()
            assert client.close.call_count == 1
        finally:
            store.close()


# ── SG1: OTEL_HTTP store is write-only ────────────────────────────────


class TestWriteOnlyContract:
    async def test_otel_http_list_by_session_raises(self, tmp_path: Path) -> None:
        store = OtelSpanTraceStore(
            base_dir=tmp_path,
            backend=TraceBackend.OTEL_HTTP,
        )
        try:
            with pytest.raises(NotImplementedError, match="write-only"):
                await store.list_by_session("sess-r")
        finally:
            store.close()

    async def test_otel_http_list_by_trace_id_raises(self, tmp_path: Path) -> None:
        store = OtelSpanTraceStore(
            base_dir=tmp_path,
            backend=TraceBackend.OTEL_HTTP,
        )
        try:
            with pytest.raises(NotImplementedError, match="write-only"):
                await store.list_by_trace_id("trace-r")
        finally:
            store.close()

    async def test_file_backend_still_reads_jsonl(self, tmp_path: Path) -> None:
        store = OtelSpanTraceStore(base_dir=tmp_path)
        await store.save_span(_make_span("s1", session_id="sess-r"))
        await store.save_span(_make_span("s2", session_id="sess-other"))

        by_session = await store.list_by_session("sess-r")
        by_trace = await store.list_by_trace_id("trace-r")

        assert [s.span_id for s in by_session] == ["s1"]
        assert sorted(s.span_id for s in by_trace) == ["s1", "s2"]

    async def test_save_span_queues_and_sender_drains(self, tmp_path: Path) -> None:
        store = OtelSpanTraceStore(
            base_dir=tmp_path,
            backend=TraceBackend.OTEL_HTTP,
            otlp_endpoint="http://collector:4318/v1/traces",
        )
        response = MagicMock()
        response.status_code = 200
        response.text = "{}"
        client = MagicMock()
        client.post = MagicMock(return_value=response)
        store._otlp_client = client
        try:
            await store.save_span(_make_span("s1"))

            _wait_until(
                lambda: store.exported_spans == 1,
                timeout=_ASYNC_DEADLINE,
                description="exported_spans == 1 after queue drain",
            )
            with pytest.raises(NotImplementedError, match="write-only"):
                await store.list_by_session("sess-r")
        finally:
            store.close()


# ── FILE mode never touches the network ───────────────────────────────


class TestFileModeNoNetwork:
    async def test_file_backend_ignores_endpoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        emit_calls: list[object] = []
        monkeypatch.setattr(
            "modex_agent.trace.otel_store._emit_span_via_json_otlp",
            lambda *args: emit_calls.append(args),
        )
        store = OtelSpanTraceStore(
            base_dir=tmp_path,
            backend=TraceBackend.FILE,
            otlp_endpoint="http://127.0.0.1:1/v1/traces",
        )
        await store.save_span(_make_span("s1"))

        assert emit_calls == []
        assert store._otlp_client is None
        assert store.dropped_spans == 0
        spans = read_jsonl_robust(tmp_path / "sess-r" / "spans.jsonl")
        assert len(spans) == 1
        store.close()
