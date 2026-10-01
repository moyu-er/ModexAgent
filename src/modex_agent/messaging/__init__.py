"""Messaging module — the pure message-vocabulary layer (level 0).

提供轻量、可插拔的消息总线抽象：
- 核心抽象: Address, BrokerMessage, MessageBroker, DeliveryError
- 内存实现: InMemoryMessageBroker
- Agent message vocabulary (AgentAddress / AgentMessageEnvelope / AgentMessageType /
  AgentMessageRouter) and message formatting live in ``agent_messages`` / ``message_format``

The pipeline bridge (BrokerInputAdapter / BrokerOutputAdapter /
BrokerBridgeService / OutputRoute) moved to
``modex_agent.pipeline.broker_bridge`` (W3b) — it composes the pipeline's
InputAdapter, so it lives above the messaging vocabulary.
"""

from .broker import Address, AddressKind, BrokerMessage, DeliveryError, MessageBroker
from .broker_memory import InMemoryMessageBroker
from .models import (
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

__all__ = [
    "Address",
    "AddressKind",
    "BrokerMessage",
    "BrokerInputPayload",
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
    "ReminderKind",
]
