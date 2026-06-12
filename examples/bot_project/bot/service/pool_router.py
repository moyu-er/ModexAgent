"""PoolRouter — session→pool dispatch with /pool_name switching."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from framework.core.types import InputMessage, OutputMessage
from framework.messaging.broker import BrokerMessage
from framework.multi_agent.address import AgentAddress
from framework.pipeline.adapters import InputAdapter, OutputAdapter

logger = logging.getLogger(__name__)


class PoolSessionStore:
    """Persists session_id → pool_name mapping to disk as JSON files.

    One file per conversation: data/pool_sessions/{conversation_id}.json
    """

    def __init__(self, data_dir: Path):
        self._dir = Path(data_dir) / "pool_sessions"
        self._dir.mkdir(parents=True, exist_ok=True)

    def _file(self, session_id: str) -> Path:
        safe = session_id.replace("/", "_").replace("\\", "_").replace(":", "_")
        return self._dir / f"{safe}.json"

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

    Only /pool_name (exact single-word match) is intercepted for switching.
    Everything else passes through to the session's current pool unchanged.

    Zero hardcoded pool names — dispatch is ``pools.get(name)``.
    """

    POOL_COMMAND_RE = re.compile(r"^/([a-z][a-z0-9_-]*)$")

    def __init__(
        self,
        input_adapter: InputAdapter,
        output_adapter: OutputAdapter,
        broker: Any,
        pools: dict[str, Any],
        session_store: PoolSessionStore,
        default_pool: str,
    ):
        self._input_adapter = input_adapter
        self._output_adapter = output_adapter
        self._broker = broker
        self._pools = pools
        self._session_store = session_store
        self._default_pool = default_pool

    async def run(self) -> None:
        async for msg in self._input_adapter.receive():
            pool_name = self._extract_pool_command(msg.content)
            if pool_name is not None:
                await self._handle_switch(msg.session_id, pool_name)
                continue
            target = self._session_store.get(msg.session_id, self._default_pool)
            pool = self._pools.get(target)
            if pool is None:
                pool = self._pools[self._default_pool]
            await self._route_to_pool(msg, pool)

    def _extract_pool_command(self, content: str | None) -> str | None:
        if not content:
            return None
        m = self.POOL_COMMAND_RE.match(content.strip())
        if m and m.group(1) in self._pools:
            return m.group(1)
        return None

    async def _handle_switch(self, session_id: str, pool_name: str) -> None:
        self._session_store.set(session_id, pool_name)
        await self._output_adapter.send(
            OutputMessage(content=f'switch to "{pool_name}" pool'),
            session_id,
        )
        logger.info("Session %s switched to pool '%s'", session_id, pool_name)

    def set_pool(self, session_id: str, pool_name: str) -> None:
        """Set pool routing for a session without sending a notification.

        Used by WebUI to switch pools via UI selector (not slash commands).
        """
        self._session_store.set(session_id, pool_name)
        logger.info("Session %s pool set to '%s' (external)", session_id, pool_name)

    async def _route_to_pool(self, msg: InputMessage, pool: Any) -> None:
        metadata = dict(msg.metadata) if msg.metadata else {}
        metadata.setdefault("conversation_id", msg.session_id)
        broker_msg = BrokerMessage(
            payload={
                "content": msg.content,
                "session_id": msg.session_id,
                "metadata": metadata,
                "sender_id": msg.sender_id,
                "chat_id": msg.chat_id,
                "conversation_id": msg.session_id,
            },
            sender=AgentAddress(kind="channel", name=msg.source or "unknown"),
            recipient=AgentAddress(kind="agent", name=pool.main_agent_name),
            headers={
                "channel": msg.channel or "",
                "chat_id": msg.chat_id or "",
                "conversation_id": msg.session_id,
            },
        )
        await self._broker.send_to(pool.main_address, broker_msg)
