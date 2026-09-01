from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class OverflowMetadata(BaseModel):
    """Sidecar record persisted as ``.meta.json`` next to ``full.txt``.

    Also serves as the entry's commit marker: ``list_tool_call_ids`` counts
    only directories that carry a ``.meta.json``, so the file must be written
    LAST (after ``full.txt``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: str
    tool_call_id: str
    session_id: str
    created_at: str = Field(description="ISO 8601 timestamp")
    total_chars: int


class OverflowRef(BaseModel):
    """Reference to a persisted overflow entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dir_path: str
    total_chars: int
    metadata_path: str


class CleanRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    kept_call_ids: frozenset[str] = Field(default_factory=frozenset)
    max_tool_call_ids: int = 500
