"""ToolNode: classify tools once, suspend for approval, then batch execute."""

from __future__ import annotations

import logging
from uuid import uuid4

from modex_agent.agents.react.constants import ReActEvent as GraphReActEvent
from modex_agent.agents.react.constants import ReActNode
from modex_agent.agents.react.context import get_agent_ctx
from modex_agent.agents.react.ids import next_call_id
from modex_agent.agents.react.nodes.tool_classification import (
    decisions_of,
    record_guard_audit,
)
from modex_agent.agents.react.nodes.tool_settlement import (
    apply_decisions_to_batch,
    normalize_batch_decisions,
    run_tool_batch,
)
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.agents.react.tool_dedup import ToolCallDeduplicator
from modex_agent.agents.react.tool_executor import ToolExecutor
from modex_agent.approval.classification import ToolClassification
from modex_agent.approval.constants import ApprovalDecision, ApprovalTier
from modex_agent.core.agent import AgentContext
from modex_agent.core.message import ChatMessage, MessageRole, ToolCall
from modex_agent.core.tool_manager import ToolResult
from modex_agent.runtime.enums import (
    ApprovalSubjectType,
    SnapshotReason,
    ToolBatchStatus,
    ToolCallStatus,
    TurnCustomKey,
    TurnPhase,
)
from modex_agent.runtime.models import (
    ApprovalRequestState,
    ApprovalTransaction,
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
        self.tool_executor = tool_executor
        self.deduplicator = deduplicator

    def agent_ctx_of(self, ctx: GraphContext[ReActTurnState]) -> AgentContext:
        return get_agent_ctx(ctx)

    async def execute(
        self,
        ctx: GraphContext[ReActTurnState],
        integrated_input: IntegratedInput,
    ) -> None:
        state = ctx.state
        if state.phase == TurnPhase.SUSPENDED:
            await self._resume_suspended_batch(ctx)
            return None

        agent_ctx = get_agent_ctx(ctx)

        # Deliver-ized: LLMNode appends the assistant message to history;
        # ToolNode reads tool_calls from the last assistant message here.
        history_messages = await agent_ctx.history.to_list()
        last_assistant: ChatMessage | None = None
        for msg in reversed(history_messages):
            if msg.role == MessageRole.ASSISTANT:
                last_assistant = msg
                break

        if last_assistant is None or not last_assistant.tool_calls:
            state.phase = TurnPhase.FAILED
            self.deliver(None, ReActNode.AFTER, ctx)
            return None

        tool_calls: list[ToolCall] = last_assistant.tool_calls
        state.current_node = ReActNode.TOOL

        # LLMNode canonicalizes call ids before the assistant message is
        # written; this re-check is the defensive fallback for paths that
        # bypass LLMNode (future refactors) — it must never crash a turn.
        tool_calls = [
            tc if tc.call_id else tc.model_copy(update={"call_id": next_call_id()})
            for tc in tool_calls
        ]
        max_tools = (
            agent_ctx.runtime.state.custom.get(TurnCustomKey.MAX_TOOLS_PER_TURN)
            if agent_ctx.runtime
            else None
        )
        if (
            max_tools is not None
            and isinstance(max_tools, int | float)
            and len(tool_calls) > max_tools
        ):
            await ctx.runtime.emit(
                GraphReActEvent.ERROR,
                f"Exceeded max_tools_per_turn ({max_tools})",
                ctx,
            )
            state.phase = TurnPhase.FAILED
            self.deliver(None, ReActNode.AFTER, ctx)
            return None

        # THE single classification pass: one classify per tool call. Every
        # downstream artifact — decisions, denial copy, audit rows — derives
        # from these stored values; nothing re-classifies.
        classifications = self._classify_all(tool_calls, agent_ctx)
        decisions = decisions_of(classifications)
        await record_guard_audit(classifications, tool_calls, ctx)
        await self._emit_batch(ctx, GraphReActEvent.TOOL_CALL_START, tool_calls)
        # A hard denial is already a result; persist it before any sibling suspends.
        call_states = [
            ToolCallState(
                # Canonicalized above; the fallback only guards against future
                # refactors breaking that invariant — it must never crash a turn.
                call_id=tc.call_id or next_call_id(),
                tool_name=tc.tool_name,
                arguments=ToolArguments(values=tc.arguments or {}),
                result=ToolResult(
                    tool_name=tc.tool_name,
                    call_id=tc.call_id,
                    error=classification.reason
                    or f"Denied by policy: '{tc.tool_name}' is not allowed.",
                )
                if classification.tier is ApprovalTier.HARDLINE
                else None,
            )
            for tc, classification in zip(tool_calls, classifications, strict=True)
        ]
        batch = state.create_tool_batch(iteration=state.iteration, calls=call_states)
        apply_decisions_to_batch(batch, decisions)

        if ApprovalDecision.PENDING in decisions:
            await self._suspend_for_approval(batch, tool_calls, decisions, classifications, ctx)
            return None

        await run_tool_batch(
            self,
            ctx,
            tool_calls=tool_calls,
            decisions=decisions,
            deny_reasons=[
                call.result.error if call.result is not None else None for call in batch.calls
            ],
        )
        return None

    async def _suspend_for_approval(
        self,
        batch: ToolBatchState,
        tool_calls: list[ToolCall],
        decisions: list[ApprovalDecision],
        classifications: list[ToolClassification],
        ctx: GraphContext[ReActTurnState],
    ) -> None:
        state = ctx.state
        agent_ctx = get_agent_ctx(ctx)
        if agent_ctx.runtime is None or agent_ctx.runtime.turn_store is None:
            logger.error("ToolNode: approval required but no TurnStateStore configured")
            state.phase = TurnPhase.FAILED
            self.deliver(None, ReActNode.AFTER, ctx)
            return None

        approval_id = uuid4().hex
        requests: list[ApprovalRequestState] = []
        for tc, call_state, decision, classification in zip(
            tool_calls, batch.calls, decisions, classifications, strict=False
        ):
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
                    tier=classification.tier,
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
            state.phase = TurnPhase.FAILED
            self.deliver(None, ReActNode.AFTER, ctx)
            return None
        batch = state.active_tool_batch()
        if batch is None:
            state.phase = TurnPhase.FAILED
            self.deliver(None, ReActNode.AFTER, ctx)
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
        decisions = normalize_batch_decisions(
            [
                call.decision
                if call.decision is ApprovalDecision.DENIED
                or call.decision is ApprovalDecision.PREEMPTED
                else state.approval.decisions.get(
                    call.call_id, call.decision or ApprovalDecision.ALLOWED
                )
                for call in batch.calls
            ]
        )
        apply_decisions_to_batch(batch, decisions)

        state.phase = TurnPhase.RUNNING
        state.current_node = ReActNode.TOOL
        await run_tool_batch(
            self,
            ctx,
            tool_calls=tool_calls,
            decisions=decisions,
            deny_reasons=[
                call.result.error if call.result is not None else None for call in batch.calls
            ],
        )
        return None

    def _classify_all(
        self,
        tool_calls: list[ToolCall],
        ctx: AgentContext,
    ) -> list[ToolClassification]:
        """One classification per call; NORMAL when no approval runtime exists."""
        runtime = ctx.runtime
        if runtime is None or runtime.approval is None:
            return [ToolClassification.tier_result(ApprovalTier.NORMAL) for _ in tool_calls]
        classifier = runtime.approval.classifier
        return [classifier.classify(tc, ctx) for tc in tool_calls]

    @staticmethod
    def denial_message(
        decision: ApprovalDecision,
        tc: ToolCall,
        state: ReActTurnState,
        captured_reason: str | None = None,
    ) -> str:
        """Clear, firm message for a denied/preempted tool call.

        Fed back to the agent as the tool result so it understands THIS
        SPECIFIC INVOCATION was rejected and must not be retried.

        Two denial origins (unified-security Ticket 05b): a USER decision
        (post-suspension, ``state.approval`` carries the deny_reason) keeps
        the user-addressed copy below. A CLASSIFICATION denial (HARDLINE
        tier decided pre-execution, no suspension ever happened —
        ``state.approval`` is None) renders the classification's deny
        reason (``captured_reason``): the guard's boundary/deny copy (or
        the delegation two-part message on subagents) is the actionable
        text, and claiming "rejected by the user" would be false.
        """
        if captured_reason:
            return captured_reason
        if decision == ApprovalDecision.PREEMPTED:
            return (
                f"Skipped: this specific call to '{tc.tool_name}' was not "
                f"allowed because another tool call in the same batch was "
                f"denied. The tool itself is not banned, but do "
                f"not retry this invocation."
            )
        if state.approval is None:
            return f"Denied by policy: '{tc.tool_name}' is not allowed."
        reason = state.approval.deny_reason
        detail = f" Reason: {reason}." if reason else ""
        return (
            f"Denied by user: this specific call to '{tc.tool_name}' was "
            f"explicitly rejected by the user.{detail} The tool itself is not "
            f"banned, but this invocation is not allowed and must not be "
            f"retried. Acknowledge the rejection and ask the user how to "
            f"proceed."
        )

    def format_hint(self, tool_calls: list[ToolCall]) -> str:
        if not tool_calls:
            return "preparing tools..."
        if len(tool_calls) == 1:
            return f"calling {tool_calls[0].tool_name}..."
        names = ", ".join(tc.tool_name for tc in tool_calls)
        return f"calling tools: {names}..."

    async def _emit_batch(
        self,
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
