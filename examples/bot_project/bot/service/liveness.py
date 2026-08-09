"""Session liveness detection + deletion reservation (ADR-0018 TOCTOU gate).

Provides dual-signal liveness checking (durable turn-state store + in-process
turn-session registry) and a deletion reservation mechanism for TOCTOU
mitigation. The ``SessionGarbageCollector`` consults this before deleting a
session so an in-flight turn is never garbage-collected mid-flight.

TOCTOU window this closes::

    is_session_active() → False
        ── (turn starts here) ──
    delete_session_tree()

The reservation (``try_reserve_deletion``) is acquired BEFORE the liveness
check and released AFTER deletion completes. New-turn admission (wired in a
follow-up task) must check the reservation before starting, closing the window
entirely.

Fail-safe policy: when the durable signal cannot be queried (store raises or
workspace cannot be resolved), ``is_session_active`` returns ``True`` (assume
active). A false positive only delays cleanup; a false negative deletes an
in-flight turn.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from pathlib import Path

from modex_agent.core.session_id import SessionInfo
from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry
from modex_agent.runtime.enums import TurnPhase
from modex_agent.runtime.models import StateQueryScope
from modex_agent.runtime.store import TurnStateStore

logger = logging.getLogger(__name__)

_ACTIVE_PHASES = frozenset({TurnPhase.RUNNING, TurnPhase.SUSPENDED})

TurnStoreResolver = Callable[[str, Path], Awaitable[TurnStateStore | None]]
RegistryResolver = Callable[[str, Path], TurnSessionRegistry | None]


class LivenessProvider(ABC):
    """Session liveness detection + deletion reservation contract.

    Deep module: three-method interface hiding workspace/pool resolution,
    dual-signal liveness checking, and in-process reservation bookkeeping.
    """

    @abstractmethod
    async def is_session_active(self, session_id: str, workspace_root: Path) -> bool:
        """True if the session has an active turn (durable OR in-process signal)."""
        ...

    @abstractmethod
    async def try_reserve_deletion(self, session_id: str, workspace_root: Path) -> bool:
        """Acquire a deletion reservation. True if newly acquired, False if already held."""
        ...

    @abstractmethod
    async def release_deletion(self, session_id: str) -> None:
        """Release a deletion reservation. No-op if not held."""
        ...


class DefaultLivenessProvider(LivenessProvider):
    """Dual-signal liveness: durable turn store + in-process registry.

    Dependencies are injected as resolver callbacks (mirroring the
    ``SessionGarbageCollector`` pattern) so the provider stays
    workspace-independent and testable without real workspace stacks.

    Args:
        turn_store_resolver: Async callback resolving the ``TurnStateStore``
            for a (session_id, workspace_root) pair. Returns ``None`` when the
            workspace has no turn store (FILE backend without SQLite, or the
            pool cannot be resolved).
        registry_resolver: Optional sync callback resolving the
            ``TurnSessionRegistry`` for a (session_id, workspace_root) pair.
            Returns ``None`` when no pipeline is running for that session.
    """

    def __init__(
        self,
        turn_store_resolver: TurnStoreResolver,
        registry_resolver: RegistryResolver | None = None,
    ) -> None:
        self._turn_store_resolver = turn_store_resolver
        self._registry_resolver = registry_resolver
        self._reserved: set[str] = set()

    async def is_session_active(self, session_id: str, workspace_root: Path) -> bool:
        agent_id = SessionInfo.from_str(session_id).agent_name

        durable_active = await self._check_durable(session_id, workspace_root, agent_id)
        if durable_active is not None:
            return durable_active

        return self._check_registry(session_id, workspace_root)

    async def _check_durable(
        self,
        session_id: str,
        workspace_root: Path,
        agent_id: str,
    ) -> bool | None:
        """Query the durable turn store.

        Returns ``True`` if an active turn is found, ``True`` if the store
        cannot be resolved or queried (fail-safe), or ``None`` if no active
        turn was found and the caller should fall through to the in-process
        registry signal.
        """
        try:
            store = await self._turn_store_resolver(session_id, workspace_root)
        except Exception:
            logger.warning(
                "liveness: turn store resolver raised for session %s; assuming active",
                session_id,
                exc_info=True,
            )
            return True

        if store is None:
            return True

        try:
            snapshots = await store.list_active_turns(
                StateQueryScope(agent_id=agent_id, session_id=session_id)
            )
        except Exception:
            logger.warning(
                "liveness: turn store query raised for session %s; assuming active",
                session_id,
                exc_info=True,
            )
            return True

        return any(snap.phase in _ACTIVE_PHASES for snap in snapshots) or None

    def _check_registry(self, session_id: str, workspace_root: Path) -> bool:
        """Query the in-process turn-session registry signal."""
        resolver = self._registry_resolver
        if resolver is None:
            return False
        registry = resolver(session_id, workspace_root)
        if registry is None:
            return False
        return registry.is_active(session_id)

    async def try_reserve_deletion(self, session_id: str, workspace_root: Path) -> bool:
        if session_id in self._reserved:
            return False
        self._reserved.add(session_id)
        return True

    async def release_deletion(self, session_id: str) -> None:
        self._reserved.discard(session_id)
