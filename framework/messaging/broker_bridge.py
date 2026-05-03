from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from ..core.constants import DefaultValues
from ..core.types import InputMessage, OutputMessage
from ..pipeline.adapters import InputAdapter, OutputAdapter
from .broker import Address, BrokerMessage, MessageBroker

logger = logging.getLogger(__name__)


class BrokerInputAdapter(InputAdapter):
    """把 Broker 的某个 Address 包装为 InputAdapter。支持 AgentMessageEnvelope 识别与去重。"""

    def __init__(
        self,
        broker: MessageBroker,
        address: Address,
        deduplicator: Any | None = None,
    ):
        self.broker = broker
        self.address = address
        self._running = False
        self._deduplicator = deduplicator

    @property
    def name(self) -> str:
        return f"broker:{self.address}"

    async def start(self) -> None:
        self._running = True
        await self.broker.register_consumer(self.address)

    async def stop(self) -> None:
        self._running = False
        await self.broker.unregister_consumer(self.address)

    def receive(self) -> AsyncIterator[InputMessage]:
        async def _gen() -> AsyncIterator[InputMessage]:
            async for broker_msg in self.broker.consume_stream(self.address):
                if not self._running:
                    break
                msg = _broker_msg_to_input_message(broker_msg)
                # 去重检查
                message_id = broker_msg.headers.get("message_id") or broker_msg.payload.get("message_id")
                if message_id and self._deduplicator is not None and self._deduplicator.is_duplicate(message_id):
                    continue
                yield msg
        return _gen()


def _broker_msg_to_input_message(msg: BrokerMessage) -> InputMessage:
    payload = msg.payload
    sender = msg.sender
    metadata = dict(payload.get("metadata", {}))
    # 透传 AgentMessageEnvelope 路由字段到 metadata
    for key in ("conversation_id", "agent_session_id", "message_id", "in_reply_to", "message_type"):
        value = payload.get(key) or msg.headers.get(key)
        if value:
            metadata[key] = value

    session_id = payload.get("session_id", str(sender))

    # Orphan 隔离：来自 agent 的消息若缺失 conversation_id，隔离到 synthetic session
    if sender.kind == "agent":
        cid = payload.get("conversation_id") or msg.headers.get("conversation_id")
        if not cid:
            import logging
            import uuid

            logger = logging.getLogger(__name__)
            logger.warning("Orphan agent message from %s, isolating to synthetic session", sender)
            session_id = f"orphan:{sender.name}:{uuid.uuid4().hex[:8]}"

    return InputMessage(
        content=payload.get("content", ""),
        session_id=session_id,
        source=str(sender),
        sender_id=sender.name if sender.kind == "user" else payload.get("sender_id", DefaultValues.SENDER_ID),
        channel=msg.headers.get("channel", DefaultValues.CHANNEL),
        chat_id=msg.headers.get("chat_id", DefaultValues.CHAT_ID),
        metadata=metadata,
    )


class BrokerOutputAdapter(OutputAdapter):
    """把 OutputAdapter 的输出转发到 Broker。"""

    def __init__(
        self,
        broker: MessageBroker,
        sender: Address,
        default_recipient: Address | None = None,
        default_topic: str | None = None,
    ):
        if not default_recipient and not default_topic:
            raise ValueError("Must provide default_recipient or default_topic")
        self.broker = broker
        self.sender = sender
        self.default_recipient = default_recipient
        self.default_topic = default_topic

    @property
    def name(self) -> str:
        return f"broker:out:{self.sender}"

    @property
    def supports_streaming(self) -> bool:
        return False

    async def send(self, message: OutputMessage, session_id: str) -> None:
        metadata = dict(message.metadata) if message.metadata else {}
        broker_msg = BrokerMessage(
            payload={
                "content": message.content,
                "session_id": session_id,
                "metadata": metadata,
                "conversation_id": metadata.get("conversation_id", session_id),
                "agent_session_id": metadata.get("agent_session_id", session_id),
                "message_id": metadata.get("message_id", ""),
                "in_reply_to": metadata.get("in_reply_to", ""),
            },
            sender=self.sender,
            correlation_id=metadata.get("correlation_id", session_id),
            headers={
                "conversation_id": metadata.get("conversation_id", session_id),
                "agent_session_id": metadata.get("agent_session_id", session_id),
                "message_id": metadata.get("message_id", ""),
                "in_reply_to": metadata.get("in_reply_to", ""),
            },
        )
        if self.default_recipient:
            await self.broker.send_to(self.default_recipient, broker_msg)
        elif self.default_topic:
            await self.broker.publish(self.default_topic, broker_msg)

    async def send_delta(self, delta: str, session_id: str, metadata: dict | None = None) -> None:  # noqa: ARG002
        pass

    async def flush_deltas(self, session_id: str) -> None:  # noqa: ARG002
        pass


@dataclass
class OutputRoute:
    """动态输出路由规则。"""

    adapter: OutputAdapter
    match_kind: str | None = None      # 例如 "user"，匹配所有该 kind 的 Address
    match_topic: str | None = None     # 例如 "agent:outgoing"，订阅 topic 并路由


