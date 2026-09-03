from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.core.media import Attachment
from modex_agent.core.message import ContentFormat
from modex_agent.core.session_id import SessionIdFactory, SessionInfo

from ..adapters.output import OutputAdapter
from ..adapters.platform import StreamingMode
from ..core.constants import DefaultValues
from ..core.types import InputMessage, OutputMessage
from ..pipeline.adapters import InputAdapter
from .broker import Address, AddressKind, BrokerMessage, MessageBroker

if TYPE_CHECKING:
    from modex_agent.approval.views import ApprovalDecisionInput

logger = logging.getLogger(__name__)


class BrokerInputAdapter(InputAdapter):
    """把 Broker 的某个 Address 包装为 InputAdapter。支持 AgentMessageEnvelope 识别与去重。"""

    def __init__(
        self,
        broker: MessageBroker,
        address: Address,
        deduplicator: Any | None = None,
        *,
        session_factory: SessionIdFactory | None = None,
    ) -> None:
        super().__init__()
        self.broker = broker
        self.address = address
        self._running = False
        self._deduplicator = deduplicator
        self._session_factory = session_factory

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
                msg = _broker_msg_to_input_message(
                    broker_msg,
                    session_factory=self._session_factory,
                )
                # 去重检查
                message_id = broker_msg.headers.get("message_id") or broker_msg.payload.get(
                    "message_id"
                )
                if (
                    message_id
                    and self._deduplicator is not None
                    and self._deduplicator.is_duplicate(message_id)
                ):
                    continue
                yield msg

        return _gen()


def _broker_msg_to_input_message(
    msg: BrokerMessage,
    *,
    session_factory: SessionIdFactory | None = None,
) -> InputMessage:
    payload = BrokerInputPayload.model_validate(msg.payload)
    raw_payload = msg.payload
    sender = msg.sender
    metadata = dict(payload.metadata)
    # 透传 AgentMessageEnvelope 路由字段到 metadata
    for key in (
        "session_id",
        "agent_session_id",
        "message_id",
        "in_reply_to",
        "message_type",
        "invocation_id",
    ):
        value = raw_payload.get(key) or msg.headers.get(key)
        if value:
            metadata[key] = value

    raw_session = payload.session_id or str(sender)
    session: SessionInfo | None = None

    # Orphan 隔离：来自 agent 的消息若缺失 session_id，隔离到 synthetic session
    if sender.kind == "agent":
        cid = payload.session_id or msg.headers.get("session_id")
        if not cid:
            import uuid

            logger.warning("Orphan agent message from %s, isolating to synthetic session", sender)
            orphan_key = f"orphan:{sender.name}:{uuid.uuid4().hex[:8]}"
            if session_factory is not None:
                session = session_factory.create(
                    agent_name=sender.name,
                    external_id=orphan_key,
                )
            else:
                session = SessionInfo(session_id=orphan_key, agent_name=sender.name)

    if session is None:
        session = SessionInfo.from_str(raw_session)

    return InputMessage(
        content=payload.content,
        session=session,
        source=str(sender),
        sender_id=sender.name
        if sender.kind == "user"
        else payload.sender_id,
        channel=msg.headers.get("channel", DefaultValues.CHANNEL),
        chat_id=msg.headers.get("chat_id", DefaultValues.CHAT_ID),
        metadata=metadata,
        content_format=payload.content_format,
        truncatable_paths=payload.truncatable_paths,
        workspace=Path(payload.workspace) if payload.workspace is not None else None,
        approval_decision=payload.to_approval_decision(),
        attachments_resolved=payload.attachments_resolved,
    )


