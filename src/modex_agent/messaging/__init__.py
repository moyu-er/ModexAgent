"""消息系统模块 (Broker 抽象层)

提供轻量、可插拔的消息总线抽象：
- 核心抽象: Address, BrokerMessage, MessageBroker, DeliveryError
- 内存实现: InMemoryMessageBroker
- Pipeline 桥接: BrokerInputAdapter, BrokerOutputAdapter, BrokerBridgeService, OutputRoute
"""

from .models import (  # noqa: I001 - payloads must bind before bridge imports pipeline
    ApprovalAction,
    ApprovalDecisionInput,
    BrokerInputPayload,
    BrokerOutputPayload,
    InputMessage,
    MessageType,
    OutputMessage,
    OutputMessageType,
    ReminderKind,
)
from .broker import Address, AddressKind, BrokerMessage, DeliveryError, MessageBroker
from .broker_bridge import BrokerBridgeService, BrokerInputAdapter, BrokerOutputAdapter, OutputRoute
from .broker_memory import InMemoryMessageBroker

__all__ = [
    "Address",
    "AddressKind",
    "BrokerMessage",
    "BrokerBridgeService",
    "BrokerInputAdapter",
    "BrokerInputPayload",
    "BrokerOutputAdapter",
    "BrokerOutputPayload",
    "DeliveryError",
    "MessageBroker",
    "InMemoryMessageBroker",
    "ApprovalAction",
    "ApprovalDecisionInput",
    "InputMessage",
    "MessageType",
    "OutputMessage",
    "OutputMessageType",
    "OutputRoute",
    "ReminderKind",
]
