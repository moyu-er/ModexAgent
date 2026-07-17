"""Trace query — read-only ABC, span models, and JSON-lines span reader.

The write side lives on :class:`~modex_agent.trace.otel_store.OtelSpanTraceStore`
(``save_span``); this module owns the data models (:class:`SpanModel`,
:class:`SpanStatus`), the read-only :class:`TraceQuery` ABC, and a pure-read
:class:`JsonlSpanQuery` implementation that parses ``spans.jsonl`` via
:func:`modex_agent.utils.file_io.read_jsonl_robust`.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.trace.semconv import SpanKind, SpanStatusCode
from modex_agent.utils.file_io import read_jsonl_robust

logger = logging.getLogger(__name__)


# ── Span models (frozen Pydantic BaseModel) ────────────────────────────


class SpanStatus(BaseModel):
    """OTel span status."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: SpanStatusCode = SpanStatusCode.OK
    message: str | None = None


class SpanModel(BaseModel):
    """One OTel-compatible span, serialized as a single JSON line.

    The shape mirrors what an OTel SDK ``Span`` would export:
    ``trace_id``, ``span_id``, ``parent_span_id``, ``name``, ``kind``,
    ``start_time``, ``end_time``, ``attributes``, ``status``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    name: str
    kind: str = SpanKind.INTERNAL
    start_time: float
    end_time: float | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    status: SpanStatus = Field(default_factory=SpanStatus)


# ── TraceQuery ABC ─────────────────────────────────────────────────────


class TraceQuery(ABC):
    """Read-only query interface for OTel span traces."""

    @abstractmethod
    async def list_by_session(self, session_id: str) -> list[SpanModel]:
        """Return all spans for a given session, ordered by file order."""

    @abstractmethod
    async def list_by_trace_id(self, trace_id: str) -> list[SpanModel]:
        """Return all spans for a given trace_id across all sessions."""


class JsonlSpanQuery(TraceQuery):
    """Read-only ``spans.jsonl`` reader.

    File layout::

        {base_dir}/{session_id}/spans.jsonl

    Each line is a JSON-encoded :class:`SpanModel`.  Malformed lines are
    skipped by :func:`read_jsonl_robust`.
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir

    def _session_path(self, session_id: str) -> Path:
        return self._base_dir / session_id / "spans.jsonl"

    async def list_by_session(self, session_id: str) -> list[SpanModel]:
        path = self._session_path(session_id)
        data = read_jsonl_robust(path)
        out: list[SpanModel] = []
        for d in data:
            span = _parse_span(d)
            if span is not None:
                out.append(span)
        return out

    async def list_by_trace_id(self, trace_id: str) -> list[SpanModel]:
        if not self._base_dir.exists():
            return []
        out: list[SpanModel] = []
        for session_dir in self._base_dir.iterdir():
            if not session_dir.is_dir():
                continue
            path = session_dir / "spans.jsonl"
            data = read_jsonl_robust(path)
            for d in data:
                if d.get("trace_id") == trace_id:
                    span = _parse_span(d)
                    if span is not None:
                        out.append(span)
        return out


def _parse_span(data: dict[str, object]) -> SpanModel | None:
    """Best-effort parse of a JSON dict into a :class:`SpanModel`.

    Returns ``None`` (with a warning) for malformed lines so a single bad
    entry does not poison the whole query result.
    """
    try:
        return SpanModel.model_validate(data)
    except Exception:
        logger.warning("Skipping malformed span line: %s", str(data)[:120])
        return None
