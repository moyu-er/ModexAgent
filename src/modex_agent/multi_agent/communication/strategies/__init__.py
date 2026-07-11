"""Communication strategies."""

from __future__ import annotations

from modex_agent.multi_agent.communication.strategies.base import (
    SendDeps,
    SendRequest,
    SendStrategy,
)
from modex_agent.multi_agent.communication.strategies.parent_reply import ParentReplyStrategy
from modex_agent.multi_agent.communication.strategies.subagent_dispatch import (
    SubagentDispatchStrategy,
)

__all__ = [
    "SendDeps",
    "SendRequest",
    "SendStrategy",
    "SubagentDispatchStrategy",
    "ParentReplyStrategy",
]
