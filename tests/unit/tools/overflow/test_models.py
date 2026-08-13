from __future__ import annotations

from dataclasses import FrozenInstanceError, fields

import pytest

from modex_agent.tools.overflow.models import CleanRequest, OverflowMetadata, OverflowRef


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
        assert {field.name for field in fields(meta)} == {
            "tool_name",
            "tool_call_id",
            "session_id",
            "created_at",
            "total_chars",
        }

    def test_immutable(self) -> None:
        meta = OverflowMetadata(
            tool_name="read_file",
            tool_call_id="call_001",
            session_id="sess_123",
            created_at="2026-05-17T10:00:00Z",
            total_chars=15000,
        )
        with pytest.raises(FrozenInstanceError):
            meta.__setattr__("total_chars", 20000)


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
        assert {field.name for field in fields(ref)} == {
            "dir_path",
            "total_chars",
            "metadata_path",
        }

    def test_immutable(self) -> None:
        ref = OverflowRef(
            dir_path="/tmp/overflow/sess_123/call_001",
            total_chars=15000,
            metadata_path="/tmp/overflow/sess_123/call_001/.meta.json",
        )
        with pytest.raises(FrozenInstanceError):
            ref.__setattr__("total_chars", 20000)


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
