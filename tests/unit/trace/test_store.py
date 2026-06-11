"""Unit tests for framework.trace store and types."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from framework.runtime.enums import OperationKind, OperationStatus
from framework.trace.store import JsonFileTraceStore
from framework.trace.types import OperationRecord


def _make_record(
    trace_id: str = "trace-001",
    session_id: str = "sess-001",
    agent_name: str = "test-agent",
    kind: OperationKind = OperationKind.LLM_CALL,
    **overrides: object,
) -> OperationRecord:
    """Build a minimal OperationRecord with sensible defaults."""
    defaults: dict[str, object] = {
        "trace_id": trace_id,
        "session_id": session_id,
        "agent_name": agent_name,
        "kind": kind,
    }
    defaults.update(overrides)
    return OperationRecord(**defaults)  # type: ignore[arg-type]


@pytest.fixture()
def tmp_store(tmp_path: Path) -> JsonFileTraceStore:
    """Provide a JsonFileTraceStore backed by a temporary directory."""
    return JsonFileTraceStore(base_dir=tmp_path)


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


class TestJsonFileTraceStore:
    async def test_save_and_list_by_session(self, tmp_store: JsonFileTraceStore) -> None:
        record = _make_record(
            trace_id="t1",
            session_id="s1",
            agent_name="agent-a",
            kind=OperationKind.TOOL_CALL,
            status=OperationStatus.COMPLETED,
            timestamp=1000.0,
            duration_ms=42,
        )

        await tmp_store.save(record)
        results = await tmp_store.list_by_session("s1")

        assert len(results) == 1
        got = results[0]
        assert got.trace_id == "t1"
        assert got.session_id == "s1"
        assert got.agent_name == "agent-a"
        assert got.kind == OperationKind.TOOL_CALL
        assert got.status == OperationStatus.COMPLETED
        assert got.duration_ms == 42

    async def test_list_by_session_empty_when_no_file(
        self, tmp_store: JsonFileTraceStore
    ) -> None:
        results = await tmp_store.list_by_session("nonexistent-session")
        assert results == []

    async def test_list_by_trace_id(self, tmp_store: JsonFileTraceStore) -> None:
        r1 = _make_record(trace_id="t1", session_id="s1")
        r2 = _make_record(trace_id="t2", session_id="s1")

        await tmp_store.save(r1)
        await tmp_store.save(r2)

        results = await tmp_store.list_by_trace_id("t1")
        assert len(results) == 1
        assert results[0].trace_id == "t1"

        results_t2 = await tmp_store.list_by_trace_id("t2")
        assert len(results_t2) == 1
        assert results_t2[0].trace_id == "t2"

    async def test_save_creates_parent_dir(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "nested" / "dir"
        store = JsonFileTraceStore(base_dir=nested)

        record = _make_record(session_id="s1")
        await store.save(record)

        assert (nested / "s1" / "operations.jsonl").exists()
        results = await store.list_by_session("s1")
        assert len(results) == 1


class TestOperationRecord:
    def test_to_json_dict_minimal(self) -> None:
        record = _make_record()
        d = record.to_json_dict()

        assert d["trace_id"] == "trace-001"
        assert d["session_id"] == "sess-001"
        assert d["kind"] == "llm_call"
        assert d["status"] == "completed"
        # Optional fields omitted when default / empty
        assert "invocation_id" not in d
        assert "duration_ms" not in d
        assert "metadata" not in d
        assert "error" not in d

    def test_to_json_dict_full(self) -> None:
        record = _make_record(
            invocation_id="inv-1",
            duration_ms=100,
            metadata={"key": "value"},
            error="something broke",
        )
        d = record.to_json_dict()

        assert d["invocation_id"] == "inv-1"
        assert d["duration_ms"] == 100
        assert d["metadata"] == {"key": "value"}
        assert d["error"] == "something broke"
