"""PoolRouter — session→pool dispatch.

/pool_name switching is handled by the input pipeline
(``EnvironmentControlStage``) before messages reach the queue.
PoolRouter routes every message to the pool recorded in a
PoolRoutingStore (set by ResolvePoolStage / UI callbacks). Agent→pool
ownership is compile-time declaration knowledge
(:func:`agent_pool_ownership`) — one lookup, never an all-pools scan.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, ValidationError

from modex_agent.core.session_id import session_id_prefix_of
from modex_agent.core.session_store import safe_filename
from modex_agent.core.types import InputMessage
from modex_agent.messaging.broker import MessageBroker
from modex_agent.multi_agent.pool_instance import PoolInstance
from modex_agent.pipeline.adapters import InputAdapter

if TYPE_CHECKING:
    from modex_agent.scope.spec import ScopeSpec

logger = logging.getLogger(__name__)


def agent_pool_ownership(spec: ScopeSpec) -> dict[str, tuple[str, ...]]:
    """agent name → the pools declaring it, in declaration order.

    The declaration lookup table the router reconciles against: agent→pool
    ownership is compile-time knowledge (SPEC §5.3 — the declaration
    replaces the runtime all-pools scan). Agent names are unique within a
    pool (V11) but MAY repeat across pools (e.g. a shared subagent name);
    the value keeps every declaring pool so the router can tell "the
    routed pool owns this agent" from "another pool does", and the first
    entry is the deterministic re-route target for repeated names.
    """
    if spec.pool is not None:
        pools = [spec.pool]
    elif spec.workspace is not None:
        pools = list(spec.workspace.pools)
    else:
        pools = []
    ownership: dict[str, list[str]] = {}
    for pool in pools:
        for agent in pool.agents:
            ownership.setdefault(agent.name, []).append(pool.name)
    return {agent: tuple(owners) for agent, owners in ownership.items()}


class PoolRoutingStore(ABC):
    """Persistence interface for session-prefix to pool routing."""

    @abstractmethod
    def get_pool(self, session_prefix: str) -> str | None:
        """Return the routed pool, or ``None`` when no route exists."""
        ...

    @abstractmethod
    def set_pool(self, session_prefix: str, pool_name: str) -> None:
        """Persist the pool route for a session prefix."""
        ...

    @abstractmethod
    def delete_pool(self, session_prefix: str) -> None:
        """Delete the route for a session prefix when present."""
        ...

    @abstractmethod
    def list_prefixes(self) -> list[str]:
        """Return all stored session prefixes in deterministic order."""
        ...

    @abstractmethod
    def delete_pool_routes(self, pool_name: str) -> int:
        """Delete all routes pointing to *pool_name*. Returns count deleted."""
        ...

    def get(self, session_prefix: str, default: str | None = None) -> str | None:
        """Convenience alias: ``get_pool`` with a default fallback."""
        return self.get_pool(session_prefix) or default

    def set(self, session_prefix: str, pool_name: str) -> None:
        """Convenience alias: delegate to ``set_pool``."""
        self.set_pool(session_prefix, pool_name)

    def close(self) -> None:  # noqa: B027 - no-op default; resource-owning stores override
        """Release resources owned by this store.

        The no-op default suits the file-backed routing stores, which own no
        dedicated resources. Stores that own real resources (a shared SQLite
        connection; an OTEL_HTTP trace store's sender thread + OTLP client)
        override this and must be closed at teardown.
        """
        return None


class _PoolRoutingRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    pool: str
    session_id: str


class LocalFilePoolRoutingStore(PoolRoutingStore):
    """Persists session_id → pool_name mapping to disk as JSON files.

    One file per session: pool_sessions/{session_id}.json
    """

    def __init__(self, data_dir: Path) -> None:
        self._dir = Path(data_dir) / "pool_sessions"
        self._dir.mkdir(parents=True, exist_ok=True)

    def _file(self, session_prefix: str) -> Path:
        return self._dir / f"{safe_filename(session_prefix)}.json"

    def get_pool(self, session_prefix: str) -> str | None:
        fp = self._file(session_prefix)
        if not fp.exists():
            return None
        record = _PoolRoutingRecord.model_validate_json(fp.read_text(encoding="utf-8"))
        return record.pool

    def set_pool(self, session_prefix: str, pool_name: str) -> None:
        record = _PoolRoutingRecord(pool=pool_name, session_id=session_prefix)
        self._file(session_prefix).write_text(record.model_dump_json(), encoding="utf-8")

    def delete_pool(self, session_prefix: str) -> None:
        self._file(session_prefix).unlink(missing_ok=True)

    def list_prefixes(self) -> list[str]:
        records = (
            _PoolRoutingRecord.model_validate_json(file.read_text(encoding="utf-8"))
            for file in self._dir.glob("*.json")
        )
        return sorted(record.session_id for record in records)

    def delete_pool_routes(self, pool_name: str) -> int:
        count = 0
        for file in self._dir.glob("*.json"):
            try:
                record = _PoolRoutingRecord.model_validate_json(file.read_text(encoding="utf-8"))
            except (OSError, ValidationError):
                continue
            if record.pool == pool_name:
                file.unlink(missing_ok=True)
                count += 1
        return count


class PoolRouter:
    """Routes incoming messages to the correct pool.

    Pool is determined from ``PoolRoutingStore`` (set by
    ``ResolvePoolStage`` / UI callbacks).  /pool_name switching is
    handled upstream by the input pipeline (``EnvironmentControlStage``).

    Zero hardcoded pool names — dispatch is ``pools.get(name)``. When a
    message's session names an agent, the declaration lookup
    (``agent_pool_ownership``) reconciles the routed pool with the
    agent's owning pool; an agent no pool declares is dropped loudly
    (addressing constitution — never routed on to produce an orphan).
    """

    def __init__(
        self,
        input_adapter: InputAdapter,
        broker: MessageBroker,
        pools: dict[str, PoolInstance],
        session_store: PoolRoutingStore,
        default_pool: str | None,
        *,
        agent_pool_ownership: Mapping[str, tuple[str, ...]],
    ) -> None:
        self._input_adapter = input_adapter
        self._broker = broker
        self._pools = pools
        self._session_store = session_store
        self._default_pool = default_pool
        self._agent_pool_ownership = agent_pool_ownership

    async def run(self) -> None:
        async for msg in self._input_adapter.receive():  # type: ignore[attr-defined]  # mypy false-positive: abstract receive() is async-generator at runtime
            await self.route_message(msg)

    async def route_message(self, msg: InputMessage) -> None:
        """Route a single already-received message to its pool.

        Used by the per-workspace dispatcher, which reads the shared
        InputAdapter itself and calls this once per message after binding the
        workspace root for the turn.
        """
        # Pool store keys by the agent-independent prefix so routing is
        # stable across pool switches.
        session_prefix = msg.session.session_id_prefix
        target = self._session_store.get_pool(session_prefix) or self._default_pool
        if target is None or target not in self._pools:
            logger.error(
                "No routable pool for session %s (target=%s)",
                session_prefix,
                target,
            )
            return
        agent_name = msg.session.agent_name
        if agent_name:
            # The routing store is NOT mutated here — per ADR-0019 the store
            # is the routing authority, maintained by the pool-switch write
            # path; the router only corrects the per-message routing decision.
            owners = self._agent_pool_ownership.get(agent_name, ())
            if target not in owners:
                owner = next(
                    (name for name in owners if name in self._pools), None
                )
                if owner is None:
                    logger.error(
                        "No pool serves agent '%s' for session %s; dropping "
                        "message (declaration lookup miss — routing to '%s' "
                        "would produce an orphan).",
                        agent_name,
                        session_prefix,
                        target,
                    )
                    return
                logger.warning(
                    "Re-routing session %s from pool '%s' to pool '%s' "
                    "(agent '%s' is declared in '%s').",
                    session_prefix,
                    target,
                    owner,
                    agent_name,
                    owner,
                )
                target = owner
        pool = self._pools[target]
        await self._route_to_pool(msg, pool)

    def set_pool(self, session_id: str, pool_name: str) -> None:
        """Set pool routing for a session without sending a notification.

        Used by WebUI to switch pools via UI selector (not slash commands).
        Accepts the full session id (as WebUI attaches); keys the store by the
        agent-independent prefix so routing is stable across pool switches.
        """
        session_prefix = session_id_prefix_of(session_id)
        self._session_store.set_pool(session_prefix, pool_name)
        logger.info("Session %s pool set to '%s' (external)", session_id, pool_name)

    async def _route_to_pool(self, msg: InputMessage, pool: PoolInstance) -> None:
        """Route a message to its pool via submit_input.

        Poll-driven cutover (Task 8): DMs are written as ``external_input``
        envelopes to this pool's inbox (``submit_input``) and stay pending for
        the next between-turn — the InboxPoller, not a Drainer, starts the turn.
        """
        sid = str(msg.session)
        metadata = dict(msg.metadata) if msg.metadata else {}
        metadata.setdefault("session_id", msg.session.session_id_prefix)
        metadata.setdefault("sender_id", msg.sender_id)
        metadata.setdefault("chat_id", msg.chat_id)
        metadata.setdefault("channel", msg.channel)
        await pool.pool.submit_input(
            sid,
            msg.model_copy(update={"metadata": metadata}),
        )


PoolSessionStore = LocalFilePoolRoutingStore
