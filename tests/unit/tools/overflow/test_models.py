from __future__ import annotations

import pytest

from framework.tools.overflow.models import CleanRequest, OverflowMetadata, OverflowRef


class TestOverflowMetadata:
    def test_create(self) -> None:
        meta = OverflowMetadata(
            tool_name="read_file",
            tool_call_id="call_001",
            session_id="sess_123",
            created_at="2026-05-17T10:00:00Z",
            total_chars=15000,
            total_chunks=2,
        )
        assert meta.tool_name == "read_file"
        assert meta.tool_call_id == "call_001"
        assert meta.session_id == "sess_123"
        assert meta.created_at == "2026-05-17T10:00:00Z"
        assert meta.total_chars == 15000
        assert meta.total_chunks == 2

    def test_immutable(self) -> None:
        meta = OverflowMetadata(
            tool_name="read_file",
            tool_call_id="call_001",
            session_id="sess_123",
            created_at="2026-05-17T10:00:00Z",
            total_chars=15000,
            total_chunks=2,
        )
        with pytest.raises(AttributeError):
            meta.total_chars = 20000  # type: ignore[misc]


class TestOverflowRef:
    def test_create(self) -> None:
        ref = OverflowRef(
            dir_path="/tmp/overflow/sess_123/call_001",
            chunk_count=2,
            total_chars=15000,
            metadata_path="/tmp/overflow/sess_123/call_001/.meta.json",
        )
        assert ref.dir_path == "/tmp/overflow/sess_123/call_001"
        assert ref.chunk_count == 2
        assert ref.total_chars == 15000
        assert ref.metadata_path == "/tmp/overflow/sess_123/call_001/.meta.json"

    def test_immutable(self) -> None:
        ref = OverflowRef(
            dir_path="/tmp/overflow/sess_123/call_001",
            chunk_count=2,
            total_chars=15000,
            metadata_path="/tmp/overflow/sess_123/call_001/.meta.json",
        )
        with pytest.raises(AttributeError):
            ref.chunk_count = 3  # type: ignore[misc]


class TestCleanRequest:
    def test_create(self) -> None:
        req = CleanRequest(
            session_id="sess_123",
            kept_call_ids={"call_001", "call_002"},
        )
        assert req.session_id == "sess_123"
        assert req.kept_call_ids == {"call_001", "call_002"}
        assert req.max_tool_call_ids == 500

    def test_custom_max(self) -> None:
        req = CleanRequest(
            session_id="sess_123",
            kept_call_ids=set(),
            max_tool_call_ids=1000,
        )
        assert req.max_tool_call_ids == 1000

    def test_mutable(self) -> None:
        req = CleanRequest(
            session_id="sess_123",
            kept_call_ids={"call_001"},
        )
        req.kept_call_ids.add("call_002")
        assert req.kept_call_ids == {"call_001", "call_002"}
