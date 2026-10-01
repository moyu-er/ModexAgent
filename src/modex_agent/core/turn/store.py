"""Turn-state persistence contract — the store ABC and shared path transform.

Turn state IS turn vocabulary (W3b): the persistence adapters (SQLite) and
the runtime's default file/in-memory backends both implement
:class:`TurnStateStore`, so the ABC lives in ``core/turn`` — the one place
both sides may import. The concrete default backends stay in
:mod:`modex_agent.runtime.store`.

``safe_turn_segment`` owns the on-disk turn-state path transform: the file
backend's ``runtime_state/<pool>/turns/<seg_agent>/<seg_sid>`` layout and the
session-artifact cleaner must derive identical segments, so both call this
single function.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

from modex_agent.core.turn.models import StateQueryScope, TurnIdentity, TurnSnapshot

__all__ = [
    "ActiveTurnConflictError",
    "TurnStateStore",
    "safe_turn_segment",
]

# Anything outside [A-Za-z0-9_-] is neutralized; a changed segment keeps a
# short digest so distinct raw ids never collapse onto one directory.
_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def safe_turn_segment(raw: str) -> str:
    """Sanitize one turn-state path segment (directory or file stem).

    Identical to the transform the JSON-file turn-state store has always
    used: unsafe characters become ``_`` and, when that changed the name, the
    original is disambiguated with an 8-hex sha256 suffix.
    """
    sanitized = _SAFE_RE.sub("_", raw)
    if sanitized != raw:
        import hashlib

        digest = hashlib.sha256(raw.encode()).hexdigest()[:8]
        return f"{sanitized}--{digest}"
    return sanitized


class ActiveTurnConflictError(Exception):
    """Raised when a second active turn is saved for the same (agent_id, session_id)."""


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
