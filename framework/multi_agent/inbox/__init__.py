"""Agent Inbox MQ 包。

提供以 MQ 语义为核心的异步消息接收、持久化和消费能力。
"""

from .consumer import InboxConsumer
from framework.hook.builtin import InboxFlushHook
from .producer import InboxProducer
from .server import InboxServer
from .server_local import LocalFileInboxServer
from .server_memory import InMemoryInboxServer
from .types import InboxMessage

__all__ = [
    "InboxMessage",
    "InboxServer",
    "LocalFileInboxServer",
    "InMemoryInboxServer",
    "InboxProducer",
    "InboxConsumer",
    "InboxFlushHook",
]
