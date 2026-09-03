from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


class AddressKind(StrEnum):
    """Closed set of address kinds used across the messaging layer.

    ``Address.kind`` is annotated as ``AddressKind`` but accepts raw strings
    too (``StrEnum`` compares equal to its value), so existing literal
    construction sites keep working without modification.
    """

    AGENT = "agent"
    USER = "user"
    CHANNEL = "channel"
    SYSTEM = "system"
    GROUP = "group"
    _TEMP = "_temp"
    """Internal sentinel used by :meth:`InMemoryMessageBroker.subscribe` for
    transient subscription mailboxes. Not a real address kind."""


class Address(BaseModel):
    """消息寻址实体。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: AddressKind = AddressKind.AGENT
    name: str = ""

    def __str__(self) -> str:
        return f"{self.kind}:{self.name}"

    @classmethod
    def parse(cls, raw: str) -> Address:
        kind, name = raw.split(":", 1)
        return cls(kind=AddressKind(kind), name=name)


class BrokerMessage(BaseModel):
    """Broker 层传输的通用消息信封。

    Mutable (runtime message that may be modified in flight). Cross-process
    serialization goes through ``to_dict``/``from_dict`` facades that keep
    the existing wire format (Address → "kind:name" string, timestamp → isoformat).
    """

    model_config = ConfigDict(extra="forbid")

    payload: dict[str, Any]
    """业务负载。典型内容：
    - InputMessage 序列化字典
    - OutputMessage 序列化字典
    - AgentResult 序列化字典
    - HandoffRequest 字典 {"target_agent": "sales", "context": ...}
    """

    sender: Address
    """发送方地址，任何消息都必须携带。"""

    recipient: Address | None = None
    """P2P 目标地址。非空时，Broker 执行点对点投递。"""

    topic: str | None = None
    """PubSub 主题。非空时，Broker 投递到所有订阅该 topic 的消费者。"""

    broadcast: bool = False
    """全局广播标志。为 True 时，Broker 投递给所有已注册消费者。"""

    headers: dict[str, str] = Field(default_factory=dict)
    """元数据头，可携带 content_type、priority、retry_count 等。"""

    correlation_id: str | None = None
    """链路追踪 ID，用于 Handoff、请求-响应配对。"""

    timestamp: datetime = Field(default_factory=datetime.now)

    @field_validator("sender", "recipient", mode="before")
    @classmethod
    def _parse_address(cls, value: Address | str | None) -> Address | None:
        if isinstance(value, str):
            return Address.parse(value)
        return value

    @field_serializer("sender", "recipient")
    def _serialize_address(self, value: Address | None) -> str | None:
        return str(value) if value is not None else None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BrokerMessage:
        return cls.model_validate(data)


class DeliveryError(Exception):
    """消息投递失败时抛出的异常。"""


class MessageBroker(ABC):
    """消息代理抽象基类。

    职责边界：
    - 只做消息路由和队列生命周期管理。
    - 不维护 Agent 的元数据、健康状态、技能注册表。
    - 不处理业务级序列化/反序列化（由 Adapter / Agent 层负责）。
    """

    # ── 生命周期 ──
    @abstractmethod
    async def start(self) -> None:
        """启动 Broker。"""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """停止 Broker 并释放资源。

        实现必须确保：调用 stop() 后，所有阻塞在 consume/consume_stream/subscribe
        上的消费者能被唤醒并优雅退出，不丢失已入队但尚未消费的消息。
        """
        ...

    # ── 发送接口 ──
    @abstractmethod
    async def send_to(self, recipient: Address, message: BrokerMessage) -> None:
        """点对点发送。底层映射：RabbitMQ Direct Exchange + routing_key = str(address)。

        若 recipient 尚未注册，实现应自动为其创建 mailbox（与 InMemoryMessageBroker
        的行为一致），而不是抛异常。
        """
        ...

    @abstractmethod
    async def publish(self, topic: str, message: BrokerMessage) -> None:
        """发布订阅。底层映射：RabbitMQ Topic Exchange。

        若 topic 当前无任何订阅者，实现应静默丢弃该消息，不抛异常。
        """
        ...

    @abstractmethod
    async def broadcast(self, message: BrokerMessage) -> None:
        """全局广播。底层映射：RabbitMQ Fanout Exchange。"""
        ...

    # ── 消费者注册 ──
    @abstractmethod
    async def register_consumer(self, address: Address) -> None:
        """注册一个消费者 Address，创建对应的 Mailbox/Queue。"""
        ...

    @abstractmethod
    async def unregister_consumer(self, address: Address) -> None:
        """注销消费者 Address，释放资源。"""
        ...

    # ── 消费接口 ──
    @abstractmethod
    async def consume(self, address: Address) -> BrokerMessage | None:
        """从 Address 的 Mailbox 阻塞消费单条消息。Returns None on shutdown."""
        ...

    @abstractmethod
    def consume_stream(self, address: Address) -> AsyncIterator[BrokerMessage]:
        """从 Address 的 Mailbox 流式消费消息。

        当 Broker 被 stop() 时，迭代器应能收到信号并优雅退出，不抛异常。
        """
        ...

    @abstractmethod
    def subscribe(self, topics: list[str]) -> AsyncIterator[BrokerMessage]:
        """订阅一个或多个 topic，返回合并后的消息流。"""
        ...
