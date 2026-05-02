"""SubagentMemoryCleanupHook — Subagent 记忆清理 Hook。

在 subagent turn 结束后，调用清理回调删除临时记忆目录。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from framework.core.agent import AgentContext

logger = logging.getLogger(__name__)


class SubagentMemoryCleanupHook:
    """Subagent 记忆清理 Hook。

    在 subagent turn 结束后，调用清理回调函数删除临时记忆目录。
    """

    def __init__(
        self,
        cleanup_fn: Callable[[str], Any] | None,
        session_id: str,
    ) -> None:
        self._cleanup_fn = cleanup_fn
        self._session_id = session_id

    async def after_turn(self, ctx: AgentContext[Any], result: Any = None) -> None:
        if self._cleanup_fn is None:
            return
        try:
            await self._cleanup_fn(self._session_id)
            logger.info(
                "SubagentMemoryCleanupHook: cleaned up memory for session %s",
                self._session_id,
            )
        except Exception:
            logger.exception(
                "SubagentMemoryCleanupHook: failed to clean up session %s",
                self._session_id,
            )
