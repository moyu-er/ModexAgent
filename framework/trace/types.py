"""Trace types: OperationRecord and related enums."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from framework.runtime.enums import OperationKind, OperationStatus


@dataclass
class OperationRecord:
    """A single operation traced during agent execution.

    One JSON line in operations.jsonl.  ``trace_id`` groups all operations
    in one turn; ``session_id`` groups all turns in one agent session.
    """

    trace_id: str  # Globally unique per turn
    session_id: str  # {conv}:{agent}[:{invocation}]
    agent_name: str
    invocation_id: str | None = None
    kind: OperationKind = OperationKind.LLM_CALL
    status: OperationStatus = OperationStatus.COMPLETED
    timestamp: float = 0.0
    duration_ms: int | None = None  # null for start/end markers
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-serialisable dict (one line in .jsonl)."""
        d: dict[str, Any] = {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "agent_name": self.agent_name,
            "kind": str(self.kind),
            "status": str(self.status),
            "timestamp": self.timestamp,
        }
        if self.invocation_id is not None:
            d["invocation_id"] = self.invocation_id
        if self.duration_ms is not None:
            d["duration_ms"] = self.duration_ms
        if self.metadata:
            d["metadata"] = self.metadata
        if self.error:
            d["error"] = self.error
        return d
