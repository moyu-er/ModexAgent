"""Re-export MessageHistory types from framework.core.history.

Canonical location moved to core to break the core <-> memory cycle.
"""

from modex_agent.core.history import (
    ListMessageHistory,
    MessageHistory,
    history_to_list,
    inject_attachments_to_history,
)

__all__ = [
    "ListMessageHistory",
    "MessageHistory",
    "history_to_list",
    "inject_attachments_to_history",
]
