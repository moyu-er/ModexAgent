"""Pool builder subpackage.

Re-exports the public API previously available from ``pool_builder.py``.
Internal modules are split per ADR-0025 ticket 6 for locality and
maintainability.
"""

from __future__ import annotations

from bot.service.pool.communication import UserNoticeCleanupListener
from bot.service.pool.factory import create_pool
from bot.service.pool.memory_defaults import ensure_long_term_defaults
from bot.service.pool.tool_projection import build_main_agent_tool_names

__all__ = [
    "UserNoticeCleanupListener",
    "build_main_agent_tool_names",
    "create_pool",
    "ensure_long_term_defaults",
]
