"""PoolRouter — session→pool dispatch.

/pool_name switching is handled by the input pipeline
(``EnvironmentControlStage``) before messages reach the queue.
PoolRouter routes every message to the pool recorded in
PoolSessionStore (set by ResolvePoolStage / UI callbacks).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from modex_agent.core.session_id import session_id_prefix_of
from modex_agent.core.session_store import safe_filename
from modex_agent.core.types import InputMessage
from modex_agent.pipeline.adapters import InputAdapter

logger = logging.getLogger(__name__)


class PoolSessionStore:
    """Persists session_id → pool_name mapping to disk as JSON files.

    One file per session: pool_sessions/{session_id}.json
    """

    def __init__(self, data_dir: Path):
        self._dir = Path(data_dir) / "pool_sessions"
        self._dir.mkdir(parents=True, exist_ok=True)

    def _file(self, session_id: str) -> Path:
        return self._dir / f"{safe_filename(session_id)}.json"

    def get(self, session_id: str, default: str) -> str:
        fp = self._file(session_id)
        if not fp.exists():
            return default
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            return data.get("pool", default)
        except Exception:
            return default

    def set(self, session_id: str, pool_name: str) -> None:
        fp = self._file(session_id)
        fp.write_text(
            json.dumps({"pool": pool_name, "session_id": session_id}),
            encoding="utf-8",
        )

    def rename_pool(self, old_pool: str, new_pool: str) -> int:
        """Rewrite every stored session->pool mapping from ``old_pool`` to ``new_pool``.

        Returns the number of records rewritten. Corrupt/empty files are skipped.
        """
        count = 0
        for file in self._dir.glob("*.json"):
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("pool") == old_pool:
                data["pool"] = new_pool
                tmp = file.with_suffix(".tmp")
                tmp.write_text(json.dumps(data), encoding="utf-8")
                os.replace(tmp, file)
                count += 1
        return count


class PoolRouter:
    """Routes incoming messages to the correct pool.

    Pool is determined from ``PoolSessionStore`` (set by
    ``ResolvePoolStage`` / UI callbacks).  /pool_name switching is
    handled upstream by the input pipeline (``EnvironmentControlStage``).

    Zero hardcoded pool names — dispatch is ``pools.get(name)``.
    """

    def __init__(
        self,
        input_adapter: InputAdapter,
        broker: Any,
        pools: dict[str, Any],
        session_store: PoolSessionStore,
        default_pool: str,
    ):
        self._input_adapter = input_adapter
        self._broker = broker
        self._pools = pools
        self._session_store = session_store
        self._default_pool = default_pool

    async def run(self) -> None:
        async for msg in self._input_adapter.receive():
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
        target = self._session_store.get(session_prefix, self._default_pool)
        pool = self._pools.get(target)
        if pool is None:
            pool = self._pools[self._default_pool]
        await self._route_to_pool(msg, pool)

    def set_pool(self, session_id: str, pool_name: str) -> None:
        """Set pool routing for a session without sending a notification.

        Used by WebUI to switch pools via UI selector (not slash commands).
        Accepts the full session id (as WebUI attaches); keys the store by the
        agent-independent prefix so routing is stable across pool switches.
        """
        session_prefix = session_id_prefix_of(session_id)
        self._session_store.set(session_prefix, pool_name)
        logger.info("Session %s pool set to '%s' (external)", session_id, pool_name)

    async def _route_to_pool(self, msg: InputMessage, pool: Any) -> None:
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
            ),
        )
