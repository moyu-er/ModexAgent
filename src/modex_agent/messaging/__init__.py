"""消息系统模块 (Broker 抽象层)

提供轻量、可插拔的消息总线抽象：
- 核心抽象: Address, BrokerMessage, MessageBroker, DeliveryError
- 内存实现: InMemoryMessageBroker
- Pipeline 桥接: BrokerInputAdapter, BrokerOutputAdapter, BrokerBridgeService, OutputRoute
"""

from .broker import Address, BrokerMessage, DeliveryError, MessageBroker
from .broker_bridge import BrokerBridgeService, BrokerInputAdapter, BrokerOutputAdapter, OutputRoute
from .broker_memory import InMemoryMessageBroker

__all__ = [
    "Address",
    "BrokerMessage",
    "DeliveryError",
    "MessageBroker",
    "InMemoryMessageBroker",
    "BrokerInputAdapter",
    "BrokerOutputAdapter",
    "BrokerBridgeService",
    "OutputRoute",
]
