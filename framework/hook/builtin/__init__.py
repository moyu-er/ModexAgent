"""内置 Hook 实现。

框架预置的常用 Hook，包括：
- logging: RunLoggingHook
- runtime_context: RuntimeContextHook
- inbox_flush: InboxFlushHook
- peer_auto_send: PeerAutoSendHook
- subagent_cleanup: SubagentMemoryCleanupHook
"""

from framework.hook.builtin.inbox_flush import InboxFlushHook
from framework.hook.builtin.logging import RunLoggingHook
from framework.hook.builtin.peer_auto_send import PeerAutoSendHook
from framework.hook.builtin.runtime_context import RuntimeContextHook
from framework.hook.builtin.subagent_cleanup import SubagentMemoryCleanupHook

__all__ = [
    "InboxFlushHook",
    "PeerAutoSendHook",
    "RunLoggingHook",
    "RuntimeContextHook",
    "SubagentMemoryCleanupHook",
]
