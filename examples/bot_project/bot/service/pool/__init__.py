"""Pool mode assembly — declared-pool boot and bot pool glue.

``create_pool`` and the execution strategies live in the framework
(:mod:`modex_agent.plugins.assembly.pool_factory` /
:mod:`modex_agent.plugins.assembly.strategies`, promoted W4a); this
package keeps the declaration boot and the bot-owned hooks.
"""

from __future__ import annotations

from bot.service.pool.communication import UserNoticeCleanupHook

__all__ = [
    "UserNoticeCleanupHook",
]