class BrokerInputPayload(BaseModel):
    """Serialized ``InputMessage`` payload that crosses the message broker.

    Built by both :func:`build_input_broker_message` and
    ``AgentPool.submit_input`` through :meth:`from_input_message`. Consumers
    validate this model before rebuilding ``InputMessage`` so transport fields
    cannot silently drift between the bridge and pool dispatch paths.

    Inter-agent envelopes also carry message-specific payload extensions, so
    those remain open while the shared input fields stay typed and validated.
    Serialize with ``model_dump(exclude_none=True)`` so absent optional input
    fields remain absent on the wire.
    """

    model_config = ConfigDict(frozen=True, extra="allow")

    content: str = ""
    session_id: str = ""
    agent_session_id: str = ""
    message_type: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    sender_id: str = DefaultValues.SENDER_ID
    chat_id: str = DefaultValues.CHAT_ID
    content_format: ContentFormat | None = None
    truncatable_paths: list[str] | None = None
    approval_decision: dict[str, Any] | None = None
    attachments_resolved: list[Attachment] = Field(default_factory=list)
    workspace: str | None = None

    @classmethod
    def from_input_message(
        cls,
        message: InputMessage,
        *,
        agent_session_id: str,
        message_type: str = "",
    ) -> BrokerInputPayload:
        return cls(
            content=message.content,
            session_id=message.session.session_id_prefix,
            agent_session_id=agent_session_id,
            message_type=message_type,
            metadata=dict(message.metadata),
            sender_id=message.sender_id,
            chat_id=message.chat_id,
            content_format=message.content_format,
            truncatable_paths=message.truncatable_paths,
            approval_decision=message.approval_decision.to_dict()
            if message.approval_decision is not None
            else None,
            attachments_resolved=list(message.attachments_resolved),
            workspace=str(message.workspace) if message.workspace is not None else None,
        )

    def to_approval_decision(self) -> ApprovalDecisionInput | None:
        from modex_agent.approval.views import ApprovalDecisionInput

        return ApprovalDecisionInput.from_dict(self.approval_decision)


def build_input_broker_message(msg: InputMessage, recipient: Address) -> BrokerMessage:
    """Build the BrokerMessage that carries an InputMessage across the broker.

    ``approval_decision`` is serialized into the payload so the dispatch side
    (pool ``_dispatch_agent_message`` / ``BrokerInputAdapter``) can reconstruct
    it. Without this, a webui approve/deny decision is lost in transport and
    arrives as an empty user turn — polluting history and leaving a dangling
    assistant ``tool_calls`` that triggers a provider 400.
    """
    payload = BrokerInputPayload.from_input_message(
        msg,
        agent_session_id=str(msg.session),
    )
    return BrokerMessage(
        payload=payload.model_dump(mode="json", exclude_none=True),
        sender=Address(kind=AddressKind.CHANNEL, name=msg.source or "unknown"),
        recipient=recipient,
        headers={
            "channel": msg.channel,
            "chat_id": msg.chat_id,
            "session_id": msg.session.session_id_prefix,
            "agent_session_id": str(msg.session),
        },
    )


