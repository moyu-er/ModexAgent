"""Turn state and command stores — ABCs and default implementations.

TurnStateStore: semantic turn-snapshot persistence.
RuntimeCommandStore: durable command queue (separate lifecycle from turns).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from abc import ABC, abstractmethod
from pathlib import Path

from framework.utils.file_io import read_json_robust

from .codec import RuntimeStateCodecRegistry
from .enums import OperationStatus, TurnPhase
from .models import ControlCommandState, StateQueryScope, TurnIdentity, TurnSnapshot
from framework.core.session_id import SessionId

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
                TurnIdentity(agent_id=agent_id, session=SessionId.from_str(session_id), turn_id="_")
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
# RuntimeCommandStore
# ===========================================================================


class RuntimeCommandStore(ABC):
    """Durable command queue — separate lifecycle from turn snapshots."""

    @abstractmethod
    async def save_command(self, command: ControlCommandState) -> None: ...

    @abstractmethod
    async def load_pending_commands(self, scope: StateQueryScope) -> list[ControlCommandState]: ...

    @abstractmethod
    async def mark_command_applied(self, command_id: str) -> None: ...


class NoOpRuntimeCommandStore(RuntimeCommandStore):
    """No-op store for clean mode."""

    async def save_command(self, command: ControlCommandState) -> None:
        return

    async def load_pending_commands(self, scope: StateQueryScope) -> list[ControlCommandState]:
        return []

    async def mark_command_applied(self, command_id: str) -> None:
        return


class InMemoryRuntimeCommandStore(RuntimeCommandStore):
    """In-memory command store for testing."""

    def __init__(self) -> None:
        self._store: dict[str, ControlCommandState] = {}

    async def save_command(self, command: ControlCommandState) -> None:
        self._store[command.command_id] = command

    async def load_pending_commands(self, scope: StateQueryScope) -> list[ControlCommandState]:
        result: list[ControlCommandState] = []
        for cmd in self._store.values():
            if cmd.status != OperationStatus.COMPLETED:
                if scope.agent_id is not None and cmd.agent_id != scope.agent_id:
                    continue
                if scope.session_id is not None and cmd.session_id != scope.session_id:
                    continue
                result.append(cmd)
        return result

    async def mark_command_applied(self, command_id: str) -> None:
        import time

        cmd = self._store.get(command_id)
        if cmd is not None:
            cmd.status = OperationStatus.COMPLETED
            cmd.applied_at = time.time()


class JsonFileRuntimeCommandStore(RuntimeCommandStore):
    """Default JSON-file backend for durable commands."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self._workspace.mkdir(parents=True, exist_ok=True)

    def _path(self, command_id: str) -> Path:
        safe = JsonFileTurnStateStore._safe_segment(command_id)
        return self._workspace / f"{safe}.json"

    async def save_command(self, command: ControlCommandState) -> None:
        data = {
            "command_id": command.command_id,
            "kind": command.kind.value,
            "agent_id": command.agent_id,
            "session_id": command.session_id,
            "payload": dict(command.payload),
            "status": command.status.value,
            "created_at": command.created_at,
            "applied_at": command.applied_at,
        }
        self._path(command.command_id).write_text(
            json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8"
        )

    async def load_pending_commands(self, scope: StateQueryScope) -> list[ControlCommandState]:
        result: list[ControlCommandState] = []
        for f in self._workspace.glob("*.json"):
            data = read_json_robust(f)
            if not data:
                continue
            try:
                if data.get("status") == "completed":
                    continue
                if scope.agent_id is not None and data.get("agent_id") != scope.agent_id:
                    continue
                if scope.session_id is not None and data.get("session_id") != scope.session_id:
                    continue
                from .enums import ControlCommandKind
                from .enums import OperationStatus as OS

                result.append(
                    ControlCommandState(
                        command_id=data["command_id"],
                        kind=ControlCommandKind(data["kind"]),
                        agent_id=data["agent_id"],
                        session_id=data.get("session_id"),
                        payload=data.get("payload", {}),
                        status=OS(data.get("status", "created")),
                        created_at=data.get("created_at", 0),
                        applied_at=data.get("applied_at"),
                    )
                )
            except Exception:
                logger.exception("Failed to load command from %s", f)
        return result

    async def mark_command_applied(self, command_id: str) -> None:
        import time

        path = self._path(command_id)
        data = read_json_robust(path)
        if data is not None:
            data["status"] = "completed"
            data["applied_at"] = time.time()
            path.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
