"""ApprovalRenderer — suspend → render → resume 审批流模块。

从 AgentPipeline 中提取: detect / handle / drain 三个审批相关方法。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ..agents.react.constants import ReActMetaKey
from ..approval.constants import ApprovalDecision
from ..approval.response import parse_approval_action
from ..approval.store import LocalFileApprovalStateStore
from ..approval.types import ApprovalAction
from ..core.graph.interrupt import GraphInterrupt, _current_resume
from ..core.types import InputMessage
from ..memory.history import inject_attachments_to_history

if TYPE_CHECKING:
    from ..agents.react.runtime import ReActRuntime
    from ..agents.react.state import TurnResumeStateStore
    from ..agents.react.strategy import SuspendStrategy
    from ..approval.state import ApprovalState
    from ..control.ui.abc import ControlUserInterface
    from ..core.agent import AgentContext
    from ..core.context import ContextManager
    from ..core.emitter import AgentResult, ContentEmitter

logger = logging.getLogger(__name__)


def format_approval_prompt(req: object) -> str:
    """Format an approval request for display to the user."""
    tool_name = getattr(req, "tool_name", "unknown")
    call_id = getattr(req, "tool_call_id", "")
    args = getattr(req, "arguments", {})
    tier = getattr(req, "tier", "unknown")
    args_str = ", ".join(f"{k}={v}" for k, v in (args or {}).items())
    return (
        f"Approval Required [{tier.upper()}]\n"
        f"Tool: {tool_name}\n"
        f"ID: {call_id}\n"
        f"Args: {args_str}\n"
        f"Reply /approve or /deny"
    )


class ApprovalRenderer:
    """审批 suspend → render → resume 流。

    从 AgentPipeline 的中分离出来, 负责检测审批命令、处理审批决策、排空缓冲。
    drain 时需要回调 on_drain 将缓冲消息重新注入 pipeline。
    """

    def __init__(
        self,
        *,
        approval_workspace: Path,
        checkpoint_store: object | None = None,
        agent: object | None = None,
        user_interface: "ControlUserInterface | None" = None,
        on_drain: Callable[[InputMessage], Awaitable[None]] | None = None,
    ) -> None:
        self._approval_workspace = approval_workspace
        self.checkpoint_store = checkpoint_store
        self.agent = agent
        self._user_interface = user_interface
        self._on_drain = on_drain
        self._approval_stores: dict[str, LocalFileApprovalStateStore] = {}
        self._resume_stores: dict[str, "TurnResumeStateStore"] = {}
        self._approval_pending: dict[str, list[InputMessage]] = {}

    async def detect(
        self,
        input_msg: InputMessage,
        session_id: str,
        input_metadata: dict[str, object],
        prebuilt_runtime: "ReActRuntime | None" = None,
    ) -> tuple[bool, "ApprovalState | None"]:
        """检测审批命令。返回 (is_approval, approval_state)。"""
        from ..agents.react.state import StateStoreTurnResumeStateStore

        if self.checkpoint_store is not None:
            if session_id not in self._approval_stores:
                self._approval_stores[session_id] = LocalFileApprovalStateStore(
                    self._approval_workspace
                )
            if session_id not in self._resume_stores:
                self._resume_stores[session_id] = StateStoreTurnResumeStateStore(
                    self.checkpoint_store
                )

        _is_approval_cmd = False
        approval_state: "ApprovalState | None" = None
        strategy: "SuspendStrategy | None" = None
        if prebuilt_runtime is not None:
            approval = getattr(prebuilt_runtime, "approval", None)
            if approval is not None:
                strategy = getattr(approval, "suspend_strategy", None)
        if strategy is not None:
            approval_state = await strategy.load_approval_state(session_id)
        else:
            approval_store = self._approval_stores.get(session_id)
            approval_state = await approval_store.load(session_id) if approval_store else None

        if approval_state is not None:
            action = parse_approval_action(input_msg.content or "")
            if action is not None:
                _is_approval_cmd = True
            elif input_metadata.get("source_agent"):
                self._approval_pending.setdefault(session_id, []).append(input_msg)
            else:
                truncated = (input_msg.content or "")[:50]
                approval_state.deny_reason = f'unrelated input: "{truncated}"'
                for req in approval_state.requests:
                    if req.tool_call_id not in approval_state.decisions or \
                       approval_state.decisions[req.tool_call_id] == ApprovalDecision.PENDING:
                        approval_state.apply(req.tool_call_id, ApprovalDecision.DENIED)
                        break
                if strategy is not None:
                    await strategy.save_approval_state(approval_state)
                else:
                    approval_store = self._approval_stores.get(session_id)
                    if approval_store is not None:
                        await approval_store.save(approval_state)

        return _is_approval_cmd, approval_state

    async def handle(
        self,
        action: ApprovalAction,
        approval_state: "ApprovalState",
        agent_context: "AgentContext[object]",
        emitter: "ContentEmitter[object]",
        session_id: str,
        context_state: object,
        input_metadata: dict[str, object],
        strategy: "SuspendStrategy | None",
        ctx_mgr: "ContextManager",
    ) -> "AgentResult | None":
        """处理审批命令: 应用决策, 恢复执行。返回 AgentResult 或 None。"""
        if strategy is None:
            approval_store = self._approval_stores.get(session_id)
        else:
            approval_store = getattr(strategy, "_approval_store", None)

        for req in approval_state.requests:
            if req.tool_call_id not in approval_state.decisions or \
               approval_state.decisions[req.tool_call_id] == ApprovalDecision.PENDING:
                decision = (
                    ApprovalDecision.ALLOWED if action == ApprovalAction.ALLOW
                    else ApprovalDecision.DENIED
                )
                approval_state.apply(req.tool_call_id, decision)
                break

        if approval_state.every_tool_decided:
            resume_state: object | None = None
            if strategy is not None:
                resume_state = await strategy.load_resume_state(session_id)
            else:
                _resume_store = self._resume_stores.get(session_id)
                resume_state = await _resume_store.load(session_id) if _resume_store else None
            if resume_state is not None and self.agent is not None:
                agent_context.metadata[ReActMetaKey.RESUME_STATE] = resume_state
                agent_context.metadata[ReActMetaKey.TOOL_DECISIONS] = (
                    approval_state.final_decisions()
                )
                deny_reason = getattr(approval_state, "deny_reason", None)
                if deny_reason is not None:
                    agent_context.metadata["APPROVAL_DENY_REASON"] = deny_reason

                _current_resume.set(approval_state.final_decisions())
                try:
                    result = await self.agent.run(agent_context, emitter)  # type: ignore[union-attr]
                except GraphInterrupt as interrupt_exc:
                    if self._user_interface is not None:
                        requests = interrupt_exc.value
                        if isinstance(requests, list):
                            for req in requests:
                                await self._user_interface.render_message(
                                    session_id, format_approval_prompt(req),
                                )
                                break
                    await self._drain(session_id)
                    return None
                finally:
                    _current_resume.set(None)

                if strategy is not None:
                    await strategy.delete_approval_state(session_id)
                    await strategy.delete_resume_state(session_id)
                else:
                    if approval_store is not None:
                        await approval_store.delete(session_id)
                    _resume_store = self._resume_stores.get(session_id)
                    if _resume_store is not None:
                        await _resume_store.delete(session_id)

                await self._drain(session_id)

                if result is not None and result.attachments:
                    await inject_attachments_to_history(
                        context_state.history, result.attachments
                    )
                await ctx_mgr.save(
                    session_id=session_id,
                    user_message=None,
                    assistant_result=result,
                    metadata={"input_metadata": input_metadata},
                )
                return result
        else:
            if strategy is not None:
                await strategy.save_approval_state(approval_state)
            elif approval_store is not None:
                await approval_store.save(approval_state)
            if self._user_interface is not None:
                for req in approval_state.requests:
                    if req.tool_call_id not in approval_state.decisions:
                        await self._user_interface.render_message(
                            session_id, format_approval_prompt(req),
                        )
                        break

        return None

    def cleanup_session(self, session_id: str) -> None:
        """Clean up per-session approval resources."""
        self._approval_pending.pop(session_id, None)
        self._approval_stores.pop(session_id, None)
        self._resume_stores.pop(session_id, None)

    async def _drain(self, session_id: str) -> None:
        """Replay buffered agent messages after approval completes."""
        pending = self._approval_pending.pop(session_id, [])
        if pending and self._on_drain is None:
            logger.warning(
                "ApprovalRenderer: _on_drain is None, dropping %d buffered messages for %s",
                len(pending), session_id,
            )
            return
        for msg in pending:
            asyncio.create_task(self._on_drain(msg))  # type: ignore[misc]
