from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import TYPE_CHECKING, Any

from framework.messaging.broker import Address

from .envelope import AgentMessageEnvelope

if TYPE_CHECKING:
    from framework.messaging.broker import MessageBroker

logger = logging.getLogger(__name__)


class RPCTimeoutError(TimeoutError):
    """RPC 请求超时异常。"""


class RPCBroker:
    """基于 MessageBroker 的 RPC 封装层。"""

    def __init__(self, broker: MessageBroker):
        self._broker = broker
        self._pending: dict[str, asyncio.Future[AgentMessageEnvelope]] = {}
        self._pending_sources: dict[str, Any] = {}
        self._send_locks: dict[str, asyncio.Lock] = {}
        self._reply_address = Address(kind="rpc", name="replies")
        self._consumer_task: asyncio.Task | None = None

    def _get_send_lock(self, target: Address) -> asyncio.Lock:
        key = f"{target.kind}:{target.name}"
        return self._send_locks.setdefault(key, asyncio.Lock())

    async def start(self) -> None:
        """启动后台回复消费协程。"""
        await self._broker.register_consumer(self._reply_address)
        self._consumer_task = asyncio.create_task(self._consume_replies())

    async def stop(self) -> None:
        """停止后台消费协程并清理资源。"""
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._consumer_task
            self._consumer_task = None
        await self._broker.unregister_consumer(self._reply_address)

    async def _consume_replies(self) -> None:
        """持续消费回复地址的消息并分发给 pending future。"""
        try:
            async for msg in self._broker.consume_stream(self._reply_address):
                envelope = AgentMessageEnvelope.from_broker_message(msg)
                if envelope:
                    await self.on_reply(envelope)
        except asyncio.CancelledError:
            raise

    async def request(
        self,
        target: Address,
        envelope: AgentMessageEnvelope,
        timeout: float = 60.0,
    ) -> AgentMessageEnvelope:
        """发送 RPC 请求并等待响应。"""
        correlation_id = envelope.correlation_id or uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[correlation_id] = future
        self._pending_sources[correlation_id] = target

        async with self._get_send_lock(target):
            broker_msg = envelope.to_broker_message()
            broker_msg.correlation_id = correlation_id
            broker_msg.headers["reply_to"] = str(self._reply_address)
            await self._broker.send_to(target, broker_msg)

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            raise RPCTimeoutError(f"RPC request to {target} timed out after {timeout}s") from exc
        finally:
            self._pending.pop(correlation_id, None)
            self._pending_sources.pop(correlation_id, None)

    async def on_reply(self, envelope: AgentMessageEnvelope) -> None:
        """处理收到的 RPC 回复。"""
        future = self._pending.get(envelope.correlation_id)
        if future is None or future.done():
            return
        expected_source = self._pending_sources.get(envelope.correlation_id)
        if expected_source is not None and envelope.source != expected_source:
            logger.warning(
                "Reply from unexpected source: %s (expected %s)",
                envelope.source,
                expected_source,
            )
            return
        future.set_result(envelope)
