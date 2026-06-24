"""Re-export ChatMessage and ContentFormat from framework.core.message.

Canonical location moved to core to break the core <-> memory cycle.
"""

from modex_agent.core.message import ChatMessage, ContentFormat

__all__ = ["ChatMessage", "ContentFormat"]