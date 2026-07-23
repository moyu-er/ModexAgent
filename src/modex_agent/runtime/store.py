"""Turn state stores — ABCs and default implementations.

TurnStateStore: semantic turn-snapshot persistence.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from modex_agent.core.types import TodoStatus
from modex_agent.utils.file_io import read_json_robust

from .codec import RuntimeStateCodecRegistry
from .enums import TurnPhase
from .models import StateQueryScope, TurnIdentity, TurnSnapshot
from modex_agent.core.session_id import SessionInfo

logger = logging.getLogger(__name__)

_ACTIVE_PHASES = {TurnPhase.RUNNING, TurnPhase.SUSPENDED}


class ActiveTurnConflictError(Exception):
    """Raised when a second active turn is saved for the same (agent_id, session_id)."""


# ===========================================================================
# TurnStateStore
# ===========================================================================


class TurnStateStore(ABC):
    """Semantic turn-snapshot persistence."""

    @abstractmethod
    async def save_turn(self, snapshot: TurnSnapshot) -> None: ...

    @abstractmethod
    async def load_turn(self, identity: TurnIdentity) -> TurnSnapshot | None: ...

    @abstractmethod
    async def delete_turn(self, identity: TurnIdentity) -> None: ...

    @abstractmethod
    async def list_active_turns(self, scope: StateQueryScope) -> list[TurnSnapshot]: ...


class NoOpTurnStateStore(TurnStateStore):
    """No-op store — used in clean mode or when persistence is disabled."""

    async def save_turn(self, snapshot: TurnSnapshot) -> None:
        return

    async def load_turn(self, identity: TurnIdentity) -> TurnSnapshot | None:
        return None

    async def delete_turn(self, identity: TurnIdentity) -> None:
        return

    async def list_active_turns(self, scope: StateQueryScope) -> list[TurnSnapshot]:
        return []


class InMemoryTurnStateStore(TurnStateStore):
    """In-memory store for testing."""

    def __init__(self) -> None:
        self._store: dict[str, TurnSnapshot] = {}

    @staticmethod
    def _key(identity: TurnIdentity) -> str:
        return f"{identity.agent_id}/{str(identity.session)}/{identity.turn_id}"

    async def save_turn(self, snapshot: TurnSnapshot) -> None:
        self._store[self._key(snapshot.identity)] = snapshot

    async def load_turn(self, identity: TurnIdentity) -> TurnSnapshot | None:
        return self._store.get(self._key(identity))

    async def delete_turn(self, identity: TurnIdentity) -> None:
        self._store.pop(self._key(identity), None)

    async def list_active_turns(self, scope: StateQueryScope) -> list[TurnSnapshot]:
        result: list[TurnSnapshot] = []
        for snap in self._store.values():
            if self._match_scope(snap, scope):
                result.append(snap)
        return result

    @staticmethod
    def _match_scope(snapshot: TurnSnapshot, scope: StateQueryScope) -> bool:
        if scope.agent_id is not None and snapshot.identity.agent_id != scope.agent_id:
            return False
        if scope.session_id is not None and str(snapshot.identity.session) != scope.session_id:
            return False
        if scope.agent_kind is not None and snapshot.agent_kind != scope.agent_kind:
            return False
        if scope.phase is not None and snapshot.phase != scope.phase:
            return False
        if scope.reason is not None and snapshot.reason != scope.reason:
            return False
        if scope.created_before is not None and snapshot.created_at >= scope.created_before:
            return False
        return True


class JsonFileTurnStateStore(TurnStateStore):
    """Default file backend — one JSON file per turn snapshot."""

    _SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")

    def __init__(self, workspace: Path, codec_registry: RuntimeStateCodecRegistry) -> None:
        self._workspace = workspace
        self._workspace.mkdir(parents=True, exist_ok=True)
        self._codec_registry = codec_registry

    # ---- path helpers ----

    @classmethod
    def _safe_segment(cls, raw: str) -> str:
        sanitized = cls._SAFE_RE.sub("_", raw)
        if sanitized != raw:
            digest = hashlib.sha256(raw.encode()).hexdigest()[:8]
            return f"{sanitized}--{digest}"
        return sanitized

    def _dir(self, identity: TurnIdentity) -> Path:
        return (
            self._workspace
            / self._safe_segment(identity.agent_id)
            / self._safe_segment(str(identity.session))
        )

    def _path(self, identity: TurnIdentity) -> Path:
        return self._dir(identity) / f"{self._safe_segment(identity.turn_id)}.json"

    # ---- store API ----

    async def save_turn(self, snapshot: TurnSnapshot) -> None:
        if snapshot.phase in _ACTIVE_PHASES:
            existing = await self._find_active_turn(
                snapshot.identity.agent_id, str(snapshot.identity.session)
            )
            if existing is not None and existing.identity.turn_id != snapshot.identity.turn_id:
                raise ActiveTurnConflictError(
                    f"Active turn already exists for agent={snapshot.identity.agent_id} "
                    f"session={str(snapshot.identity.session)}: "
                    f"existing={existing.identity.turn_id}, new={snapshot.identity.turn_id}"
                )

        codec = self._codec_registry.get(snapshot.agent_kind)
        payload = codec.encode_turn(snapshot)
        self._dir(snapshot.identity).mkdir(parents=True, exist_ok=True)
        self._path(snapshot.identity).write_text(
            json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8"
        )

    async def load_turn(self, identity: TurnIdentity) -> TurnSnapshot | None:
        path = self._path(identity)
        data = read_json_robust(path)
        if not data:
            return None
        agent_kind_raw = data.get("agent_kind", "react")
        from .enums import AgentKind as AK

        agent_kind = AK(agent_kind_raw)
        codec = self._codec_registry.get(agent_kind)
        return codec.decode_turn(data)

    async def delete_turn(self, identity: TurnIdentity) -> None:
        path = self._path(identity)
        path.unlink(missing_ok=True)

    async def list_active_turns(self, scope: StateQueryScope) -> list[TurnSnapshot]:
        result: list[TurnSnapshot] = []
        agent_id = scope.agent_id
        session_id = scope.session_id

        if agent_id is not None and session_id is not None:
            dir_path = self._dir(
                TurnIdentity(agent_id=agent_id, session=SessionInfo.from_str(session_id), turn_id="_")
            )
            if dir_path.exists():
                for f in dir_path.glob("*.json"):
                    snap = await self._load_file(f)
                    if snap is not None and self._match_scope(snap, scope):
                        result.append(snap)
        else:
            for agent_dir in self._workspace.iterdir():
                if not agent_dir.is_dir():
                    continue
                for sess_dir in agent_dir.iterdir():
                    if not sess_dir.is_dir():
                        continue
                    for f in sess_dir.glob("*.json"):
                        snap = await self._load_file(f)
                        if snap is not None and self._match_scope(snap, scope):
                            result.append(snap)
        return result

    # ---- internal ----

    async def _find_active_turn(self, agent_id: str, session_id: str) -> TurnSnapshot | None:
        results = await self.list_active_turns(
            StateQueryScope(agent_id=agent_id, session_id=session_id)
        )
        for snap in results:
            if snap.phase in _ACTIVE_PHASES:
                return snap
        return None

    async def _load_file(self, path: Path) -> TurnSnapshot | None:
        data = read_json_robust(path)
        if not data:
            return None
        try:
            agent_kind_raw = data.get("agent_kind", "react")
            from .enums import AgentKind as AK

            agent_kind = AK(agent_kind_raw)
            codec = self._codec_registry.get(agent_kind)
            return codec.decode_turn(data)
        except Exception:
            logger.exception("Failed to load turn snapshot from %s", path)
            return None

    @staticmethod
    def _match_scope(snapshot: TurnSnapshot, scope: StateQueryScope) -> bool:
        if scope.agent_id is not None and snapshot.identity.agent_id != scope.agent_id:
            return False
        if scope.session_id is not None and str(snapshot.identity.session) != scope.session_id:
            return False
        if scope.agent_kind is not None and snapshot.agent_kind != scope.agent_kind:
            return False
        if scope.phase is not None and snapshot.phase != scope.phase:
            return False
        if scope.reason is not None and snapshot.reason != scope.reason:
            return False
        if scope.created_before is not None and snapshot.created_at >= scope.created_before:
            return False
        return True


# ===========================================================================
# TodoStore — per-session task list persistence
# ===========================================================================


class TodoItem(BaseModel):
    """A single task-list entry. Order is conveyed by list position (no id)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str
    status: TodoStatus

    def to_dict(self) -> dict[str, Any]:
        return {"content": self.content, "status": self.status.value}

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
        payload = [t.to_dict() for t in todos]
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
