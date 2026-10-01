"""Per-session Todo values and the persistence contract (TodoStore ABC).

The JSON-file implementation lives with the store adapters
(:class:`~modex_agent.persistence.adapters.todo_store.JsonFileTodoStore`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class TodoStatus(StrEnum):
    """Status of a todo item in a session task list."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TodoItem(BaseModel):
    """A single task-list entry. Order is conveyed by list position (no id)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str
    status: TodoStatus

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TodoItem:
        return cls.model_validate(data)


class TodoStore(ABC):
    """Per-session task list persistence. Session-scoped; pool-isolated by base_dir."""

    @abstractmethod
    async def save(self, session_id: str, todos: list[TodoItem]) -> None: ...

    @abstractmethod
    async def get(self, session_id: str) -> list[TodoItem]: ...

    @abstractmethod
    async def delete(self, session_id: str) -> None: ...


__all__ = ["TodoItem", "TodoStatus", "TodoStore"]
