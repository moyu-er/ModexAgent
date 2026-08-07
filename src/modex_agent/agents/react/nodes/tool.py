"""ToolNode: classify tools, suspend for approval, then batch execute."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from modex_agent.agents.react.constants import ReActEvent as GraphReActEvent
from modex_agent.agents.react.constants import (
    ReActHookPoint,
    ReActNode,
)
from modex_agent.agents.react.context import get_agent_ctx
from modex_agent.agents.react.message_builder import build_tool_message
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.agents.react.tool_dedup import StreakDecision, ToolCallDeduplicator
from modex_agent.agents.react.tool_executor import ToolExecutor
from modex_agent.approval.constants import ApprovalDecision, ApprovalTier
from modex_agent.core.agent import AgentContext
from modex_agent.core.message import TextPart
from modex_agent.core.tool_manager import ToolResult
from modex_agent.core.types import ToolCall
from modex_agent.runtime.enums import (
    ApprovalDenyPolicy,
    ApprovalSubjectType,
    MessageDeltaSource,
    OperationStatus,
    SnapshotReason,
    ToolBatchStatus,
    ToolCallStatus,
    TurnCustomKey,
    TurnPhase,
)
from modex_agent.runtime.models import (
    ApprovalRequestState,
    ApprovalTransaction,
    MessageDelta,
    ToolArguments,
    ToolBatchState,
    ToolCallState,
)
from modex_graph.context import GraphContext
from modex_graph.integration import IntegratedInput
from modex_graph.node import Node

logger = logging.getLogger(__name__)


class ToolNode(Node[ReActTurnState]):
    """Two-phase tool node: classify all, persist approval state, batch execute."""

    def __init__(
        self,
        tool_executor: ToolExecutor,
        deduplicator: ToolCallDeduplicator | None = None,
    ) -> None:
        self.name = ReActNode.TOOL
        self._tool_executor = tool_executor
        self._deduplicator = deduplicator

    async def execute(
        self,
        ctx: GraphContext[ReActTurnState],
        integrated_input: IntegratedInput,
    ) -> None:
        state = ctx.state
        if state.phase == TurnPhase.SUSPENDED:
            await self._resume_suspended_batch(ctx)
            return None
        if state.llm_response is None:
            self.deliver(None, ReActNode.END, ctx)
            return None

        response = state.llm_response
        tool_calls: list[ToolCall] = response.tool_calls
        state.llm_response = None
        state.current_node = ReActNode.TOOL

        # Canonicalize call_id once, up front: providers may omit it, and
        # every downstream consumer (TOOL_CALL_START/END events,
        # ToolCallState, approval requests, history tool messages) must see
        # the SAME id for a call — otherwise streamed start/end pairs and
        # persisted call/result records cannot be matched by id.
        tool_calls = [
            tc if tc.call_id else tc.model_copy(update={"call_id": uuid4().hex})
            for tc in tool_calls
        ]

        agent_ctx = get_agent_ctx(ctx)
        max_tools = (
            agent_ctx.runtime.state.custom.get(TurnCustomKey.MAX_TOOLS_PER_TURN)
            if agent_ctx.runtime
            else None
        )
        if (
            max_tools is not None
            and isinstance(max_tools, (int, float))
            and len(tool_calls) > max_tools
        ):
            await ctx.runtime.emit(
                GraphReActEvent.ERROR,
                f"Exceeded max_tools_per_turn ({max_tools})",
                ctx,
            )
            self.deliver(None, ReActNode.END, ctx)
            return None

        decisions = self._classify_all(tool_calls, agent_ctx)
        await self._emit_batch(ctx, GraphReActEvent.TOOL_CALL_START, tool_calls)
        call_states = [
            ToolCallState(
                # Canonicalized above; the fallback only guards against future
                # refactors breaking that invariant — it must never crash a turn.
                call_id=tc.call_id or uuid4().hex,
                tool_name=tc.tool_name,
                arguments=ToolArguments(values=tc.arguments or {}),
            )
            for tc in tool_calls
        ]
        batch = state.create_tool_batch(iteration=state.iteration, calls=call_states)
        self._apply_decisions_to_batch(batch, decisions)

        if ApprovalDecision.PENDING in decisions:
            await self._suspend_for_approval(batch, tool_calls, decisions, ctx)
            return None

        await self._execute_batch(
            tool_calls,
            self._normalize_batch_decisions(decisions),
            ctx,
        )
        return None

    async def _suspend_for_approval(
        self,
        batch: ToolBatchState,
        tool_calls: list[ToolCall],
        decisions: list[ApprovalDecision],
        ctx: GraphContext[ReActTurnState],
    ) -> None:
        state = ctx.state
        agent_ctx = get_agent_ctx(ctx)
        if agent_ctx.runtime is None or agent_ctx.runtime.turn_store is None:
            logger.error("ToolNode: approval required but no TurnStateStore configured")
            self.deliver(None, ReActNode.END, ctx)
            return None

        approval_id = uuid4().hex
        requests: list[ApprovalRequestState] = []
        for tc, call_state, decision in zip(tool_calls, batch.calls, decisions, strict=False):
            if decision != ApprovalDecision.PENDING:
                continue
            call_state.approval_id = approval_id
            call_state.status = ToolCallStatus.PENDING
            requests.append(
                ApprovalRequestState(
                    request_id=uuid4().hex,
                    approval_id=approval_id,
                    tool_call_id=call_state.call_id,
                    tool_name=tc.tool_name,
                    arguments=ToolArguments(values=tc.arguments or {}),
                    tier=self._get_tier(tc, agent_ctx),
                    iteration=state.iteration,
                )
            )

        batch.approval_id = approval_id
        batch.status = ToolBatchStatus.SUSPENDED
        state.approval = ApprovalTransaction(
            approval_id=approval_id,
            turn_id=state.identity.turn_id,
            subject_type=ApprovalSubjectType.TOOL_BATCH,
            subject_ids=[batch.batch_id],
            requests=requests,
        )
        state.phase = TurnPhase.SUSPENDED
        state.current_node = ReActNode.TOOL
        state.resume_target = ReActNode.TOOL

        # Snapshot capture routes through ``ctx.runtime``. ``ctx.interrupt``
        # raises ``modex_graph.GraphInterrupt`` (a ``GraphBubbleUp`` subclass)
        # — the engine propagates it verbatim to the caller's ``run()``.
        # ``resume_target`` is set before the snapshot so it persists and
        # ``StartNode`` routes back here on re-entry.
        await ctx.runtime.capture_snapshot(ctx, SnapshotReason.TOOL_APPROVAL_REQUIRED.value)
        ctx.interrupt(requests)

    async def _resume_suspended_batch(self, ctx: GraphContext[ReActTurnState]) -> None:
        state = ctx.state
        if state.approval is None:
            self.deliver(None, ReActNode.END, ctx)
            return None
        batch = state.active_tool_batch()
        if batch is None:
            self.deliver(None, ReActNode.END, ctx)
            return None

        pending_requests = [
            req
            for req in state.approval.requests
            if state.approval.decisions.get(req.tool_call_id, ApprovalDecision.PENDING)
            == ApprovalDecision.PENDING
        ]
        if pending_requests:
            ctx.interrupt(pending_requests)

        tool_calls = [
            ToolCall(
                tool_name=call.tool_name,
                arguments=dict(call.arguments.values),
                call_id=call.call_id,
            )
            for call in batch.calls
        ]
        decisions = self._normalize_batch_decisions(
            [
                state.approval.decisions.get(call.call_id, ApprovalDecision.ALLOWED)
                for call in batch.calls
            ]
        )
        self._apply_decisions_to_batch(batch, decisions)

        pre_approved_ids = {
            call.call_id
            for call in batch.calls
            if state.approval.decisions.get(call.call_id) == ApprovalDecision.ALLOWED
        }
        if pre_approved_ids:
            agent_ctx = get_agent_ctx(ctx)
            if agent_ctx.runtime is not None:
                agent_ctx.runtime.state.custom[TurnCustomKey.PRE_APPROVED_TOOL_IDS] = (
                    pre_approved_ids
                )

        state.phase = TurnPhase.RUNNING
        state.current_node = ReActNode.TOOL
        await self._execute_batch(tool_calls, decisions, ctx)
        return None

    def _classify_all(
        self,
        tool_calls: list[ToolCall],
        ctx: AgentContext,
    ) -> list[ApprovalDecision]:
        decisions: list[ApprovalDecision] = []
        for tc in tool_calls:
            tier = self._get_tier(tc, ctx)
            if tier == ApprovalTier.NORMAL:
                decisions.append(ApprovalDecision.ALLOWED)
            elif tier == ApprovalTier.HARDLINE:
                decisions.append(ApprovalDecision.DENIED)
            else:
                decisions.append(ApprovalDecision.PENDING)
        return decisions

    def _get_tier(self, tc: ToolCall, ctx: AgentContext) -> ApprovalTier:
        runtime = ctx.runtime
        if runtime and runtime.approval:
            return ApprovalTier(runtime.approval.classifier.classify(tc, ctx))
        return ApprovalTier.NORMAL

    @staticmethod
    def _normalize_batch_decisions(decisions: list[ApprovalDecision]) -> list[ApprovalDecision]:
        has_denial = any(
            decision in (ApprovalDecision.DENIED, ApprovalDecision.PREEMPTED)
            for decision in decisions
        )
        if not has_denial:
            return decisions
        return [
            ApprovalDecision.PREEMPTED if decision == ApprovalDecision.ALLOWED else decision
            for decision in decisions
        ]

    @staticmethod
    def _apply_decisions_to_batch(
        batch: ToolBatchState,
        decisions: list[ApprovalDecision],
    ) -> None:
        for call_state, decision in zip(batch.calls, decisions, strict=False):
            call_state.decision = decision
            if decision == ApprovalDecision.ALLOWED:
                call_state.status = ToolCallStatus.ALLOWED
            elif decision == ApprovalDecision.DENIED:
                call_state.status = ToolCallStatus.DENIED
            elif decision == ApprovalDecision.PREEMPTED:
                call_state.status = ToolCallStatus.PREEMPTED

    async def _execute_batch(
        self,
        tool_calls: list[ToolCall],
        decisions: list[ApprovalDecision],
        ctx: GraphContext[ReActTurnState],
    ) -> None:
        decisions = self._normalize_batch_decisions(decisions)
        state = ctx.state
        agent_ctx = get_agent_ctx(ctx)

        if self._deduplicator is not None:
            self._deduplicator.begin_step()

        await ctx.runtime.emit(
            GraphReActEvent.PROGRESS,
            {"hint": self._format_hint(tool_calls), "tool_hint": True},
            ctx,
        )
        await ctx.runtime.dispatch_hook(
            ReActHookPoint.BEFORE_TOOL_EXECUTION,
            ctx,
            data={"tool_calls": tool_calls},
        )
        await ctx.runtime.drain_control(ctx)

        batch = state.active_tool_batch()
        denied_encountered = False
        dedup_stop = False
        tool_results: list[Any] = []
        for tc, decision in zip(tool_calls, decisions, strict=False):
            if decision == ApprovalDecision.ALLOWED:
                result, stop = await self._execute_single(tc, agent_ctx)
                if stop:
                    dedup_stop = True
            else:
                result = ToolResult(
                    tool_name=tc.tool_name,
                    error=self._denial_message(decision, tc, state),
                )

            await ctx.runtime.emit(GraphReActEvent.TOOL_CALL_END, (tc, result), ctx)

            tool_results.append(result)

            tool_msg = build_tool_message(result, tc.call_id)
            await agent_ctx.history.append(tool_msg)
            state.message_delta.append(
                MessageDelta(message=tool_msg, source=MessageDeltaSource.TOOL)
            )

            if result.content_blocks and agent_ctx.runtime is not None:
                from modex_agent.media.tool_media import ToolMediaEntry

                media_cache = state.custom.setdefault(TurnCustomKey.TOOL_MEDIA_CACHE, {})
                media_cache[tc.call_id or ""] = ToolMediaEntry(
                    call_id=tc.call_id or "",
                    tool_name=tc.tool_name,
                    image_blocks=result.content_blocks,
                )

            if batch is not None:
                for call_state in batch.calls:
                    if call_state.call_id == tc.call_id:
                        call_state.result = result
                        call_state.status = (
                            ToolCallStatus.COMPLETED
                            if result.error is None
                            else ToolCallStatus.FAILED
                        )

            if decision in (ApprovalDecision.DENIED, ApprovalDecision.PREEMPTED):
                denied_encountered = True

        if self._deduplicator is not None:
            self._deduplicator.end_step()

        if batch is not None:
            if batch.operation_id:
                state.update_operation(batch.operation_id, OperationStatus.COMPLETED)
            batch.status = (
                ToolBatchStatus.FAILED if denied_encountered else ToolBatchStatus.COMPLETED
            )

        await ctx.runtime.dispatch_hook(
            ReActHookPoint.AFTER_TOOL_EXECUTION,
            ctx,
            data={"results": tool_results},
        )
        await ctx.runtime.emit(
            GraphReActEvent.ITERATION_END,
            {"iteration": state.iteration, "has_tool_calls": True},
            ctx,
        )

        if dedup_stop:
            state.phase = TurnPhase.CANCELLED
            self.deliver(None, ReActNode.END, ctx)
            return None

        if denied_encountered:
            # EXTENSION POINT: whether denial cancels the ReAct turn is
            # configurable per-agent or per-batch via ApprovalDenyPolicy.
            # Default TOOL_RESULT_ONLY keeps the loop running so the agent
            # can respond with denial context (e.g. "unrelated input: xxx").
            # To cancel the turn on any denied tool, set
            # ctx.runtime.approval.default_deny_policy to CANCEL_TURN.
            deny_policy: ApprovalDenyPolicy = ApprovalDenyPolicy.TOOL_RESULT_ONLY
            if agent_ctx.runtime and agent_ctx.runtime.approval:
                deny_policy = agent_ctx.runtime.approval.default_deny_policy
            if deny_policy == ApprovalDenyPolicy.CANCEL_TURN:
                state.phase = TurnPhase.CANCELLED
                self.deliver(None, ReActNode.END, ctx)
                return None

        self.deliver(tool_results, ReActNode.LLM, ctx)
        return None

    async def _execute_single(
        self,
        tc: ToolCall,
        agent_ctx: AgentContext,
    ) -> tuple[ToolResult, bool]:
        """Execute a single allowed tool call, applying dedup logic.

        When a :class:`ToolCallDeduplicator` is configured:
        1. Same-step dedup — if an identical call was already executed
           in this step, reuse the cached result.
        2. Cross-step streak — check the streak before executing. At
           high streaks the call is skipped (synthetic result) or the
           turn is stopped.

        Returns:
            A ``(ToolResult, should_stop)`` tuple. *should_stop* is
            ``True`` only when the streak threshold for force-cancelling
            the turn has been reached.
        """
        args = tc.arguments or {}
        if self._deduplicator is not None:
            cached = self._deduplicator.check_same_step(tc.tool_name, args)
            if cached is not None:
                return cached, False

            streak_action = self._deduplicator.check_streak(tc.tool_name, args)

            if streak_action.action == StreakDecision.STOP:
                result = ToolResult.from_text(tc.tool_name, streak_action.reminder)
                self._deduplicator.register_result(tc.tool_name, args, result)
                return result, True

            if streak_action.action == StreakDecision.SKIP:
                result = ToolResult.from_text(tc.tool_name, streak_action.reminder)
                self._deduplicator.register_result(tc.tool_name, args, result)
                return result, False

        # Execute the tool call
        result = await self._tool_executor.execute(tc, agent_ctx)

        if self._deduplicator is not None:
            if streak_action.action == StreakDecision.REMIND:
                existing = result.message_content()
                appended = (
                    f"{existing}\n{streak_action.reminder}" if existing else streak_action.reminder
                )
                result = result.model_copy(update={"content": [TextPart(text=appended)]})
            self._deduplicator.register_result(tc.tool_name, args, result)

        return result, False

    @staticmethod
    def _denial_message(
        decision: ApprovalDecision,
        tc: ToolCall,
        state: ReActTurnState,
    ) -> str:
        """Clear, firm message for a denied/preempted tool call.

        Fed back to the agent as the tool result so it understands THIS
        SPECIFIC INVOCATION was rejected by the user and must not be retried.
        The old ``"Error: denied"`` rendered as ``"Error: Error: denied"``,
        which looked like a generic tool error and left the agent unable to
        tell it had been rejected -- so it re-issued the same dangerous tool,
        re-suspended, and the user saw "deny all 后卡住 / 又冒出一个待审批卡
        片". The wording also clarifies that the tool itself is not banned,
        only this specific invocation is disallowed.

        ``error`` is the bare message; ``ToolResult.to_message`` prepends a
        single ``"Error: "`` so the final history content reads cleanly.
        """
        reason = state.approval.deny_reason if state.approval is not None else None
        if decision == ApprovalDecision.PREEMPTED:
            return (
                f"Skipped: this specific call to '{tc.tool_name}' was not "
                f"allowed because another tool call in the same batch was "
                f"denied by the user. The tool itself is not banned, but do "
                f"not retry this invocation."
            )
        detail = f" Reason: {reason}." if reason else ""
        return (
            f"Denied by user: this specific call to '{tc.tool_name}' was "
            f"explicitly rejected by the user.{detail} The tool itself is not "
            f"banned, but this invocation is not allowed and must not be "
            f"retried. Acknowledge the rejection and ask the user how to "
            f"proceed."
        )

    @staticmethod
    def _format_hint(tool_calls: list[ToolCall]) -> str:
        if not tool_calls:
            return "preparing tools..."
        if len(tool_calls) == 1:
            return f"calling {tool_calls[0].tool_name}..."
        names = ", ".join(tc.tool_name for tc in tool_calls)
        return f"calling tools: {names}..."

    @staticmethod
    async def _emit_batch(
        ctx: GraphContext[ReActTurnState],
        event: GraphReActEvent,
        items: list[ToolCall],
    ) -> None:
        """Emit ``event`` once per item in ``items`` through ``ctx.runtime.emit``.

        Preserves the previous ``for tc in tool_calls: await ctx.emitter.emit(...)``
        ordering — ``ctx.runtime.emit`` is async and awaited in a loop, so emit
        order matches the prior direct-emitter path.
        """
        for item in items:
            await ctx.runtime.emit(event, item, ctx)


__all__ = ["ToolNode"]
