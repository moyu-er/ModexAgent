from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OverflowMetadata:
    tool_name: str
    tool_call_id: str
    session_id: str
    created_at: str  # ISO 8601
    total_chars: int
    total_chunks: int


@dataclass(frozen=True)
class OverflowRef:
    dir_path: str
    chunk_count: int
    total_chars: int
    metadata_path: str


@dataclass
class CleanRequest:
    session_id: str
    kept_call_ids: set[str]
    max_tool_call_ids: int = 500
