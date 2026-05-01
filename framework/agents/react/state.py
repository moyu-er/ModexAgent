"""TurnResumeState — execution snapshot for SuspendResume recovery."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from framework.control.checkpoint import CheckpointStore


@dataclass
class TurnResumeState:
    """Snapshot captured when ToolNode suspends for approval."""
    iteration: int
    tool_calls: list[dict[str, Any]]
    tool_decisions: list[str]
    all_new_messages: list[dict[str, Any]]


class TurnResumeStateStore(ABC):
    """Persistence abstraction for TurnResumeState."""

    @abstractmethod
    async def save(self, session_id: str, state: TurnResumeState) -> None: ...

    @abstractmethod
    async def load(self, session_id: str) -> TurnResumeState | None: ...

    @abstractmethod
    async def delete(self, session_id: str) -> None: ...


class InMemoryTurnResumeStateStore(TurnResumeStateStore):
    """In-memory store for testing."""

    def __init__(self) -> None:
        self._store: dict[str, TurnResumeState] = {}

    async def save(self, session_id: str, state: TurnResumeState) -> None:
        self._store[session_id] = state

    async def load(self, session_id: str) -> TurnResumeState | None:
        return self._store.get(session_id)

    async def delete(self, session_id: str) -> None:
        self._store.pop(session_id, None)


class StateStoreTurnResumeStateStore(TurnResumeStateStore):
    """Wraps existing CheckpointStore for durable persistence."""

    def __init__(self, checkpoint_store: CheckpointStore) -> None:
        self._store = checkpoint_store

    def _key(self, session_id: str) -> str:
        return f"{session_id}:turn_resume"

    async def save(self, session_id: str, state: TurnResumeState) -> None:
        await self._store.save(self._key(session_id), {
            "iteration": state.iteration,
            "tool_calls": state.tool_calls,
            "tool_decisions": state.tool_decisions,
            "all_new_messages": state.all_new_messages,
        })

    async def load(self, session_id: str) -> TurnResumeState | None:
        data = await self._store.load(self._key(session_id))
        if data is None:
            return None
        return TurnResumeState(
            iteration=data["iteration"],
            tool_calls=data["tool_calls"],
            tool_decisions=data["tool_decisions"],
            all_new_messages=data["all_new_messages"],
        )

    async def delete(self, session_id: str) -> None:
        await self._store.clear(self._key(session_id))
