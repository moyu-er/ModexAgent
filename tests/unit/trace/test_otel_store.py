"""Unit tests for OtelSpanTraceStore and build_trace_stores."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("opentelemetry.sdk")  # skip if opentelemetry-sdk not installed (CI [dev] doesn't include [observability] deps)

from modex_agent.ioc.configs.observability import ObservabilityConfig, TraceBackend
from modex_agent.trace.otel_store import (
    OtelSpanTraceStore,
    build_trace_stores,
)
from modex_agent.trace.semconv import GenAiAttr, SpanName, SpanStatusCode
from modex_agent.trace.store import SpanModel, SpanStatus
from modex_agent.utils.file_io import read_jsonl_robust


def _make_span(
    trace_id: str = "trace-001",
    session_id: str = "sess-001",
    agent_name: str = "react_main",
    name: str = SpanName.INVOKE_AGENT.value,
    span_id: str = "span-001",
    **overrides: object,
) -> SpanModel:
    defaults: dict[str, object] = {
        "trace_id": trace_id,
        "span_id": span_id,
        "name": name,
        "start_time": 1000.0,
        "attributes": {
            GenAiAttr.AGENT_NAME: agent_name,
            GenAiAttr.CONVERSATION_ID: session_id,
        },
    }
    defaults.update(overrides)
    return SpanModel(**defaults)  # type: ignore[arg-type]


# ── OtelSpanTraceStore.save_span ──────────────────────────────────────


class TestOtelSpanTraceStoreSaveSpan:
    async def test_save_span_writes_jsonl(self, tmp_path: Path) -> None:
        store = OtelSpanTraceStore(base_dir=tmp_path)
        span = _make_span(name=SpanName.INVOKE_AGENT.value)
        await store.save_span(span)

        spans_path = tmp_path / "sess-001" / "spans.jsonl"
        assert spans_path.exists()
        spans = read_jsonl_robust(spans_path)
        assert len(spans) == 1
        assert spans[0]["name"] == SpanName.INVOKE_AGENT.value
        assert spans[0]["attributes"][GenAiAttr.CONVERSATION_ID] == "sess-001"

    async def test_save_span_appends_to_existing_file(self, tmp_path: Path) -> None:
        store = OtelSpanTraceStore(base_dir=tmp_path)
        await store.save_span(_make_span(name=SpanName.INVOKE_AGENT.value, span_id="s1"))
        await store.save_span(
            _make_span(name=SpanName.CHAT.value, span_id="s2", parent_span_id="s1")
        )

        spans = read_jsonl_robust(tmp_path / "sess-001" / "spans.jsonl")
        assert len(spans) == 2
        assert spans[0]["name"] == SpanName.INVOKE_AGENT.value
        assert spans[1]["name"] == SpanName.CHAT.value
        assert spans[1]["parent_span_id"] == "s1"

    async def test_save_span_creates_parent_dir(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "nested"
        store = OtelSpanTraceStore(base_dir=nested)
        await store.save_span(_make_span(session_id="s1"))
        assert (nested / "s1" / "spans.jsonl").exists()

    async def test_spans_file_created_at_expected_path(self, tmp_path: Path) -> None:
        store = OtelSpanTraceStore(base_dir=tmp_path)
        await store.save_span(_make_span())
        expected = tmp_path / "sess-001" / "spans.jsonl"
        assert expected.exists()

    async def test_spans_readable_with_read_jsonl_robust(self, tmp_path: Path) -> None:
        store = OtelSpanTraceStore(base_dir=tmp_path)
        await store.save_span(_make_span(name=SpanName.INVOKE_AGENT.value))
        await store.save_span(
            _make_span(
                name=SpanName.CHAT.value,
                span_id="s2",
                end_time=1001.1,
            )
        )

        path = tmp_path / "sess-001" / "spans.jsonl"
        spans = read_jsonl_robust(path)
        assert len(spans) == 2
        for span in spans:
            assert "trace_id" in span
            assert "span_id" in span
            assert "name" in span
            assert "attributes" in span

    async def test_otel_emission_failure_preserves_jsonl(
        self, tmp_path: Path
    ) -> None:
        failing_tracer = MagicMock()
        failing_tracer.start_span.side_effect = RuntimeError("OTLP unreachable")

        store = OtelSpanTraceStore(base_dir=tmp_path, tracer=failing_tracer)
        span = _make_span(name=SpanName.INVOKE_AGENT.value)
        await store.save_span(span)

        spans = read_jsonl_robust(tmp_path / "sess-001" / "spans.jsonl")
        assert len(spans) == 1


# ── OtelSpanTraceStore queries ────────────────────────────────────────


class TestOtelSpanTraceStoreQueries:
    async def test_list_by_session_returns_spans(self, tmp_path: Path) -> None:
        store = OtelSpanTraceStore(base_dir=tmp_path)
        await store.save_span(_make_span(name=SpanName.INVOKE_AGENT.value, span_id="s1"))
        await store.save_span(
            _make_span(name=SpanName.CHAT.value, span_id="s2", parent_span_id="s1")
        )

        results = await store.list_by_session("sess-001")
        assert len(results) == 2
        assert results[0].name == SpanName.INVOKE_AGENT.value
        assert results[1].name == SpanName.CHAT.value

    async def test_list_by_session_empty_when_no_file(self, tmp_path: Path) -> None:
        store = OtelSpanTraceStore(base_dir=tmp_path)
        results = await store.list_by_session("nonexistent")
        assert results == []

    async def test_list_by_trace_id(self, tmp_path: Path) -> None:
        store = OtelSpanTraceStore(base_dir=tmp_path)
        await store.save_span(_make_span(trace_id="t1", session_id="s1", span_id="sp1"))
        await store.save_span(_make_span(trace_id="t2", session_id="s1", span_id="sp2"))

        results = await store.list_by_trace_id("t1")
        assert len(results) == 1
        assert results[0].trace_id == "t1"

    async def test_list_by_trace_id_across_sessions(self, tmp_path: Path) -> None:
        store = OtelSpanTraceStore(base_dir=tmp_path)
        await store.save_span(
            _make_span(trace_id="shared", session_id="s1", span_id="sp1")
        )
        await store.save_span(
            _make_span(trace_id="shared", session_id="s2", span_id="sp2")
        )

        results = await store.list_by_trace_id("shared")
        assert len(results) == 2

    async def test_list_by_trace_id_empty_when_no_base_dir(self, tmp_path: Path) -> None:
        store = OtelSpanTraceStore(base_dir=tmp_path / "does_not_exist")
        results = await store.list_by_trace_id("any")
        assert results == []


# ── build_trace_stores ────────────────────────────────────────────────


class TestBuildTraceStores:
    def test_off_returns_none(self) -> None:
        config = ObservabilityConfig(trace_backend=TraceBackend.OFF)
        result = build_trace_stores(config, Path("/tmp/unused"))
        assert result is None

    def test_file_returns_otel_store(self, tmp_path: Path) -> None:
        config = ObservabilityConfig(trace_backend=TraceBackend.FILE)
        result = build_trace_stores(config, tmp_path)
        assert result is not None
        assert isinstance(result, OtelSpanTraceStore)

    async def test_file_writes_spans_jsonl(self, tmp_path: Path) -> None:
        config = ObservabilityConfig(trace_backend=TraceBackend.FILE)
        store = build_trace_stores(config, tmp_path)
        assert store is not None

        span = _make_span(name=SpanName.INVOKE_AGENT.value)
        await store.save_span(span)

        assert (tmp_path / "sess-001" / "spans.jsonl").exists()

    async def test_off_mode_produces_no_spans_file(self, tmp_path: Path) -> None:
        config = ObservabilityConfig(trace_backend=TraceBackend.OFF)
        store = build_trace_stores(config, tmp_path)
        assert store is None
        assert not (tmp_path / "sess-001" / "spans.jsonl").exists()

    def test_otel_http_without_extra_raises_import_error(self, tmp_path: Path) -> None:
        config = ObservabilityConfig(trace_backend=TraceBackend.OTEL_HTTP)
        try:
            result = build_trace_stores(config, tmp_path)
            assert isinstance(result, OtelSpanTraceStore)
        except ImportError as exc:
            assert "[observability]" in str(exc)
            assert "pip install" in str(exc)

    async def test_retain_false_propagated_through_factory(self, tmp_path: Path) -> None:
        config = ObservabilityConfig(
            trace_backend=TraceBackend.FILE,
            retain_reasoning_content=False,
        )
        store = build_trace_stores(config, tmp_path)
        assert store is not None
        assert store.retain_reasoning_content is False


# ── OTLP export (T3) ──────────────────────────────────────────────────


@pytest.fixture
def fake_otel(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """Inject fake opentelemetry modules into ``sys.modules`` for testing."""

    created_spans: list[object] = []
    created_providers: list[object] = []
    created_exporters: list[object] = []
    created_processors: list[object] = []

    class FakeSpanContext:
        def __init__(self, trace_id: int = 0, span_id: int = 0, is_remote: bool = False, trace_flags: object = None, trace_state: object = None) -> None:
            self.trace_id = trace_id
            self.span_id = span_id
            self.is_remote = is_remote
            self.trace_flags = trace_flags
            self.trace_state = trace_state if trace_state is not None else {}

    class FakeTraceFlags:
        SAMPLED = 1

        def __init__(self, flags: int = 0) -> None:
            self.flags = flags

    class FakeSpan:
        def __init__(
            self,
            name: str,
            kind: str,
            attributes: dict[str, object] | None,
            start_time: int | None,
        ) -> None:
            self.name = name
            self.kind = kind
            self.attributes = dict(attributes) if attributes else {}
            self.start_time = start_time
            self.status: object | None = None
            self.end_time: int | None = None
            created_spans.append(self)

        def set_status(self, status: object) -> None:
            self.status = status

        def end(self, end_time: int | None = None) -> None:
            self.end_time = end_time

    class FakeStatus:
        def __init__(self, code: str, description: str | None = None) -> None:
            self.code = code
            self.description = description

    class FakeStatusCode:
        OK = "OK"
        ERROR = "ERROR"
        UNSET = "UNSET"

    class FakeSpanKind:
        INTERNAL = "INTERNAL"
        CLIENT = "CLIENT"
        SERVER = "SERVER"
        PRODUCER = "PRODUCER"
        CONSUMER = "CONSUMER"

    class FakeTracer:
        def start_span(
            self,
            name: str,
            kind: str = FakeSpanKind.INTERNAL,
            attributes: dict[str, object] | None = None,
            start_time: int | None = None,
            context: object | None = None,
        ) -> FakeSpan:
            return FakeSpan(name, kind, attributes, start_time)

    class FakeTracerProvider:
        def __init__(self, resource: object | None = None) -> None:
            self.resource = resource
            self.processors: list[object] = []
            created_providers.append(self)

        def add_span_processor(self, processor: object) -> None:
            self.processors.append(processor)

        def get_tracer(self, name: str) -> FakeTracer:
            return FakeTracer()

    class FakeResourceInstance:
        def __init__(self, attrs: dict[str, str]) -> None:
            self.attrs = attrs

    class FakeResource:
        @staticmethod
        def create(attrs: dict[str, str]) -> FakeResourceInstance:
            return FakeResourceInstance(attrs)

    class FakeBatchSpanProcessor:
        def __init__(self, exporter: object) -> None:
            self.exporter = exporter
            created_processors.append(self)

    class FakeOTLPSpanExporter:
        def __init__(
            self,
            endpoint: str | None = None,
            headers: dict[str, str] | None = None,
        ) -> None:
            self.endpoint = endpoint
            self.headers = headers
            created_exporters.append(self)

    modules: dict[str, types.ModuleType] = {
        "opentelemetry": types.ModuleType("opentelemetry"),
        "opentelemetry.trace": types.ModuleType("opentelemetry.trace"),
        "opentelemetry.trace.propagation": types.ModuleType("opentelemetry.trace.propagation"),
        "opentelemetry.sdk": types.ModuleType("opentelemetry.sdk"),
        "opentelemetry.sdk.trace": types.ModuleType("opentelemetry.sdk.trace"),
        "opentelemetry.sdk.resources": types.ModuleType("opentelemetry.sdk.resources"),
        "opentelemetry.sdk.trace.export": types.ModuleType("opentelemetry.sdk.trace.export"),
        "opentelemetry.exporter": types.ModuleType("opentelemetry.exporter"),
        "opentelemetry.exporter.otlp": types.ModuleType("opentelemetry.exporter.otlp"),
        "opentelemetry.exporter.otlp.proto": types.ModuleType("opentelemetry.exporter.otlp.proto"),
        "opentelemetry.exporter.otlp.proto.http": types.ModuleType(
            "opentelemetry.exporter.otlp.proto.http"
        ),
        "opentelemetry.exporter.otlp.proto.http.trace_exporter": types.ModuleType(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter"
        ),
    }

    modules["opentelemetry.trace"].SpanKind = FakeSpanKind  # type: ignore[attr-defined]
    modules["opentelemetry.trace"].Status = FakeStatus  # type: ignore[attr-defined]
    modules["opentelemetry.trace"].StatusCode = FakeStatusCode  # type: ignore[attr-defined]
    modules["opentelemetry.trace"].SpanContext = FakeSpanContext  # type: ignore[attr-defined]
    modules["opentelemetry.trace"].TraceFlags = FakeTraceFlags  # type: ignore[attr-defined]
    modules["opentelemetry.trace"].set_span_in_context = lambda span: {}  # type: ignore[attr-defined]
    modules["opentelemetry.trace"].Span = FakeSpan  # type: ignore[attr-defined]
    modules["opentelemetry.trace"].NonRecordingSpan = lambda ctx: FakeSpan("", "", None, None)  # type: ignore[attr-defined]
    modules["opentelemetry.sdk.trace"].TracerProvider = FakeTracerProvider  # type: ignore[attr-defined]
    modules["opentelemetry.sdk.resources"].Resource = FakeResource  # type: ignore[attr-defined]
    modules["opentelemetry.sdk.trace.export"].BatchSpanProcessor = (  # type: ignore[attr-defined]
        FakeBatchSpanProcessor
    )
    modules["opentelemetry.exporter.otlp.proto.http.trace_exporter"].OTLPSpanExporter = (  # type: ignore[attr-defined]
        FakeOTLPSpanExporter
    )

    for name, mod in modules.items():
        monkeypatch.setitem(sys.modules, name, mod)

    return types.SimpleNamespace(
        spans=created_spans,
        providers=created_providers,
        exporters=created_exporters,
        processors=created_processors,
        FakeTracer=FakeTracer,
        FakeSpan=FakeSpan,
        FakeTracerProvider=FakeTracerProvider,
        FakeResource=FakeResource,
        FakeBatchSpanProcessor=FakeBatchSpanProcessor,
        FakeOTLPSpanExporter=FakeOTLPSpanExporter,
    )


class TestOtlpExport:
    _OBS_EXTRA_INSTALLED: bool = importlib.util.find_spec(
        "opentelemetry.sdk.trace"
    ) is not None

    @pytest.mark.skipif(
        _OBS_EXTRA_INSTALLED,
        reason="[observability] extra installed — cannot test missing-extra fallback",
    )
    def test_otel_endpoint_set_without_extra_falls_back_to_file(self, tmp_path: Path) -> None:
        """OTLP extra missing → fail-open to file-only, no exception (ADR-0024 IN9)."""
        config = ObservabilityConfig(
            trace_backend=TraceBackend.FILE,
            otel_endpoint="http://localhost:4318/v1/traces",
        )
        store = build_trace_stores(config, tmp_path)
        assert store is not None
        assert store._tracer is None

    @pytest.mark.skipif(
        _OBS_EXTRA_INSTALLED,
        reason="[observability] extra installed — cannot test missing-extra fallback",
    )
    def test_otel_http_without_extra_falls_back_to_file(self, tmp_path: Path) -> None:
        """OTLP extra missing → fail-open to file-only, no exception (ADR-0024 IN9)."""
        config = ObservabilityConfig(trace_backend=TraceBackend.OTEL_HTTP)
        store = build_trace_stores(config, tmp_path)
        assert store is not None
        assert store._tracer is None

    def test_otel_endpoint_not_set_file_only_no_tracer(self, tmp_path: Path) -> None:
        config = ObservabilityConfig(trace_backend=TraceBackend.FILE)
        store = build_trace_stores(config, tmp_path)
        assert store is not None
        assert isinstance(store, OtelSpanTraceStore)
        assert store._tracer is None

    def test_file_mode_ignores_endpoint_and_headers_no_otlp(
        self, tmp_path: Path, fake_otel: types.SimpleNamespace
    ) -> None:
        """FILE mode must never export via OTLP, even with endpoint+headers set.

        Regression: bot_config.yml defaults otel_endpoint to a non-null
        ${LANGFUSE_HOST:-http://localhost:3000}/... URL. Previously the gate
        OR'd ``otel_endpoint is not None`` into the OTLP decision, so FILE
        mode leaked spans to Langfuse whenever LANGFUSE_BASIC_AUTH was set.
        """
        config = ObservabilityConfig(
            trace_backend=TraceBackend.FILE,
            otel_endpoint="http://localhost:3000/api/public/otel/v1/traces",
            otel_headers={"Authorization": "Basic cGstbGYtNzQx"},
        )
        store = build_trace_stores(config, tmp_path)
        assert store is not None
        assert isinstance(store, OtelSpanTraceStore)
        assert store._tracer is None
        assert store._otlp_endpoint is None
        assert store._otlp_client is None
        assert len(fake_otel.exporters) == 0

    def test_otel_endpoint_set_with_extra_builds_otlp_tracer(
        self, tmp_path: Path, fake_otel: types.SimpleNamespace
    ) -> None:
        endpoint = "http://collector:4318/v1/traces"
        config = ObservabilityConfig(
            trace_backend=TraceBackend.OTEL_HTTP,
            otel_endpoint=endpoint,
            otel_service_name="test-service",
        )
        store = build_trace_stores(config, tmp_path)
        assert store is not None
        assert isinstance(store, OtelSpanTraceStore)

        assert len(fake_otel.providers) == 1
        assert len(fake_otel.exporters) == 1
        assert len(fake_otel.processors) == 1

        assert fake_otel.exporters[0].endpoint == endpoint
        assert fake_otel.processors[0].exporter is fake_otel.exporters[0]
        assert fake_otel.processors[0] in fake_otel.providers[0].processors

        assert store._tracer is not None

    def test_otel_http_with_endpoint_and_extra_builds_otlp_tracer(
        self, tmp_path: Path, fake_otel: types.SimpleNamespace
    ) -> None:
        endpoint = "http://otel:4318/v1/traces"
        config = ObservabilityConfig(
            trace_backend=TraceBackend.OTEL_HTTP,
            otel_endpoint=endpoint,
        )
        store = build_trace_stores(config, tmp_path)
        assert store is not None
        assert isinstance(store, OtelSpanTraceStore)
        assert len(fake_otel.exporters) == 1
        assert fake_otel.exporters[0].endpoint == endpoint

    def test_otel_http_without_endpoint_no_tracer_but_extra_required(
        self, tmp_path: Path, fake_otel: types.SimpleNamespace
    ) -> None:
        config = ObservabilityConfig(trace_backend=TraceBackend.OTEL_HTTP)
        store = build_trace_stores(config, tmp_path)
        assert store is not None
        assert isinstance(store, OtelSpanTraceStore)
        assert store._tracer is None
        assert len(fake_otel.exporters) == 0

    async def test_save_span_emits_span_via_json_otlp(
        self, tmp_path: Path, fake_otel: types.SimpleNamespace
    ) -> None:
        config = ObservabilityConfig(
            trace_backend=TraceBackend.OTEL_HTTP,
            otel_endpoint="http://collector:4318/v1/traces",
            otel_headers={"Authorization": "Basic dGVzdDp0ZXN0"},
        )
        store = build_trace_stores(config, tmp_path)
        assert store is not None

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "{}"
        store._otlp_client = MagicMock()
        store._otlp_client.post = MagicMock(return_value=mock_response)

        span = _make_span(name=SpanName.INVOKE_AGENT.value)
        await store.save_span(span)

        spans = read_jsonl_robust(tmp_path / "sess-001" / "spans.jsonl")
        assert len(spans) == 1

        assert store._otlp_client.post.called
        call_kwargs = store._otlp_client.post.call_args
        payload = call_kwargs[1]["json"] if "json" in call_kwargs[1] else call_kwargs[0][1]
        rs = payload["resourceSpans"][0]
        assert "resource" in rs
        res_attrs = {a["key"]: a["value"] for a in rs["resource"]["attributes"]}
        assert res_attrs["service.name"]["stringValue"] == "modex_agent"
        otlp_span = rs["scopeSpans"][0]["spans"][0]
        assert "resource" not in otlp_span
        assert otlp_span["name"] == SpanName.INVOKE_AGENT.value
        attrs_dict = {a["key"]: a["value"] for a in otlp_span["attributes"]}
        assert attrs_dict[GenAiAttr.AGENT_NAME]["stringValue"] == "react_main"
        assert attrs_dict[GenAiAttr.CONVERSATION_ID]["stringValue"] == "sess-001"

    async def test_save_span_emits_error_span_status_via_json_otlp(
        self, tmp_path: Path, fake_otel: types.SimpleNamespace
    ) -> None:
        config = ObservabilityConfig(
            trace_backend=TraceBackend.OTEL_HTTP,
            otel_endpoint="http://collector:4318/v1/traces",
            otel_headers={"Authorization": "Basic dGVzdDp0ZXN0"},
        )
        store = build_trace_stores(config, tmp_path)
        assert store is not None

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "{}"
        store._otlp_client = MagicMock()
        store._otlp_client.post = MagicMock(return_value=mock_response)

        await store.save_span(_make_span(name=SpanName.INVOKE_AGENT.value, span_id="s1"))
        error_span = _make_span(
            name=SpanName.ERROR.value,
            span_id="s2",
            status=SpanStatus(code=SpanStatusCode.ERROR, message="boom"),
        )
        await store.save_span(error_span)

        assert store._otlp_client.post.call_count == 2
        call_kwargs = store._otlp_client.post.call_args[1]
        payload = call_kwargs["json"] if "json" in call_kwargs else call_kwargs[0][1]
        otlp_span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert otlp_span["status"]["code"] == "STATUS_CODE_ERROR"
        assert otlp_span["status"]["message"] == "boom"


# ── format_send_ack single-path ──────────────────────────────────────


class TestFormatSendAckSinglePath:
    def test_ack_omits_trace_paths(self, tmp_path: Path) -> None:
        from modex_agent.multi_agent.comm_kind import AgentCommKind
        from modex_agent.multi_agent.communication.result import (
            AgentSendResult,
            format_send_ack,
        )

        trace_dir = tmp_path / "trace" / "sess-001"
        result = AgentSendResult(
            target_agent="worker",
            target_kind=AgentCommKind.SUBAGENT,
            session_id="sess-001",
            invocation_id="inv-1",
            created_new_task=True,
            trace_dir=trace_dir,
        )
        ack = format_send_ack(result)
        assert "spans.jsonl" not in ack
        assert "operations.jsonl" not in ack
        assert "OTel" not in ack

    def test_ack_no_trace_dir_omits_paths(self) -> None:
        from modex_agent.multi_agent.comm_kind import AgentCommKind
        from modex_agent.multi_agent.communication.result import (
            AgentSendResult,
            format_send_ack,
        )

        result = AgentSendResult(
            target_agent="worker",
            target_kind=AgentCommKind.SUBAGENT,
            session_id="sess-001",
            invocation_id="inv-1",
            created_new_task=True,
            trace_dir=None,
        )
        ack = format_send_ack(result)
        assert "spans.jsonl" not in ack
        assert "operations.jsonl" not in ack
