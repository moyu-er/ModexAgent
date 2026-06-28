"""ToolNode: classify tools, suspend for approval, then batch execute."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from modex_agent.agents.react.agent import ReActEvent
from modex_agent.agents.react.constants import ReActNode, ReActReason
from modex_agent.agents.react.message_builder import build_tool_message
from modex_agent.agents.react.state import ReActSnapshotPolicy, ReActTurnState, get_react_state
from modex_agent.agents.react.tool_executor import ToolExecutor
from modex_agent.approval.constants import ApprovalDecision, ApprovalTier
from modex_agent.core.agent import AgentContext
from modex_agent.core.graph.interrupt import interrupt
from modex_agent.core.graph.node import Node, NodeTransition
from modex_agent.core.tool_manager import ToolResult
from modex_agent.core.types import ToolCall
from modex_agent.hook import HookPayload, HookPoint
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

logger = logging.getLogger(__name__)


class ToolNode(Node):
    """Two-phase tool node: classify all, persist approval state, batch execute."""

    def __init__(self, tool_executor: ToolExecutor) -> None:
        super().__init__(ReActNode.TOOL)
        self._tool_executor = tool_executor

    async def execute(self, ctx: AgentContext) -> NodeTransition:
        state = get_react_state(ctx)
        if state is not None and state.phase == TurnPhase.SUSPENDED:
            return await self._resume_suspended_batch(ctx)
        if state is None or state.llm_response is None:
            return NodeTransition(ReActNode.END, ReActReason.LLM_ERROR)

        response = state.llm_response
        tool_calls: list[ToolCall] = response.tool_calls
        state.llm_response = None
        state.current_node = ReActNode.TOOL

        max_tools = (
            ctx.runtime.state.custom.get(TurnCustomKey.MAX_TOOLS_PER_TURN) if ctx.runtime else None
        )
        if max_tools is not None and len(tool_calls) > max_tools:
            if ctx.emitter is not None:
                await ctx.emitter.emit(
                    ReActEvent.ERROR, f"Exceeded max_tools_per_turn ({max_tools})"
                )
            return NodeTransition(ReActNode.END, ReActReason.TURN_CANCELLED)

        decisions = self._classify_all(tool_calls, ctx)
        if ctx.emitter is not None:
            for tc in tool_calls:
                await ctx.emitter.emit(ReActEvent.TOOL_CALL_START, tc)
        call_states = [
            ToolCallState(
                call_id=tc.call_id or uuid4().hex,
                tool_name=tc.tool_name,
                arguments=ToolArguments(values=tc.arguments or {}),
            )
            for tc in tool_calls
        ]
        batch = state.create_tool_batch(iteration=state.iteration, calls=call_states)
        self._apply_decisions_to_batch(batch, decisions)

        if ApprovalDecision.PENDING in decisions:
            return await self._suspend_for_approval(batch, tool_calls, decisions, ctx)

        return await self._execute_batch(
            tool_calls,
            self._normalize_batch_decisions(decisions),
            ctx,
        )

    async def _suspend_for_approval(
        self,
        batch: ToolBatchState,
        tool_calls: list[ToolCall],
        decisions: list[ApprovalDecision],
        ctx: AgentContext,
    ) -> NodeTransition:
        react_state = get_react_state(ctx)
        if react_state is None:
            return NodeTransition(ReActNode.END, ReActReason.TURN_CANCELLED)
        if ctx.runtime is None or ctx.runtime.turn_store is None:
            logger.error("ToolNode: approval required but no TurnStateStore configured")
            return NodeTransition(ReActNode.END, ReActReason.TURN_CANCELLED)

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
                    tier=self._get_tier(tc, ctx),
                    iteration=react_state.iteration,
                )
            )

        batch.approval_id = approval_id
        batch.status = ToolBatchStatus.SUSPENDED
        react_state.approval = ApprovalTransaction(
            approval_id=approval_id,
            turn_id=react_state.identity.turn_id,
            subject_type=ApprovalSubjectType.TOOL_BATCH,
            subject_ids=[batch.batch_id],
            requests=requests,
        )
        react_state.phase = TurnPhase.SUSPENDED
        react_state.current_node = ReActNode.TOOL

        snapshot = ReActSnapshotPolicy().capture(
            react_state,
            SnapshotReason.TOOL_APPROVAL_REQUIRED,
        )
        await ctx.runtime.turn_store.save_turn(snapshot)
        return interrupt(requests)

    async def _resume_suspended_batch(self, ctx: AgentContext) -> NodeTransition:
        react_state = get_react_state(ctx)
        if react_state is None or react_state.approval is None:
            return NodeTransition(ReActNode.END, ReActReason.TURN_CANCELLED)
        batch = react_state.active_tool_batch()
        if batch is None:
            return NodeTransition(ReActNode.END, ReActReason.TURN_CANCELLED)

        pending_requests = [
            req
            for req in react_state.approval.requests
            if react_state.approval.decisions.get(req.tool_call_id, ApprovalDecision.PENDING)
            == ApprovalDecision.PENDING
        ]
        if pending_requests:
            return interrupt(pending_requests)

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
                react_state.approval.decisions.get(call.call_id, ApprovalDecision.ALLOWED)
                for call in batch.calls
            ]
        )
        self._apply_decisions_to_batch(batch, decisions)

        pre_approved_ids = {
            call.call_id
            for call in batch.calls
            if react_state.approval.decisions.get(call.call_id) == ApprovalDecision.ALLOWED
        }
        if pre_approved_ids and ctx.runtime and ctx.runtime.state:
            ctx.runtime.state.custom[TurnCustomKey.PRE_APPROVED_TOOL_IDS] = pre_approved_ids

        react_state.phase = TurnPhase.RUNNING
        react_state.current_node = ReActNode.TOOL
        return await self._execute_batch(tool_calls, decisions, ctx)

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
        ctx: AgentContext,
    ) -> NodeTransition:
        decisions = self._normalize_batch_decisions(decisions)
        state = get_react_state(ctx)
        if ctx.emitter is not None:
            await ctx.emitter.emit(
                ReActEvent.PROGRESS,
                {"hint": self._format_hint(tool_calls), "tool_hint": True},
            )
        if ctx.runtime and ctx.runtime.hooks:
            await ctx.runtime.hooks.dispatch(
                HookPoint.BEFORE_TOOL_EXECUTION,
                ctx,
                payload=HookPayload(data={"tool_calls": tool_calls}),
            )

        # Drain control commands before tool execution
        if ctx.runtime and ctx.runtime.control_channel:
            from modex_agent.hook.builtin.control_drain import drain_control_channel

            await drain_control_channel(
                ctx.runtime.control_channel,
                ctx,
                turn_uuid=ctx.runtime.turn_uuid,
            )

        batch = state.active_tool_batch() if state else None
        denied_encountered = False
        tool_results: list[Any] = []
        for tc, decision in zip(tool_calls, decisions, strict=False):
            if decision == ApprovalDecision.ALLOWED:
                result = await self._tool_executor.execute(tc, ctx)
            else:
                result = ToolResult(
                    tool_name=tc.tool_name,
                    result=None,
                    error=self._denial_message(decision, tc, state),
                )

            if ctx.emitter is not None:
                await ctx.emitter.emit(ReActEvent.TOOL_CALL_END, (tc, result))

            tool_results.append(result)

            tool_msg = build_tool_message(result, tc.call_id)
            await ctx.history.append(tool_msg)
            if state is not None:
                state.message_delta.append(
                    MessageDelta(message=tool_msg, source=MessageDeltaSource.TOOL)
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

        if state is not None and batch is not None:
            if batch.operation_id:
                state.update_operation(batch.operation_id, OperationStatus.COMPLETED)
            batch.status = (
                ToolBatchStatus.FAILED if denied_encountered else ToolBatchStatus.COMPLETED
            )

        if ctx.runtime and ctx.runtime.hooks:
            await ctx.runtime.hooks.dispatch(
                HookPoint.AFTER_TOOL_EXECUTION,
                ctx,
                payload=HookPayload(data={"results": tool_results}),
            )

        if ctx.emitter is not None:
            await ctx.emitter.emit(
                ReActEvent.ITERATION_END,
                {"iteration": state.iteration if state else 0, "has_tool_calls": True},
            )

        if denied_encountered:
            # EXTENSION POINT: whether denial cancels the ReAct turn is
            # configurable per-agent or per-batch via ApprovalDenyPolicy.
            # Default TOOL_RESULT_ONLY keeps the loop running so the agent
            # can respond with denial context (e.g. "unrelated input: xxx").
            # To cancel the turn on any denied tool, set
            # ctx.runtime.approval.default_deny_policy to CANCEL_TURN.
            deny_policy: ApprovalDenyPolicy = ApprovalDenyPolicy.TOOL_RESULT_ONLY
            if ctx.runtime and ctx.runtime.approval:
                deny_policy = ctx.runtime.approval.default_deny_policy
            if deny_policy == ApprovalDenyPolicy.CANCEL_TURN:
                if state is not None:
                    state.phase = TurnPhase.CANCELLED
                return NodeTransition(ReActNode.END, ReActReason.TURN_CANCELLED)

        return NodeTransition(ReActNode.LLM, ReActReason.TOOLS_DONE)

    @staticmethod
    def _denial_message(
        decision: ApprovalDecision,
        tc: ToolCall,
        state: ReActTurnState | None,
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
        reason = (
            state.approval.deny_reason
            if state is not None and state.approval is not None
            else None
        )
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
