"""Per-session Todo values and persistence contracts."""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from modex_agent.utils.file_io import read_json_robust


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


class JsonFileTodoStore(TodoStore):
    """One JSON file per session: ``<base_dir>/<session_id>.json``.

    ``base_dir`` is injected by the caller (pool-aware in production; a tmp dir
    in tests). Atomic write via tmp + os.replace.

    ``_safe_segment`` only neutralizes characters that are genuinely unsafe on
    common filesystems (``/``, ``\\``, ``:``, ``*``, ``?``, ``"``, ``<``, ``>``,
    ``|``). Session ids in this system are ``{prefix}.{agent}[.{invocation_id}]``,
    so the resulting filename is essentially the session id plus ``.json``.
    """

    _SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._base_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def _safe_segment(cls, raw: str) -> str:
        return cls._SAFE_RE.sub("_", raw)

    def _path(self, session_id: str) -> Path:
        return self._base_dir / f"{self._safe_segment(session_id)}.json"

    async def save(self, session_id: str, todos: list[TodoItem]) -> None:
        payload = [todo.to_dict() for todo in todos]
        target = self._path(session_id)
        tmp = target.with_suffix(target.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, target)
        except Exception:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            raise

    async def get(self, session_id: str) -> list[TodoItem]:
        data = read_json_robust(self._path(session_id))
        if not isinstance(data, list):
            return []
        items: list[TodoItem] = []
        for entry in data:
            if isinstance(entry, dict):
                try:
                    items.append(TodoItem.from_dict(entry))
                except (KeyError, ValueError):
                    continue
        return items

    async def delete(self, session_id: str) -> None:
        path = self._path(session_id)
        if path.exists():
            path.unlink()


__all__ = ["JsonFileTodoStore", "TodoItem", "TodoStatus", "TodoStore"]
