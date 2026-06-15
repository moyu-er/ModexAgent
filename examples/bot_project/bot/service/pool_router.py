"""PoolRouter — session→pool dispatch.

/pool_name switching is handled by the input pipeline
(``EnvironmentControlStage``) before messages reach the queue.
PoolRouter routes every message to the pool recorded in
PoolSessionStore (set by ResolvePoolStage / UI callbacks).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from framework.core.session_id import snowflake_of
from framework.core.session_store import safe_filename
from framework.core.types import InputMessage
from framework.messaging.broker import BrokerMessage
from framework.multi_agent.address import AgentAddress
from framework.pipeline.adapters import InputAdapter

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
            # Pool store keys by the agent-independent snowflake so routing is
            # stable across pool switches.
            snowflake = msg.session.snowflake
            target = self._session_store.get(snowflake, self._default_pool)
            pool = self._pools.get(target)
            if pool is None:
                pool = self._pools[self._default_pool]
            await self._route_to_pool(msg, pool)

    def set_pool(self, session_id: str, pool_name: str) -> None:
        """Set pool routing for a session without sending a notification.

        Used by WebUI to switch pools via UI selector (not slash commands).
        Accepts the full session id (as WebUI attaches); keys the store by the
        agent-independent snowflake so routing is stable across pool switches.
        """
        snowflake = snowflake_of(session_id)
        self._session_store.set(snowflake, pool_name)
        logger.info("Session %s pool set to '%s' (external)", session_id, pool_name)

    async def _route_to_pool(self, msg: InputMessage, pool: Any) -> None:
        sid = str(msg.session)
        conv_id = msg.session.snowflake
        metadata = dict(msg.metadata) if msg.metadata else {}
        metadata.setdefault("conversation_id", conv_id)
        broker_msg = BrokerMessage(
            payload={
                "content": msg.content,
                "session_id": sid,
                "metadata": metadata,
                "sender_id": msg.sender_id,
                "chat_id": msg.chat_id,
                "conversation_id": conv_id,
                "agent_session_id": sid,
            },
            sender=AgentAddress(kind="channel", name=msg.source or "unknown"),
            recipient=AgentAddress(kind="agent", name=pool.main_agent_name),
            headers={
                "channel": msg.channel or "",
                "chat_id": msg.chat_id or "",
                "conversation_id": conv_id,
                "agent_session_id": sid,
            },
        )
        await self._broker.send_to(pool.main_address, broker_msg)
