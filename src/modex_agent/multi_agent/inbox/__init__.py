"""Agent Inbox MQ 包。

提供以 MQ 语义为核心的异步消息接收、持久化和消费能力。

T11 evolution: ``InboxServer`` → ``InboxMQ`` ABC (with sync ``deliver()``
for CLI cross-process use). ``InboxServer`` is kept as a deprecated alias.
``LocalFileInboxServer`` → ``LocalFileInboxMQ`` (alias retained).
``DeliveredIdTracker`` is deprecated (merged into ``InboxMQ`` internal).
"""

from .consumer import InboxConsumer
from .producer import InboxProducer
from .server import InboxMQ, InboxServer
from .server_local import LocalFileInboxMQ, LocalFileInboxServer
from .server_memory import InMemoryInboxServer
from .tracker import DeliveredIdTracker, FileDeliveredIdTracker
from .types import InboxMessage

__all__ = [
    "InboxMessage",
    # New T11 names (preferred)
    "InboxMQ",
    "LocalFileInboxMQ",
    # Deprecated aliases (transition)
    "InboxServer",
    "LocalFileInboxServer",
    # Other
    "InMemoryInboxServer",
    "InboxProducer",
    "InboxConsumer",
    # Deprecated (delivered-id tracking merged into InboxMQ)
    "DeliveredIdTracker",
    "FileDeliveredIdTracker",
]
