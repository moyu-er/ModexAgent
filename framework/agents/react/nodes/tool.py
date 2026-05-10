"""ToolNode — classify tools -> strategy -> batch execute."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import uuid4

logger = logging.getLogger(__name__)

from framework.agents.react.agent import ReActEvent
from framework.agents.react.constants import ReActNode, ReActReason
from framework.agents.react.state import get_react_state
from framework.approval.constants import ApprovalDecision, ApprovalTier
from framework.approval.state import ApprovalRequest
from framework.control.runtime import ControlPhase
from framework.core.agent import AgentContext
from framework.core.context_extensions import ExtensionKey
from framework.core.emitter import ToolCall
from framework.core.graph.node import Node, NodeTransition
from framework.core.tool_manager import ToolResult
from framework.hook import HookPayload, HookPoint
from framework.runtime.enums import (
    OperationKind,
    OperationStatus,
    ApprovalDenyPolicy,
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
        state = get_react_state(ctx)
        if state is None or state.llm_response is None:
            return NodeTransition(ReActNode.END, ReActReason.LLM_ERROR)

        response = state.llm_response
        tool_calls: list[ToolCall] = response.tool_calls
        state.llm_response = None
        state.current_node = ReActNode.TOOL

        max_tools = ctx.extensions.get(ExtensionKey.MAX_TOOLS_PER_TURN)
        if max_tools is not None and len(tool_calls) > max_tools:
            if ctx.emitter is not None:
                await ctx.emitter.emit(ReActEvent.ERROR, f"Exceeded max_tools_per_turn ({max_tools})")
            return NodeTransition(ReActNode.END, ReActReason.TURN_CANCELLED)

        # Phase 1: classify all tools
        decisions = self._classify_all(tool_calls, ctx)

        # Create typed ToolBatchState
        call_states = [
            ToolCallState(
                call_id=tc.call_id or uuid4().hex,
                tool_name=tc.tool_name,
                arguments=ToolArguments(values=tc.arguments or {}),
            )
            for tc in tool_calls
        ]
        batch = state.create_tool_batch(iteration=state.iteration, calls=call_states)
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
                for cs, dec in zip(batch.calls, decisions, strict=False):
                    cs.decision = ApprovalDecision(dec) if isinstance(dec, str) else dec
                    if dec == ApprovalDecision.ALLOWED:
                        cs.status = ToolCallStatus.ALLOWED
            else:
                requests = [
                    ApprovalRequest(
                        tool_name=tc.tool_name,
                        tool_call_id=tc.call_id or "",
                        arguments=tc.arguments or {},
                        tier=self._get_tier(tc, ctx),
                        iteration=state.iteration,
                    )
                    for tc, d in zip(tool_calls, decisions, strict=False)
                    if d == ApprovalDecision.PENDING
                ]
                all_tc_dicts = [
                    {"id": tc.call_id or "", "type": "function",
                     "function": {"name": tc.tool_name, "arguments": tc.arguments or {}}}
                    for tc in tool_calls
                ]
                llm_content = getattr(response, "content", "") or ""
                llm_reasoning = getattr(response, "reasoning_content", None)
                resolved: list[str] = await strategy.solicit_approval(
                    requests, ctx,
                    all_tool_calls=all_tc_dicts,
                    llm_content=llm_content,
                    llm_reasoning=llm_reasoning,
                )
                decisions = self._merge(decisions, resolved)

        # Guard: ensure no PENDING remains
        if ApprovalDecision.PENDING in decisions:
            logger.error("ToolNode: unresolved PENDING decisions: %s", decisions)
            return NodeTransition(ReActNode.END, ReActReason.TURN_CANCELLED)

        # Phase 3: batch execute
        return await self._execute_batch(tool_calls, decisions, ctx)

    def _classify_all(
        self, tool_calls: list[ToolCall], ctx: AgentContext,
    ) -> list[str]:
        decisions: list[str] = []
        for tc in tool_calls:
            tier = self._get_tier(tc, ctx)
            if tier == ApprovalTier.NORMAL:
                decisions.append(ApprovalDecision.ALLOWED)
            elif tier == ApprovalTier.HARDLINE:
                decisions.append(ApprovalDecision.DENIED)
            else:
                decisions.append(ApprovalDecision.PENDING)
        return decisions

    def _get_tier(self, tc: ToolCall, ctx: AgentContext) -> str:
        runtime = ctx.runtime
        if runtime and runtime.approval:
            return runtime.approval.classifier.classify(tc, ctx)
        return ApprovalTier.NORMAL

    @staticmethod
    def _merge(original: list[str], resolved: list[str]) -> list[str]:
        result = list(original)
        ri = 0
        for i in range(len(result)):
            if result[i] == ApprovalDecision.PENDING and ri < len(resolved):
                result[i] = resolved[ri]
                ri += 1
        return result

    async def _execute_batch(
        self, tool_calls: list[ToolCall], decisions: list[str], ctx: AgentContext,
    ) -> NodeTransition:
        state = get_react_state(ctx)
        if ctx.runtime and ctx.runtime.control:
            await ctx.runtime.control.drain(ctx, phase=ControlPhase.BEFORE_TOOL_BATCH)
        if ctx.emitter is not None:
            await ctx.emitter.emit(ReActEvent.PROGRESS, {"hint": self._format_hint(tool_calls), "tool_hint": True})
        if ctx.runtime and ctx.runtime.hooks:
            await ctx.runtime.hooks.dispatch(
                HookPoint.BEFORE_TOOL_EXECUTION, ctx,
                payload=HookPayload(data={"tool_calls": tool_calls}),
            )

        batch = state.active_tool_batch() if state else None

        denied_encountered = False
        for tc, dec in zip(tool_calls, decisions, strict=False):
            if denied_encountered and dec == ApprovalDecision.ALLOWED:
                dec = ApprovalDecision.PREEMPTED

            if ctx.emitter is not None:
                await ctx.emitter.emit(ReActEvent.TOOL_CALL_START, tc)

            if dec == ApprovalDecision.ALLOWED:
                result = await self._agent._execute_tool(tc, ctx)
            else:
                deny_policy = ApprovalDenyPolicy.CANCEL_TURN
                if ctx.runtime and ctx.runtime.approval:
                    deny_policy = ctx.runtime.approval.default_deny_policy
                error_msg = f"Error: {dec}"
                if dec == ApprovalDecision.DENIED:
                    if deny_policy != ApprovalDenyPolicy.TOOL_RESULT_ONLY:
                        pass
                    error_msg = f"Error: {dec}"
                result = ToolResult(tool_name=tc.tool_name, result=None, error=error_msg)

            if ctx.emitter is not None:
                await ctx.emitter.emit(ReActEvent.TOOL_CALL_END, (tc, result))

            tool_msg = self._agent._build_tool_message(result, tc.call_id)
            await ctx.history.append(tool_msg)
            if state is not None:
                from framework.memory.core.message import ChatMessage
                from framework.runtime.models import MessageDelta
                from framework.runtime.enums import MessageDeltaSource
                cm = ChatMessage.from_dict(tool_msg) if isinstance(tool_msg, dict) else ChatMessage(role="tool", content=str(result.result or result.error or ""))
                state.message_delta.append(MessageDelta(message=cm, source=MessageDeltaSource.TOOL))

            if batch is not None:
                for cs in batch.calls:
                    if cs.call_id == tc.call_id:
                        cs.result = result
                        cs.status = ToolCallStatus.COMPLETED if result.error is None else ToolCallStatus.FAILED

            if dec in (ApprovalDecision.DENIED, ApprovalDecision.PREEMPTED):
                denied_encountered = True

        if state is not None and batch is not None:
            if batch.operation_id:
                state.update_operation(batch.operation_id, OperationStatus.COMPLETED)
            batch.status = ToolBatchStatus.COMPLETED if not denied_encountered else ToolBatchStatus.FAILED

        if ctx.runtime and ctx.runtime.hooks:
            await ctx.runtime.hooks.dispatch(HookPoint.AFTER_TOOL_EXECUTION, ctx)

        if ctx.emitter is not None:
            await ctx.emitter.emit(ReActEvent.ITERATION_END, {
                "iteration": state.iteration if state else 0, "has_tool_calls": True,
            })

        if denied_encountered:
            deny_policy = ApprovalDenyPolicy.CANCEL_TURN
            if ctx.runtime and ctx.runtime.approval:
                deny_policy = ctx.runtime.approval.default_deny_policy
            if deny_policy == ApprovalDenyPolicy.CANCEL_TURN:
                return NodeTransition(ReActNode.END, ReActReason.TURN_CANCELLED)

        return NodeTransition(ReActNode.LLM, ReActReason.TOOLS_DONE)

    @staticmethod
    def _format_hint(tool_calls: list[ToolCall]) -> str:
        if not tool_calls:
            return "preparing tools..."
        if len(tool_calls) == 1:
            return f"calling {tool_calls[0].tool_name}..."
        names = ", ".join(tc.tool_name for tc in tool_calls)
        return f"calling tools: {names}..."
