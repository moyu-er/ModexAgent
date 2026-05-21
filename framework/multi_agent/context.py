"""Shared context variables for multi-agent coordination.

Defined in a standalone module (not __init__.py) to avoid circular imports
between pipeline, session, and multi_agent packages.
"""

import contextvars

current_conversation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_conversation_id", default=None
)
