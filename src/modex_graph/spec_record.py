"""REST-friendly metadata view of a persisted graph specification."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class GraphSpecRecord(BaseModel):
    """Persisted graph specification metadata without the serialized spec."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    spec_id: int
    name: str
    version: str
    created_at: int


__all__ = ["GraphSpecRecord"]
