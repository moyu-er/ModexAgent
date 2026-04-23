"""Multi-agent 专属 Hook 实现。

通用基类（AgentRunHook、CompositeRunHook）和通用策略/Runner 已迁移到 core 层：
  - core/hooks.py     → AgentRunHook, CompositeRunHook
  - core/strategy.py  → ExecutionStrategy, ReActStrategy, SingleTurnStrategy
  - core/runner.py    → InterruptibleRunner

此文件仅保留 multi-agent 场景下的具体 Hook 实现。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from framework.core.agent import AgentContext
from framework.core.hooks import AgentRunHook

if TYPE_CHECKING:
    from .bus import AgentMessageBus
    from .coordinator import TaskCoordinator
    from .event_bus import TaskEventBus

logger = logging.getLogger(__name__)


class TaskProgressHook(AgentRunHook):
    """任务进度 Hook：向 TaskEventBus 报告进度。"""

    def __init__(self, task_id: str, event_bus: TaskEventBus):
        self._task_id = task_id
        self._event_bus = event_bus
        self._iteration = 0
        self._tool_calls = 0

    async def before_iteration(self, ctx: AgentContext) -> None:
        self._iteration += 1

    async def before_tool_execution(self, ctx: AgentContext, tool_calls: list[Any]) -> None:
        self._tool_calls += len(tool_calls)
        if self._event_bus:
            from .event_bus import TaskEvent, TaskEventType

            try:
                await self._event_bus.emit(
                    TaskEvent(
                        task_id=self._task_id,
                        event_type=TaskEventType.PROGRESS,
                        payload={
                            "iteration": self._iteration,
                            "tool_calls": self._tool_calls,
                            "progress_percent": min(95, self._iteration * 10),
                        },
                    )
                )
            except Exception:
                logger.exception("TaskProgressHook emit failed")


class TaskInterventionHook(AgentRunHook):
    """任务干预 Hook：在每次 ReAct 迭代前检查该 turn 是否被策略要求取消。"""

    def __init__(self, task_id: str, coordinator: TaskCoordinator):
        self._task_id = task_id
        self._coordinator = coordinator

    async def before_iteration(self, ctx: AgentContext) -> None:
        record = await self._coordinator.get_task_record(self._task_id)
        if not record:
            return
        results = await record.check_all()
        for result in results:
            if result.action.value == "cancel":
                raise asyncio.CancelledError(result.reason)


class PeerAutoSendHook(AgentRunHook):
    """Peer agent auto-send hook.

    Ensures peer agent content is always forwarded to the parent (main) agent,
    even when the LLM forgets to call ``send_message_async`` on its final turn.
    This is a safety net; it does **not** replace the system prompt guidance.
    """

    # Matches paired reasoning tags and their content (greedy across lines)
    _THINK_PAIRED_RE = re.compile(
        r"<\s*(?:think|reasoning|reflection)\b[^>]*(?:>|\n)(.*?)</\s*(?:think|reasoning|reflection)\b[^>]*(?:>|\n)",
        re.IGNORECASE | re.DOTALL,
    )
    # Matches self-closing or orphaned single tags
    _THINK_TAG_RE = re.compile(
        r"<\s*/?\s*(?:think|reasoning|reflection)\b[^>]*>?",
        re.IGNORECASE,
    )

    def __init__(
        self,
        agent_bus: AgentMessageBus,
        self_name: str,
        parent_name: str = "main",
    ) -> None:
        self._agent_bus = agent_bus
        self._self_name = self_name
        self._parent_name = parent_name

    async def before_turn(self, ctx: AgentContext) -> None:
        """No-op: runtime context is cleared by ReActAgent._clear_runtime_context."""

    async def after_turn(self, ctx: AgentContext, result) -> None:
        if not result or not result.content:
            return

        # Prefer resolved runtime_context; fall back to manager if not cached
        rc = ctx.runtime_context
        if rc is None and ctx.runtime_context_manager is not None:
            rc = await ctx.runtime_context_manager.get_context(
                ctx.session_id, ctx.metadata
            )
        if rc is not None:
            calls = await rc.get_tool_calls()
            sent_tools = {"send_message", "send_message_async"}
            if any(c.tool_name in sent_tools for c in calls):
                logger.debug(
                    "PeerAutoSendHook: skipped, message already sent via tool (peer=%s)",
                    self._self_name,
                )
                return

        logger.info(
            "PeerAutoSendHook: auto-forwarding peer %s content to %s (len=%d)",
            self._self_name,
            self._parent_name,
            len(result.content),
        )

        # Auto-forward final content to main.
        session_id = ctx.metadata.get("session_id", "")
        from .address import AgentAddress
        from .envelope import AgentMessageEnvelope
        from .utils import (
            format_pool_session_id,
            is_peer_session_id,
            parse_peer_session_id,
            parse_pool_session_id,
        )

        if is_peer_session_id(session_id):
            conversation_id, _, _ = parse_peer_session_id(session_id)
        else:
            conversation_id, _ = parse_pool_session_id(session_id)

        inbox_key = format_pool_session_id(conversation_id, self._parent_name)

        sanitized = self._sanitize_forward_content(result.content)

        envelope = AgentMessageEnvelope(
            payload={"content": sanitized, "message_type": "agent_message"},
            source=AgentAddress(name=self._self_name),
            target=AgentAddress(name=self._parent_name),
            message_type="agent_message",
            conversation_id=conversation_id,
            agent_session_id=session_id,
        )

        try:
            await self._agent_bus.send(inbox_key, envelope)
            logger.info(
                "Auto-forwarded peer %s content to %s (session=%s)",
                self._self_name,
                self._parent_name,
                session_id,
            )
        except Exception:
            logger.exception(
                "Failed to auto-forward peer %s content to %s",
                self._self_name,
                self._parent_name,
            )

    @classmethod
    def _sanitize_forward_content(cls, content: str) -> str:
        """Strip LLM reasoning tags and apply inbox sanitization."""
        from .inbox.hook import InboxFlushHook

        # Remove paired tags and their content: <think>...content...</think>
        content = cls._THINK_PAIRED_RE.sub("", content)
        # Remove any remaining self-closing or orphaned single tags
        content = cls._THINK_TAG_RE.sub("", content)
        # Reuse InboxFlushHook sanitization for injection defense
        content = InboxFlushHook._sanitize_content(content)
        return content


class SubagentMemoryCleanupHook(AgentRunHook):
    """Subagent 记忆清理 Hook。

    在 subagent turn 结束后，调用清理回调函数删除临时记忆目录。
    由 CompositeRunHook 按顺序调用。
    """

    def __init__(
        self,
        cleanup_fn: Callable[[str], Any] | None,
        session_id: str,
    ) -> None:
        self._cleanup_fn = cleanup_fn
        self._session_id = session_id

    async def after_turn(self, ctx: AgentContext, result) -> None:
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
