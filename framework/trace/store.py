"""Trace store — ABC and JSON-lines file implementation."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path

from framework.trace.types import OperationKind, OperationRecord, OperationStatus

logger = logging.getLogger(__name__)


class TraceStore(ABC):
    """Abstract interface for persisting and querying operation traces."""

    @abstractmethod
    async def save(self, record: OperationRecord) -> None:
        """Persist a single operation record."""

    @abstractmethod
    async def list_by_session(self, session_id: str) -> list[OperationRecord]:
        """Return all records for a given session, ordered by timestamp."""

    @abstractmethod
    async def list_by_trace_id(self, trace_id: str) -> list[OperationRecord]:
        """Return all records for a given trace_id across all sessions."""


def _record_from_json(data: dict[str, object]) -> OperationRecord:
    """Deserialise a JSON dict into an OperationRecord."""
    return OperationRecord(
        trace_id=str(data["trace_id"]),
        session_id=str(data["session_id"]),
        agent_name=str(data["agent_name"]),
        invocation_id=data.get("invocation_id") if data.get("invocation_id") is not None else None,
        kind=OperationKind(str(data["kind"])),
        status=OperationStatus(str(data["status"])),
        timestamp=float(data.get("timestamp", 0.0)),
        duration_ms=int(data["duration_ms"]) if data.get("duration_ms") is not None else None,
        metadata=data.get("metadata", {}),
        error=str(data["error"]) if data.get("error") is not None else None,
    )


class JsonFileTraceStore(TraceStore):
    """Append-only JSON-lines store.

    File layout::

        {base_dir}/{session_id}/operations.jsonl

    Each line is a JSON-encoded ``OperationRecord``.
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir

    def _session_path(self, session_id: str) -> Path:
        return self._base_dir / session_id / "operations.jsonl"

    async def save(self, record: OperationRecord) -> None:
        path = self._session_path(record.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record.to_json_dict(), ensure_ascii=False)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    async def list_by_session(self, session_id: str) -> list[OperationRecord]:
        path = self._session_path(session_id)
        if not path.exists():
            return []
        records: list[OperationRecord] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                records.append(_record_from_json(data))
            except (json.JSONDecodeError, KeyError, ValueError):
                logger.warning("Skipping malformed trace line: %s", line[:120])
        return records

    async def list_by_trace_id(self, trace_id: str) -> list[OperationRecord]:
        records: list[OperationRecord] = []
        if not self._base_dir.exists():
            return records
        for session_dir in self._base_dir.iterdir():
            if not session_dir.is_dir():
                continue
            path = session_dir / "operations.jsonl"
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if data.get("trace_id") == trace_id:
                        records.append(_record_from_json(data))
                except (json.JSONDecodeError, KeyError, ValueError):
                    logger.warning("Skipping malformed trace line: %s", line[:120])
        return records
