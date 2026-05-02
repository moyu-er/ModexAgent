"""ToolNode — classify tools → strategy → batch execute."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

from framework.agents.react.agent import ReActEvent
from framework.agents.react.constants import ReActMetaKey, ReActNode, ReActReason
from framework.approval.constants import ApprovalDecision, ApprovalTier
from framework.approval.state import ApprovalRequest
from framework.core.agent import AgentContext, ctx_ext
from framework.core.context_extensions import ExtensionKey
from framework.core.emitter import ToolCall
from framework.core.graph.node import Node, NodeTransition
from framework.core.tool_manager import ToolResult
from framework.hook import HookPoint

if TYPE_CHECKING:
    from framework.agents.react.agent import ReActAgent


class ToolNode(Node):
    """Two-phase tool node: classify all → strategy solicit → batch execute."""

    def __init__(self, agent: ReActAgent, *,
                 enable_approval: bool = True,
                 enable_hooks: bool = True) -> None:
        super().__init__(ReActNode.TOOL)
        self._agent = agent
        self._enable_approval = enable_approval
        self._enable_hooks = enable_hooks

    async def execute(self, ctx: AgentContext) -> NodeTransition:
        response = ctx.metadata.pop(ReActMetaKey.LLM_RESPONSE)
        tool_calls: list[ToolCall] = response.tool_calls
        max_tools = ctx_ext(ctx, ExtensionKey.MAX_TOOLS_PER_TURN)

        if max_tools is not None and len(tool_calls) > max_tools:
            if ctx.emitter is not None:
                await ctx.emitter.emit(
                    ReActEvent.ERROR, f"Exceeded max_tools_per_turn ({max_tools})",
                )
            return NodeTransition(ReActNode.END, ReActReason.TURN_CANCELLED)

        # Phase 1: classify all tools
        decisions = self._classify_all(tool_calls, ctx)

        # Phase 2: if any need approval, delegate to strategy
        if self._enable_approval and ApprovalDecision.PENDING in decisions:
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
            strategy = ctx_ext(ctx, ExtensionKey.SUSPEND_STRATEGY)
            if strategy is not None:
                resolved = await strategy.solicit_approval(requests, ctx)
                decisions = self._merge(decisions, resolved)

        # Phase 3: batch execute
        return await self._execute_batch(tool_calls, decisions, ctx)

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
            "ToolNode._classify_all: %d tools → %s",
            len(decisions),
            [d for d in decisions],
        )
        return decisions

    def _get_tier(self, tc: ToolCall, ctx: AgentContext) -> str:
        interceptor_chain = ctx_ext(ctx, ExtensionKey.INTERCEPTOR_CHAIN)
        logger.debug(
            "ToolNode._get_tier: tool=%s interceptor_chain=%s interceptors=%d",
            tc.tool_name,
            type(interceptor_chain).__name__ if interceptor_chain is not None else None,
            len(getattr(interceptor_chain, "interceptors", [])),
        )
        if interceptor_chain is not None:
            for interceptor in getattr(interceptor_chain, "interceptors", []):
                classify_fn = getattr(interceptor, "classify_tier", None)
                logger.debug("  interceptor=%s classify_fn=%s", type(interceptor).__name__, classify_fn is not None)
                if classify_fn is not None:
                    tier = classify_fn(tc)
                    logger.info(
                        "ToolNode._get_tier: tool=%s args=%s → tier=%s",
                        tc.tool_name, dict(tc.arguments or {}), tier,
                    )
                    return tier
        logger.warning("ToolNode._get_tier: NO classify_tier found, defaulting to NORMAL for tool=%s", tc.tool_name)
        return ApprovalTier.NORMAL

    def _merge(self, original: list[str], resolved: list[str]) -> list[str]:
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
        if ctx.emitter is not None:
            await ctx.emitter.emit(ReActEvent.PROGRESS, {
                "hint": self._format_hint(tool_calls), "tool_hint": True,
            })
        if self._enable_hooks:
            await self._agent._call_hooks(HookPoint.BEFORE_TOOL_EXECUTION, ctx, tool_calls)

        denied_encountered = False
        for tc, dec in zip(tool_calls, decisions, strict=False):
            if denied_encountered:
                dec = ApprovalDecision.PREEMPTED

            if ctx.emitter is not None:
                await ctx.emitter.emit(ReActEvent.TOOL_CALL_START, tc)

            if dec == ApprovalDecision.ALLOWED:
                result = await self._agent._execute_tool(tc, ctx)
            else:
                result = ToolResult(
                    tool_name=tc.tool_name, result=None,
                    error=f"Error: {dec}",
                )

            if ctx.emitter is not None:
                await ctx.emitter.emit(ReActEvent.TOOL_CALL_END, (tc, result))

            tool_msg = self._agent._build_tool_message(result, tc.call_id)
            await ctx.history.append(tool_msg)
            msgs: list = ctx.metadata.setdefault(ReActMetaKey.ITERATION_MSGS, [])
            msgs.append(tool_msg)
            await self._agent._save_checkpoint(msgs, ctx)

            if dec in (ApprovalDecision.DENIED, ApprovalDecision.PREEMPTED):
                denied_encountered = True

        if self._enable_hooks:
            await self._agent._call_hooks(
                HookPoint.AFTER_TOOL_EXECUTION, ctx,
                [m for m in ctx.metadata.get(ReActMetaKey.ITERATION_MSGS, [])
                 if isinstance(m, dict) and m.get("role") == "tool"],
            )
            await self._agent._drain_injections(ctx)

        if ctx.emitter is not None:
            await ctx.emitter.emit(ReActEvent.ITERATION_END, {
                "iteration": ctx.metadata.get(ReActMetaKey.ITERATION),
                "has_tool_calls": True,
            })

        if denied_encountered and ctx.metadata.get(ReActMetaKey.DENY_AS_CANCEL):
            await self._agent._save_denial_checkpoint(ctx)
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
