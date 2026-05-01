"""ApprovalStateStore ABC and default implementations."""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path

from .state import ApprovalRequest, ApprovalState


class ApprovalStateStore(ABC):
    """Approval state persistence abstraction."""

    @abstractmethod
    async def save(self, state: ApprovalState) -> None: ...

    @abstractmethod
    async def load(self, session_id: str) -> ApprovalState | None: ...

    @abstractmethod
    async def delete(self, session_id: str) -> None: ...


class InMemoryApprovalStateStore(ApprovalStateStore):
    """In-memory store for testing and InlineWait strategy."""

    def __init__(self) -> None:
        self._store: dict[str, ApprovalState] = {}

    async def save(self, state: ApprovalState) -> None:
        self._store[state.session_id] = state

    async def load(self, session_id: str) -> ApprovalState | None:
        return self._store.get(session_id)

    async def delete(self, session_id: str) -> None:
        self._store.pop(session_id, None)


class LocalFileApprovalStateStore(ApprovalStateStore):
    """Default: JSON file persistence."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self._workspace.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
        return self._workspace / f"{safe}_approval.json"

    async def save(self, state: ApprovalState) -> None:
        data = {
            "session_id": state.session_id,
            "requests": [
                {"tool_name": r.tool_name, "tool_call_id": r.tool_call_id,
                 "arguments": r.arguments, "tier": r.tier, "iteration": r.iteration}
                for r in state.requests
            ],
            "decisions": state.decisions,
            "status": state.status,
        }
        self._path(state.session_id).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

    async def load(self, session_id: str) -> ApprovalState | None:
        path = self._path(session_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        reqs = [ApprovalRequest(**r) for r in data["requests"]]
        return ApprovalState(
            session_id=data["session_id"], requests=reqs,
            decisions=data.get("decisions", {}), status=data.get("status", "pending"),
        )

    async def delete(self, session_id: str) -> None:
        self._path(session_id).unlink(missing_ok=True)
