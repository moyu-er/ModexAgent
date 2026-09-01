from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_agent.tools.overflow.models import CleanRequest, OverflowMetadata, OverflowRef

_META_FIELDS = {"tool_name", "tool_call_id", "session_id", "created_at", "total_chars"}
_REF_FIELDS = {"dir_path", "total_chars", "metadata_path"}


class TestOverflowMetadata:
    def test_create(self) -> None:
        meta = OverflowMetadata(
            tool_name="read_file",
            tool_call_id="call_001",
            session_id="sess_123",
            created_at="2026-05-17T10:00:00Z",
            total_chars=15000,
        )
        assert meta.tool_name == "read_file"
        assert meta.tool_call_id == "call_001"
        assert meta.session_id == "sess_123"
        assert meta.created_at == "2026-05-17T10:00:00Z"
        assert meta.total_chars == 15000
        assert set(OverflowMetadata.model_fields) == _META_FIELDS

    def test_immutable(self) -> None:
        meta = OverflowMetadata(
            tool_name="read_file",
            tool_call_id="call_001",
            session_id="sess_123",
            created_at="2026-05-17T10:00:00Z",
            total_chars=15000,
        )
        with pytest.raises(ValidationError):
            meta.total_chars = 20000

    def test_roundtrip(self) -> None:
        meta = OverflowMetadata(
            tool_name="read_file",
            tool_call_id="call_001",
            session_id="sess_123",
            created_at="2026-05-17T10:00:00Z",
            total_chars=15000,
        )
        assert OverflowMetadata.model_validate_json(meta.model_dump_json()) == meta


class TestOverflowRef:
    def test_create(self) -> None:
        ref = OverflowRef(
            dir_path="/tmp/overflow/sess_123/call_001",
            total_chars=15000,
            metadata_path="/tmp/overflow/sess_123/call_001/.meta.json",
        )
        assert ref.dir_path == "/tmp/overflow/sess_123/call_001"
        assert ref.total_chars == 15000
        assert ref.metadata_path == "/tmp/overflow/sess_123/call_001/.meta.json"
        assert set(OverflowRef.model_fields) == _REF_FIELDS

    def test_immutable(self) -> None:
        ref = OverflowRef(
            dir_path="/tmp/overflow/sess_123/call_001",
            total_chars=15000,
            metadata_path="/tmp/overflow/sess_123/call_001/.meta.json",
        )
        with pytest.raises(ValidationError):
            ref.total_chars = 20000


class TestCleanRequest:
    def test_create(self) -> None:
        req = CleanRequest(
            session_id="sess_123",
            kept_call_ids=frozenset({"call_001", "call_002"}),
        )
        assert req.session_id == "sess_123"
        assert req.kept_call_ids == frozenset({"call_001", "call_002"})
        assert req.max_tool_call_ids == 500

    def test_custom_max(self) -> None:
        req = CleanRequest(
            session_id="sess_123",
            kept_call_ids=frozenset(),
            max_tool_call_ids=1000,
        )
        assert req.max_tool_call_ids == 1000
