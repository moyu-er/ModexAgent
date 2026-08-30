from __future__ import annotations

import json
from typing import Final

from bot.eval.harbor.entry import UsageArtifact
from bot.eval.harbor.pool_mode_types import (
    PoolModeConfig,
    PoolTaskResultArtifact,
    PoolUsageArtifact,
    SpanExporter,
)
from modex_agent.ioc.configs.observability import TraceBackend
from modex_agent.trace.experiment_attrs import attach_experiment_attrs
from modex_agent.trace.otel_store import OtelSpanTraceStore
from modex_agent.trace.semconv import GenAiAttr, SpanName
from modex_agent.trace.store import SpanModel

_SPANS_FILENAME: Final = "spans.jsonl"


class PoolTraceStore(OtelSpanTraceStore):
    """Eval harness trace store: local JSONL record + best-effort OTLP export.

    Unlike the production ``OTEL_HTTP`` store (write-only), the harness always
    appends each span to ``{trace_dir}/{session_id}/spans.jsonl`` so the host
    can read trials back via :class:`~modex_agent.trace.store.JsonlSpanQuery`
    even when the collector is unreachable.
    """

    def __init__(self, config: PoolModeConfig, exporter: SpanExporter | None) -> None:
        self._trace_dir = config.data_dir / "runtime_state" / config.pool_name / "trace"
        super().__init__(
            self._trace_dir,
            backend=TraceBackend.OTEL_HTTP,
            otlp_endpoint=config.entry.otel_traces_endpoint if exporter is None else None,
            otlp_service_name="modex_harbor_pool",
        )
        self._config = config
        self._task_name = config.entry.task_name
        self._exporter = exporter
        self._missing_endpoint_drops = 0
        self._usage = UsageArtifact(model=config.entry.model)

    @property
    def dropped_span_count(self) -> int:
        return self._missing_endpoint_drops + self.dropped_spans

    @property
    def usage(self) -> UsageArtifact:
        return self._usage.model_copy(update={"dropped_span_count": self.dropped_span_count})

    async def save_span(self, span: SpanModel) -> None:
        mapped = (
            attach_experiment_attrs(span, self._config.entry.experiment)
            if span.name == SpanName.INVOKE_AGENT
            else span
        )
        if mapped.name == SpanName.INVOKE_AGENT and self._task_name is not None:
            # Langfuse names a trace after its root observation's span name, so
            # the task name rides the span name for trace-list visibility.
            # Applies to every invoke_agent span (subagent roots can be
            # parentless), keeping the observation tree uniformly named.
            mapped = mapped.model_copy(
                update={"name": f"{SpanName.INVOKE_AGENT.value}/{self._task_name}"}
            )
        if mapped.name == SpanName.CHAT:
            attrs = mapped.attributes
            self._usage = self._usage.model_copy(
                update={
                    "input_tokens": self._usage.input_tokens
                    + int(attrs.get(GenAiAttr.USAGE_INPUT_TOKENS, 0)),
                    "output_tokens": self._usage.output_tokens
                    + int(attrs.get(GenAiAttr.USAGE_OUTPUT_TOKENS, 0)),
                    "cache_read_tokens": self._usage.cache_read_tokens
                    + int(attrs.get(GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS, 0)),
                    "cache_write_tokens": self._usage.cache_write_tokens
                    + int(attrs.get(GenAiAttr.USAGE_CACHE_CREATION_INPUT_TOKENS, 0)),
                }
            )
        self._persist_span(mapped)
        if self._exporter is not None:
            await self._exporter(mapped)
        elif self._config.entry.otel_traces_endpoint is not None:
            await super().save_span(mapped)
        else:
            self._missing_endpoint_drops += 1

    def _persist_span(self, span: SpanModel) -> None:
        session_id = str(span.attributes.get(GenAiAttr.CONVERSATION_ID.value, "unknown"))
        path = self._trace_dir / session_id / _SPANS_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(span.model_dump(mode="json"), ensure_ascii=False)
        with path.open("a", encoding="utf-8") as spans_file:
            spans_file.write(line + "\n")


def write_trace_record(config: PoolModeConfig, trace_id: str, session_id: str) -> None:
    record = {
        "trace_id": trace_id,
        "turn": 1,
        "session_id": session_id,
        "experiment_id": config.entry.experiment.experiment_id,
        "item_id": config.entry.experiment.item_id,
        "task_name": config.entry.task_name,
        "pool_name": config.pool_name,
    }
    config.entry.output_dir.mkdir(parents=True, exist_ok=True)
    (config.entry.output_dir / "trace-ids.jsonl").write_text(
        json.dumps(record) + "\n",
        encoding="utf-8",
    )


def write_pool_artifacts(
    config: PoolModeConfig,
    instruction: str,
    result: PoolTaskResultArtifact,
    usage: PoolUsageArtifact,
) -> None:
    output_dir = config.entry.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "instruction-rendered.txt").write_text(instruction, encoding="utf-8")
    (output_dir / "result.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    (output_dir / "usage.json").write_text(usage.model_dump_json(indent=2), encoding="utf-8")
    (output_dir / "trajectory.jsonl").write_text(result.model_dump_json() + "\n", encoding="utf-8")
    summary = (
        f"# Result\n\n{result.output}" if result.error is None else f"# Error\n\n{result.error}"
    )
    (output_dir / "summary.md").write_text(summary + "\n", encoding="utf-8")