class BrokerBridgeService:
    """将原生 InputAdapter / OutputAdapter 桥接到 MessageBroker。

    input_bindings:  原生 Adapter -> 它对应的 Broker Address
    output_routes:   动态路由规则列表，解决静态 Address 绑定无法覆盖动态 user_id 的问题
    """

    def __init__(
        self,
        broker: MessageBroker,
        input_bindings: dict[InputAdapter, Address] | None = None,
        output_routes: list[OutputRoute] | None = None,
        *,
        send_timeout: float | None = None,
        restart_on_failure: bool = True,
        restart_max_retries: int = 5,
        restart_backoff_seconds: float = 5.0,
        restart_max_window_seconds: float = 300.0,
    ):
        self.broker = broker
        self.input_bindings = dict(input_bindings or {})
        self.output_routes = list(output_routes or [])
        self._send_timeout = send_timeout
        self._restart_on_failure = restart_on_failure
        self._restart_max_retries = restart_max_retries
        self._restart_backoff_seconds = restart_backoff_seconds
        self._restart_max_window_seconds = restart_max_window_seconds
        self._tasks: list[asyncio.Task] = []
        self._bridge_specs: dict[str, Callable[[], Coroutine[Any, Any, None]]] = {}
        self._restart_counts: dict[str, int] = {}
        self._restart_first_fail_time: dict[str, float] = {}

    def _bridge_done_callback(self, task: asyncio.Task, name: str) -> None:
        """Record exception from completed bridge task; optionally schedule restart."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Bridge task %s exited with error", name, exc_info=exc)
            if self._restart_on_failure:
                self._schedule_restart(name)

    def _schedule_restart(self, name: str) -> None:
        now = asyncio.get_event_loop().time()
        first_fail = self._restart_first_fail_time.get(name)
        if first_fail is None or (now - first_fail) > self._restart_max_window_seconds:
            self._restart_first_fail_time[name] = now
            self._restart_counts[name] = 0

        self._restart_counts[name] = self._restart_counts.get(name, 0) + 1
        if self._restart_counts[name] > self._restart_max_retries:
            logger.critical(
                "Bridge task %s exceeded max restarts (%d), giving up",
                name,
                self._restart_max_retries,
            )
            return

        delay = self._restart_backoff_seconds * (2 ** (self._restart_counts[name] - 1))
        logger.info(
            "Scheduling bridge task %s restart in %.1fs (attempt %d/%d)",
            name,
            delay,
            self._restart_counts[name],
            self._restart_max_retries,
        )
        asyncio.create_task(self._restart_bridge_after_delay(name, delay))

    async def _restart_bridge_after_delay(self, name: str, delay: float) -> None:
        await asyncio.sleep(delay)
        spec = self._bridge_specs.get(name)
        if spec is None:
            logger.warning("No bridge spec found for %s, cannot restart", name)
            return
        logger.info("Restarting bridge task %s", name)
        task = asyncio.create_task(spec())
        task.add_done_callback(lambda t, n=name: self._bridge_done_callback(t, n))
        self._tasks.append(task)

    async def start(self) -> None:
        await self.broker.start()
        for adapter, addr in self.input_bindings.items():
            await adapter.start()
            bridge_name = f"input:{getattr(adapter, 'name', addr)}"
            self._bridge_specs[bridge_name] = lambda a=adapter, ad=addr: self._bridge_input(a, ad)
            task = asyncio.create_task(self._bridge_specs[bridge_name]())
            task.add_done_callback(
                lambda t, n=bridge_name: self._bridge_done_callback(t, n)
            )
            self._tasks.append(task)
        for route in self.output_routes:
            if route.match_topic:
                bridge_name = f"output:{route.match_topic}"
                self._bridge_specs[bridge_name] = lambda r=route: self._bridge_output_topic(r)
                task = asyncio.create_task(self._bridge_specs[bridge_name]())
                task.add_done_callback(
                    lambda t, n=bridge_name: self._bridge_done_callback(t, n)
                )
                self._tasks.append(task)
            elif route.match_kind:
                raise NotImplementedError(
                    "match_kind routing is not yet supported. "
                    "Use match_topic instead (e.g., OutputRoute(adapter=..., match_topic='agent:outgoing'))."
                )

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._bridge_specs.clear()
        for adapter in self.input_bindings:
            await adapter.stop()
        await self.broker.stop()

    async def _bridge_input(self, adapter: InputAdapter, addr: Address) -> None:
        try:
            async for msg in adapter.receive():
                broker_msg = BrokerMessage(
                    payload={
                        "content": msg.content,
                        "session_id": msg.session_id,
                        "metadata": msg.metadata,
                        "sender_id": msg.sender_id,
                        "chat_id": msg.chat_id,
                        "conversation_id": msg.session_id,
                    },
                    sender=Address(kind="channel", name=msg.source or "unknown"),
                    recipient=addr,
                    headers={
                        "channel": msg.channel,
                        "chat_id": msg.chat_id,
                        "conversation_id": msg.session_id,
                    },
                )
                await self.broker.send_to(addr, broker_msg)
        except asyncio.CancelledError:
            pass

    async def _bridge_output_topic(self, route: OutputRoute) -> None:
        if not route.match_topic:
            return
        try:
            async for broker_msg in self.broker.subscribe([route.match_topic]):
                out_msg = OutputMessage(
                    content=broker_msg.payload.get("content", ""),
                    metadata=broker_msg.payload.get("metadata", {}),
                )
                session_id = broker_msg.payload.get("session_id", "default")
                if self._send_timeout is not None:
                    try:
                        await asyncio.wait_for(
                            route.adapter.send(out_msg, session_id),
                            timeout=self._send_timeout,
                        )
                    except TimeoutError:
                        logger.error(
                            "Bridge output send timeout after %.1fs topic=%s session=%s",
                            self._send_timeout, route.match_topic, session_id,
                        )
                    except Exception:
                        logger.exception(
                            "Bridge output send failed topic=%s session=%s",
                            route.match_topic, session_id,
                        )
                else:
                    await route.adapter.send(out_msg, session_id)
        except asyncio.CancelledError:
            pass

    async def _bridge_output_kind(self, route: OutputRoute) -> None:
        # 对于 kind 匹配，当前实现暂不支持；未来可扩展为监听内部注册表 topic。
        raise NotImplementedError("match_kind routing requires broker registry support")
