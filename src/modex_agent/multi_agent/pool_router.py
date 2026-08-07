"""PoolRouter — session→pool dispatch.

/pool_name switching is handled by the input pipeline
(``EnvironmentControlStage``) before messages reach the queue.
PoolRouter routes every message to the pool recorded in a
PoolRoutingStore (set by ResolvePoolStage / UI callbacks).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from modex_agent.core.session_id import session_id_prefix_of
from modex_agent.core.session_store import safe_filename
from modex_agent.core.types import InputMessage
from modex_agent.messaging.broker import MessageBroker
from modex_agent.multi_agent.pool_instance import PoolInstance
from modex_agent.pipeline.adapters import InputAdapter

logger = logging.getLogger(__name__)


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

    def close(self) -> None:  # noqa: B027 - file-backed stores own no resources
        """Release resources owned by this store."""
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

    Zero hardcoded pool names — dispatch is ``pools.get(name)``.
    """

    def __init__(
        self,
        input_adapter: InputAdapter,
        broker: MessageBroker,
        pools: dict[str, PoolInstance],
        session_store: PoolRoutingStore,
        default_pool: str | None,
    ) -> None:
        self._input_adapter = input_adapter
        self._broker = broker
        self._pools = pools
        self._session_store = session_store
        self._default_pool = default_pool

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
        target = self._reconcile_pool_for_agent(target, msg)
        pool = self._pools[target]
        await self._route_to_pool(msg, pool)

    def _reconcile_pool_for_agent(self, target: str, msg: InputMessage) -> str:
        """Reconcile the routed pool with the message's agent_name.

        ``session_id`` encodes an agent_name (e.g. ``conv1.orchestrator``).
        When a user switches pools via WebUI the routing store updates to the
        new pool, but a message already carrying the old pool's agent_name
        would land in the wrong pool — creating an orphan that the poller
        can never consume (``no template for X; skipping`` forever).

        If the target pool does not serve ``msg.session.agent_name``, search
        all pools for one that does and re-route there. The routing store is
        NOT mutated — per ADR-0019 the store is the routing authority,
        maintained by the pool-switch write path; the router only corrects
        the per-message routing decision.
        """
        agent_name = msg.session.agent_name
        if not agent_name:
            return target
        if self._pools[target].pool.serves_agent(agent_name):
            return target
        for name, instance in self._pools.items():
            if name == target:
                continue
            if instance.pool.serves_agent(agent_name):
                logger.warning(
                    "Re-routing session %s from pool '%s' to pool '%s' "
                    "(agent '%s' is not served by '%s').",
                    msg.session.session_id_prefix,
                    target,
                    name,
                    agent_name,
                    target,
                )
                return name
        logger.error(
            "No pool serves agent '%s' for session %s; routing to '%s' "
            "will produce an orphan message.",
            agent_name,
            msg.session.session_id_prefix,
            target,
        )
        return target

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
            InputMessage(
                content=msg.content,
                session=msg.session,
                metadata=metadata,
                sender_id=msg.sender_id,
                chat_id=msg.chat_id,
                approval_decision=msg.approval_decision,
                attachments_resolved=msg.attachments_resolved,
                workspace=msg.workspace,
            ),
        )


PoolSessionStore = LocalFilePoolRoutingStore
