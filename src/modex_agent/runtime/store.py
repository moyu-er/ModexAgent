"""Turn state stores — default implementations.

The :class:`~modex_agent.core.turn.store.TurnStateStore` ABC,
:class:`ActiveTurnConflictError`, and the shared path transform
(:func:`~modex_agent.core.turn.store.safe_turn_segment`) live in
``core/turn/store.py`` (W3b); this module keeps the framework's default
backends: no-op, in-memory, and the one-JSON-file-per-turn store.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from modex_agent.core.session_id import SessionInfo
from modex_agent.core.turn.codec import RuntimeStateCodecRegistry
from modex_agent.core.turn.enums import AgentKind, TurnPhase
from modex_agent.core.turn.models import StateQueryScope, TurnIdentity, TurnSnapshot
from modex_agent.core.turn.store import (
    ActiveTurnConflictError,
    TurnStateStore,
    safe_turn_segment,
)
from modex_agent.utils.file_io import read_json_robust

logger = logging.getLogger(__name__)

_ACTIVE_PHASES = {TurnPhase.RUNNING, TurnPhase.SUSPENDED}


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
        return not (scope.created_before is not None and snapshot.created_at >= scope.created_before)


class JsonFileTurnStateStore(TurnStateStore):
    """Default file backend — one JSON file per turn snapshot."""

    def __init__(self, workspace: Path, codec_registry: RuntimeStateCodecRegistry) -> None:
        self._workspace = workspace
        self._workspace.mkdir(parents=True, exist_ok=True)
        self._codec_registry = codec_registry

    # ---- path helpers ----

    def _dir(self, identity: TurnIdentity) -> Path:
        return (
            self._workspace
            / safe_turn_segment(identity.agent_id)
            / safe_turn_segment(str(identity.session))
        )

    def _path(self, identity: TurnIdentity) -> Path:
        return self._dir(identity) / f"{safe_turn_segment(identity.turn_id)}.json"

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
        agent_kind = AgentKind(agent_kind_raw)
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
            agent_kind = AgentKind(agent_kind_raw)
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
        return not (scope.created_before is not None and snapshot.created_at >= scope.created_before)
