from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator

from .broker import Address, BrokerMessage, MessageBroker

_SENTINEL = object()
logger = logging.getLogger(__name__)


class InMemoryMessageBroker(MessageBroker):
    """基于内存 asyncio.Queue 的 MessageBroker 实现。"""

    def __init__(self) -> None:
        self._mailboxes: dict[Address, asyncio.Queue[BrokerMessage]] = {}
        self._consumers: set[Address] = set()
        self._topic_subscriptions: dict[str, set[Address]] = {}
        self._running = False

    def _ensure_mailbox(self, address: Address) -> asyncio.Queue[BrokerMessage]:
        if address not in self._mailboxes:
            self._mailboxes[address] = asyncio.Queue()
        return self._mailboxes[address]

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False
        for q in list(self._mailboxes.values()):
            await q.put(_SENTINEL)  # type: ignore[arg-type]

    async def send_to(self, recipient: Address, message: BrokerMessage) -> None:
        if recipient not in self._consumers:
            logger.debug(
                "Sending to address %s with no registered consumer", recipient
            )
        await self._ensure_mailbox(recipient).put(message)

    async def publish(self, topic: str, message: BrokerMessage) -> None:
        subscribers = self._topic_subscriptions.get(topic)
        if not subscribers:
            return
        for addr in subscribers:
            await self._ensure_mailbox(addr).put(message)

    async def broadcast(self, message: BrokerMessage) -> None:
        targets = list(self._mailboxes.keys())
        for addr in targets:
            await self._ensure_mailbox(addr).put(message)

    async def register_consumer(self, address: Address) -> None:
        self._ensure_mailbox(address)
        self._consumers.add(address)

    async def unregister_consumer(self, address: Address) -> None:
        self._mailboxes.pop(address, None)
        self._consumers.discard(address)
        for subs in self._topic_subscriptions.values():
            subs.discard(address)

    async def consume(self, address: Address) -> BrokerMessage | None:
        msg = await self._ensure_mailbox(address).get()
        if msg is _SENTINEL:
            return None
        return msg

    async def consume_stream(self, address: Address) -> AsyncIterator[BrokerMessage]:
        while self._running:
            msg = await self._ensure_mailbox(address).get()
            if msg is _SENTINEL:
                break
            yield msg  # type: ignore[misc]

    async def subscribe(self, topics: list[str]) -> AsyncIterator[BrokerMessage]:
        temp_address = Address(kind="_temp", name=str(uuid.uuid4()))
        await self.register_consumer(temp_address)
        try:
            for topic in topics:
                self._topic_subscriptions.setdefault(topic, set()).add(temp_address)
            async for msg in self.consume_stream(temp_address):
                yield msg
        finally:
            for topic in topics:
                self._topic_subscriptions.get(topic, set()).discard(temp_address)
            await self.unregister_consumer(temp_address)