class BrokerOutputAdapter(OutputAdapter):
    """把 OutputAdapter 的输出转发到 Broker。"""

    def __init__(
        self,
        broker: MessageBroker,
        sender: Address,
        default_recipient: Address | None = None,
        default_topic: str | None = None,
    ) -> None:
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
    def streaming_mode(self) -> StreamingMode:
        # TODO(STREAMING_DEBUG): changed from NONE to PSEUDO so pool-mode
        # agents use streaming LLM API while still buffering deltas.
        # After confirming it works, make this permanent.
        return StreamingMode.PSEUDO

    async def send(self, message: OutputMessage, session_id: str) -> None:
        metadata = dict(message.metadata) if message.metadata else {}
        broker_msg = BrokerMessage(
            payload={
                "content": message.content,
                "metadata": metadata,
                "session_id": metadata.get("session_id", session_id),
                "agent_session_id": metadata.get("agent_session_id", session_id),
                "message_id": metadata.get("message_id", ""),
                "in_reply_to": metadata.get("in_reply_to", ""),
            },
            sender=self.sender,
            correlation_id=metadata.get("correlation_id", session_id),
            headers={
                "session_id": metadata.get("session_id", session_id),
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
    match_kind: str | None = None  # 例如 "user"，匹配所有该 kind 的 Address
    match_topic: str | None = None  # 例如 "agent:outgoing"，订阅 topic 并路由


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
    ) -> None:
        self.broker = broker
        self.input_bindings = dict(input_bindings or {})
        self.output_routes = list(output_routes or [])
        self._send_timeout = send_timeout
        self._restart_on_failure = restart_on_failure
        self._restart_max_retries = restart_max_retries
        self._restart_backoff_seconds = restart_backoff_seconds
        self._restart_max_window_seconds = restart_max_window_seconds
        self._tasks: list[asyncio.Task[None]] = []
        self._bridge_specs: dict[str, Callable[[], Coroutine[Any, Any, None]]] = {}
        self._restart_counts: dict[str, int] = {}
        self._restart_first_fail_time: dict[str, float] = {}
        self._stopping = False

    def _bridge_done_callback(self, task: asyncio.Task[None], name: str) -> None:
        """Record exception from completed bridge task; optionally schedule restart."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Bridge task %s exited with error", name, exc_info=exc)
        else:
            logger.warning("Bridge task %s exited normally (unexpected for infinite loop)", name)
        if self._restart_on_failure:
            self._schedule_restart(name)

    def _schedule_restart(self, name: str) -> None:
        if self._stopping:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("No running event loop, cannot schedule restart for %s", name)
            return
        now = loop.time()
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
        restart_task = loop.create_task(self._restart_bridge_after_delay(name, delay))
        restart_task.add_done_callback(self._prune_done_tasks)
        self._add_task(restart_task)

    async def _restart_bridge_after_delay(self, name: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            logger.debug("Restart for bridge task %s cancelled during delay", name)
            return
        spec = self._bridge_specs.get(name)
        if spec is None:
            logger.warning("No bridge spec found for %s, cannot restart", name)
            return
        logger.info("Restarting bridge task %s", name)
        task = asyncio.create_task(spec())
        task.add_done_callback(partial(self._bridge_done_callback, name=name))
        self._add_task(task)

    def _add_task(self, task: asyncio.Task[None]) -> None:
        """Add a task and prune completed ones to keep the list clean."""
        self._tasks = [t for t in self._tasks if not t.done()]
        self._tasks.append(task)

    def _prune_done_tasks(self, _task: asyncio.Task[None]) -> None:
        """Remove completed tasks from the tasks list."""
        self._tasks = [t for t in self._tasks if not t.done()]

    async def start(self) -> None:
        await self.broker.start()
        for adapter, addr in self.input_bindings.items():
            await adapter.start()
            bridge_name = f"input:{adapter.name}"
            self._bridge_specs[bridge_name] = partial(self._bridge_input, adapter, addr)
            task = asyncio.create_task(self._bridge_specs[bridge_name]())
            task.add_done_callback(partial(self._bridge_done_callback, name=bridge_name))
            self._add_task(task)
        for route in self.output_routes:
            if route.match_topic:
                bridge_name = f"output:{route.match_topic}"
                self._bridge_specs[bridge_name] = partial(self._bridge_output_topic, route)
                task = asyncio.create_task(self._bridge_specs[bridge_name]())
                task.add_done_callback(partial(self._bridge_done_callback, name=bridge_name))
                self._add_task(task)
            elif route.match_kind:
                raise NotImplementedError(
                    "match_kind routing is not yet supported. "
                    "Use match_topic instead (e.g., OutputRoute(adapter=..., match_topic='agent:outgoing'))."
                )

    async def stop(self) -> None:
        self._stopping = True
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._bridge_specs.clear()
        for adapter in self.input_bindings:
            await adapter.stop()
        await self.broker.stop()

    async def _bridge_input(self, adapter: InputAdapter, addr: Address) -> None:
        while True:
            try:
                async for msg in adapter.receive():
                    broker_msg = build_input_broker_message(msg, addr)
                    await self.broker.send_to(addr, broker_msg)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Bridge input error for adapter=%s addr=%s",
                    adapter.name,
                    addr,
                )
                await asyncio.sleep(1)

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
                            self._send_timeout,
                            route.match_topic,
                            session_id,
                        )
                    except Exception:
                        logger.exception(
                            "Bridge output send failed topic=%s session=%s",
                            route.match_topic,
                            session_id,
                        )
                else:
                    await route.adapter.send(out_msg, session_id)
        except asyncio.CancelledError:
            pass

    async def _bridge_output_kind(self, route: OutputRoute) -> None:
        # 对于 kind 匹配，当前实现暂不支持；未来可扩展为监听内部注册表 topic。
        raise NotImplementedError("match_kind routing requires broker registry support")
