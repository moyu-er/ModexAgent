"""Pool builder subpackage.

Re-exports the public API previously available from ``pool_builder.py``.
Internal modules are split per ADR-0025 ticket 6 for locality and
maintainability.
"""

from __future__ import annotations

from bot.service.pool.communication import UserNoticeCleanupHook
from bot.service.pool.factory import create_pool

__all__ = [
    "UserNoticeCleanupHook",
    "create_pool",
]
