"""ToolNode — classify tools -> strategy -> batch execute."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import uuid4

logger = logging.getLogger(__name__)

from framework.agents.react.agent import ReActEvent
from framework.agents.react.constants import ReActMetaKey, ReActNode, ReActReason
from framework.approval.constants import ApprovalDecision, ApprovalTier
from framework.approval.state import ApprovalRequest
from framework.control.runtime import ControlPhase
from framework.core.agent import AgentContext, ctx_ext
from framework.core.context_extensions import ExtensionKey
from framework.core.emitter import ToolCall
from framework.core.graph.node import Node, NodeTransition
from framework.core.tool_manager import ToolResult
from framework.hook import HookPayload, HookPoint
from framework.runtime.enums import (
    OperationKind,
    OperationStatus,
    SnapshotReason,
    ToolBatchStatus,
    ToolCallStatus,
    TurnPhase,
)
from framework.runtime.models import ToolArguments, ToolBatchState, ToolCallState

if TYPE_CHECKING:
    from framework.agents.react.agent import ReActAgent


class ToolNode(Node):
    """Two-phase tool node: classify all -> strategy solicit -> batch execute."""

    def __init__(self, agent: ReActAgent) -> None:
        super().__init__(ReActNode.TOOL)
        self._agent = agent

    async def execute(self, ctx: AgentContext) -> NodeTransition:
        response = ctx.metadata.pop(ReActMetaKey.LLM_RESPONSE)
        tool_calls: list[ToolCall] = response.tool_calls
        max_tools = ctx_ext(ctx, ExtensionKey.MAX_TOOLS_PER_TURN)

        # ---- typed ReActTurnState path (new) ----
        react_state = self._get_react_state(ctx)
        if react_state is not None:
            react_state.current_node = ReActNode.TOOL

        if max_tools is not None and len(tool_calls) > max_tools:
            if ctx.emitter is not None:
                await ctx.emitter.emit(
                    ReActEvent.ERROR, f"Exceeded max_tools_per_turn ({max_tools})",
                )
            ctx.metadata[ReActMetaKey.END_REASON] = ReActReason.TURN_CANCELLED
            ctx.metadata[ReActMetaKey.CANCEL_REASON] = "max_tools_per_turn"
            return NodeTransition(ReActNode.END, ReActReason.TURN_CANCELLED)

        # Extract data for resume state (LLM_RESPONSE is now popped, capture before strategy reads)
        all_tc_dicts: list[dict[str, Any]] = [
            {
                "id": tc.call_id or "",
                "type": "function",
                "function": {
                    "name": tc.tool_name,
                    "arguments": tc.arguments or {},
                },
            }
            for tc in tool_calls
        ]
        llm_content = getattr(response, "content", "") or ""
        llm_reasoning = getattr(response, "reasoning_content", None)

        # Phase 1: classify all tools
        decisions = self._classify_all(tool_calls, ctx)

        # ---- typed: create ToolBatchState (new) ----
        if react_state is not None:
            call_states = [
                ToolCallState(
                    call_id=tc.call_id or uuid4().hex,
                    tool_name=tc.tool_name,
                    arguments=ToolArguments(values=tc.arguments or {}),
                )
                for tc in tool_calls
            ]
            batch = react_state.create_tool_batch(
                iteration=ctx.metadata[ReActMetaKey.ITERATION],
                calls=call_states,
            )
            # Update batch decisions from classifier
            for cs, dec in zip(batch.calls, decisions, strict=False):
                cs.decision = ApprovalDecision(dec) if isinstance(dec, str) else dec
                if dec == ApprovalDecision.ALLOWED:
                    cs.status = ToolCallStatus.ALLOWED
                elif dec == ApprovalDecision.DENIED:
                    cs.status = ToolCallStatus.DENIED

        # Phase 2: if any need approval, delegate to strategy
        if ApprovalDecision.PENDING in decisions:
            strategy = ctx.runtime.approval.suspend_strategy if ctx.runtime and ctx.runtime.approval else None
            if strategy is None:
                decisions = [
                    ApprovalDecision.ALLOWED if d == ApprovalDecision.PENDING else d
                    for d in decisions
                ]
            else:
                iteration = ctx.metadata[ReActMetaKey.ITERATION]
                requests = [
                    ApprovalRequest(
                        tool_name=tc.tool_name,
                        tool_call_id=tc.call_id or "",
                        arguments=tc.arguments or {},
                        tier=self._get_tier(tc, ctx),
                        iteration=iteration,
                    )
                    for tc, d in zip(tool_calls, decisions, strict=False)
                    if d == ApprovalDecision.PENDING
                ]
                resolved: list[str] = await strategy.solicit_approval(
                    requests, ctx,
                    all_tool_calls=all_tc_dicts,
                    llm_content=llm_content,
                    llm_reasoning=llm_reasoning,
                )
                decisions = self._merge(decisions, resolved)

        # Guard: ensure no PENDING remains before batch execution
        if ApprovalDecision.PENDING in decisions:
            logger.error(
                "ToolNode: unresolved PENDING decisions after strategy: %s", decisions,
            )
            ctx.metadata[ReActMetaKey.END_REASON] = ReActReason.TURN_CANCELLED
            ctx.metadata[ReActMetaKey.CANCEL_REASON] = "unresolved_approval"
            return NodeTransition(ReActNode.END, ReActReason.TURN_CANCELLED)

        # Phase 3: batch execute (all decisions resolved)
        return await self._execute_batch(tool_calls, decisions, ctx)

    # ---- typed state helpers ----

    @staticmethod
    def _get_react_state(ctx: AgentContext) -> Any:
        if ctx.identity is None or ctx.runtime is None:
            return None
        if not hasattr(ctx.runtime, "state"):
            return None
        from framework.agents.react.state import ReActTurnState
        state = ctx.runtime.state
        if isinstance(state, ReActTurnState):
            return state
        return None

    # ---- classification ----

    def _classify_all(
        self, tool_calls: list[ToolCall], ctx: AgentContext,
    ) -> list[str]:
        decisions: list[str] = []
        for tc in tool_calls:
            tier = self._get_tier(tc, ctx)
            logger.debug("ToolNode._classify_all: tool=%s tier=%s", tc.tool_name, tier)
            if tier == ApprovalTier.NORMAL:
                decisions.append(ApprovalDecision.ALLOWED)
            elif tier == ApprovalTier.HARDLINE:
                decisions.append(ApprovalDecision.DENIED)
            else:
                decisions.append(ApprovalDecision.PENDING)
        logger.info(
            "ToolNode._classify_all: %d tools -> %s",
            len(decisions),
            [d for d in decisions],
        )
        return decisions

    def _get_tier(self, tc: ToolCall, ctx: AgentContext) -> str:
        runtime = ctx.runtime
        if runtime and runtime.approval:
            return runtime.approval.classifier.classify(tc, ctx)
        return ApprovalTier.NORMAL

    def _merge(self, original: list[str], resolved: list[str]) -> list[str]:
        result = list(original)
        ri = 0
        for i in range(len(result)):
            if result[i] == ApprovalDecision.PENDING and ri < len(resolved):
                result[i] = resolved[ri]
                ri += 1
        return result

    # ---- batch execution ----

    async def _execute_batch(
        self, tool_calls: list[ToolCall], decisions: list[str], ctx: AgentContext,
    ) -> NodeTransition:
        if ctx.runtime and ctx.runtime.control:
            await ctx.runtime.control.drain(ctx, phase=ControlPhase.BEFORE_TOOL_BATCH)
        if ctx.emitter is not None:
            await ctx.emitter.emit(ReActEvent.PROGRESS, {
                "hint": self._format_hint(tool_calls), "tool_hint": True,
            })
        if ctx.runtime and ctx.runtime.hooks:
            await ctx.runtime.hooks.dispatch(
                HookPoint.BEFORE_TOOL_EXECUTION, ctx,
                payload=HookPayload(data={"tool_calls": tool_calls}),
            )

        approved_ids: set[str] = {
            tc.call_id or ""
            for tc, d in zip(tool_calls, decisions, strict=False)
            if d == ApprovalDecision.ALLOWED
        }
        ctx.metadata[ReActMetaKey.PRE_APPROVED_TOOL_IDS] = approved_ids
        try:
            denied_encountered = False
            for tc, dec in zip(tool_calls, decisions, strict=False):
                if denied_encountered and dec == ApprovalDecision.ALLOWED:
                    dec = ApprovalDecision.PREEMPTED

                if ctx.emitter is not None:
                    await ctx.emitter.emit(ReActEvent.TOOL_CALL_START, tc)

                if dec == ApprovalDecision.ALLOWED:
                    result = await self._agent._execute_tool(tc, ctx)
                else:
                    error_msg = f"Error: {dec}"
                    if dec == ApprovalDecision.DENIED:
                        deny_reason = ctx.metadata.get("APPROVAL_DENY_REASON")
                        if deny_reason:
                            error_msg = f"Error: {dec} ({deny_reason})"
                    result = ToolResult(
                        tool_name=tc.tool_name, result=None,
                        error=error_msg,
                    )

                if ctx.emitter is not None:
                    await ctx.emitter.emit(ReActEvent.TOOL_CALL_END, (tc, result))

                tool_msg = self._agent._build_tool_message(result, tc.call_id)
                await ctx.history.append(tool_msg)
                msgs: list = ctx.metadata.setdefault(ReActMetaKey.ITERATION_MSGS, [])
                msgs.append(tool_msg)
                await self._agent._save_checkpoint(msgs, ctx)

                # ---- typed: update ToolCallState result (new) ----
                react_state = self._get_react_state(ctx)
                if react_state is not None:
                    batch = react_state.active_tool_batch()
                    if batch is not None:
                        batch.status = ToolBatchStatus.RUNNING
                        for cs in batch.calls:
                            if cs.call_id == tc.call_id:
                                cs.result = result
                                cs.status = ToolCallStatus.COMPLETED if result.error is None else ToolCallStatus.FAILED
                                batch.operation_id = batch.operation_id

                if dec in (ApprovalDecision.DENIED, ApprovalDecision.PREEMPTED):
                    denied_encountered = True

            if ctx.runtime and ctx.runtime.hooks:
                await ctx.runtime.hooks.dispatch(
                    HookPoint.AFTER_TOOL_EXECUTION, ctx,
                    payload=HookPayload(data={
                        "results": [
                            m for m in ctx.metadata.get(ReActMetaKey.ITERATION_MSGS, [])
                            if isinstance(m, dict) and m.get("role") == "tool"
                        ],
                    }),
                )
            if ctx.runtime and ctx.runtime.injection_queue:
                await self._agent._drain_injections(ctx)

            if ctx.emitter is not None:
                await ctx.emitter.emit(ReActEvent.ITERATION_END, {
                    "iteration": ctx.metadata.get(ReActMetaKey.ITERATION),
                    "has_tool_calls": True,
                })

            # ---- typed: mark batch completed (new) ----
            react_state = self._get_react_state(ctx)
            if react_state is not None:
                batch = react_state.active_tool_batch()
                if batch is not None and not denied_encountered:
                    batch.status = ToolBatchStatus.COMPLETED
                    if batch.operation_id:
                        react_state.update_operation(batch.operation_id, OperationStatus.COMPLETED)

            if denied_encountered and ctx.metadata.get(ReActMetaKey.DENY_AS_CANCEL):
                await self._agent._save_denial_checkpoint(
                    list(ctx.metadata.get(ReActMetaKey.ITERATION_MSGS, [])),
                    ctx,
                )
                ctx.metadata[ReActMetaKey.END_REASON] = ReActReason.TURN_CANCELLED
                ctx.metadata[ReActMetaKey.CANCEL_REASON] = "approval_denied"
                return NodeTransition(ReActNode.END, ReActReason.TURN_CANCELLED)

            return NodeTransition(ReActNode.LLM, ReActReason.TOOLS_DONE)
        finally:
            ctx.metadata.pop(ReActMetaKey.PRE_APPROVED_TOOL_IDS, None)

    @staticmethod
    def _format_hint(tool_calls: list[ToolCall]) -> str:
        if not tool_calls:
            return "preparing tools..."
        if len(tool_calls) == 1:
            return f"calling {tool_calls[0].tool_name}..."
        names = ", ".join(tc.tool_name for tc in tool_calls)
        return f"calling tools: {names}..."
