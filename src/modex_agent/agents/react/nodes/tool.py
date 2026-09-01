"""ToolNode: classify tools, suspend for approval, then batch execute."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import Counter
from dataclasses import dataclass
from uuid import uuid4

from modex_agent.agents.react.constants import ReActEvent as GraphReActEvent
from modex_agent.agents.react.constants import (
    ReActHookPoint,
    ReActNode,
    ToolCallEndPayload,
)
from modex_agent.agents.react.context import get_agent_ctx
from modex_agent.agents.react.message_builder import build_tool_message
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.agents.react.tool_dedup import (
    StreakAction,
    StreakDecision,
    ToolCallDeduplicator,
)
from modex_agent.agents.react.tool_executor import ToolExecutor
from modex_agent.approval.constants import ApprovalDecision, ApprovalTier
from modex_agent.control.exceptions import AgentCancelledError
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import DefaultValues
from modex_agent.core.ids import next_call_id
from modex_agent.core.message import ChatMessage, TextPart
from modex_agent.core.tool_manager import ExecutionMode, ToolResult
from modex_agent.core.types import MessageRole, ToolCall
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


@dataclass(frozen=True, slots=True)
class _SettledResult:
    result: ToolResult


@dataclass(frozen=True, slots=True)
class _SettledCancellation:
    error: AgentCancelledError | asyncio.CancelledError


@dataclass(frozen=True, slots=True)
class _SettledFailure:
    error: Exception


type _SettledSlot = _SettledResult | _SettledCancellation | _SettledFailure


@dataclass(frozen=True, slots=True)
class _ToolSegment:
    mode: ExecutionMode
    indices: tuple[int, ...]


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

        decisions = self._classify_all(tool_calls, agent_ctx)
        await self._emit_batch(ctx, GraphReActEvent.TOOL_CALL_START, tool_calls)
        call_states = [
            ToolCallState(
                # Canonicalized above; the fallback only guards against future
                # refactors breaking that invariant — it must never crash a turn.
                call_id=tc.call_id or next_call_id(),
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
            state.phase = TurnPhase.FAILED
            self.deliver(None, ReActNode.AFTER, ctx)
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
        decisions = self._normalize_batch_decisions(
            [
                state.approval.decisions.get(call.call_id, ApprovalDecision.ALLOWED)
                for call in batch.calls
            ]
        )
        self._apply_decisions_to_batch(batch, decisions)

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
        batch = state.active_tool_batch()
        if batch is not None:
            self._apply_decisions_to_batch(batch, decisions)
        next_seq = state.custom.get(TurnCustomKey.TOOL_SEQ_COUNTER, 0)
        seq_for_index = {
            index: next_seq + index for index in range(len(tool_calls))
        }
        state.custom[TurnCustomKey.TOOL_SEQ_COUNTER] = next_seq + len(tool_calls)
        slots: list[_SettledSlot | None] = [None] * len(tool_calls)
        leader_for_index = list(range(len(tool_calls)))
        followers: dict[int, list[int]] = {}
        streak_actions: dict[int, StreakAction] = {}
        completed_indices: set[int] = set()
        active_indices: set[int] = set()
        cancellation_cleanup_indices: set[int] = set()
        completion_queue: asyncio.Queue[int] = asyncio.Queue()
        committed_results: list[ToolResult] = []
        commit_cursor = 0

        if self._deduplicator is not None:
            self._deduplicator.begin_step()

        async def commit_ready() -> None:
            nonlocal commit_cursor
            while commit_cursor < len(tool_calls):
                if commit_cursor not in completed_indices:
                    break
                match slots[commit_cursor]:
                    case _SettledResult(result=result):
                        tc = tool_calls[commit_cursor]
                        if result.call_id != tc.call_id:
                            msg = f"tool result call_id mismatch for {tc.tool_name}"
                            raise RuntimeError(msg)
                        tool_msg = build_tool_message(result, tc.call_id)
                        await agent_ctx.history.append(tool_msg)
                        state.message_delta.append(
                            MessageDelta(message=tool_msg, source=MessageDeltaSource.TOOL)
                        )
                        committed_results.append(result)
                        commit_cursor += 1
                    case None | _SettledCancellation() | _SettledFailure():
                        break

        async def settle_result(
            index: int,
            result: ToolResult,
            *,
            status: ToolCallStatus | None = None,
            propagate_followers: bool = True,
            register_result: bool = False,
        ) -> None:
            tc = tool_calls[index]
            stamped = (
                result
                if result.call_id == tc.call_id
                else result.model_copy(update={"call_id": tc.call_id})
            )
            slots[index] = _SettledResult(stamped)
            completed_indices.add(index)
            if batch is not None:
                call_state = batch.calls[index]
                call_state.result = stamped
                call_state.status = status or (
                    ToolCallStatus.COMPLETED
                    if stamped.error is None
                    else ToolCallStatus.FAILED
                )
            await ctx.runtime.emit(
                GraphReActEvent.TOOL_CALL_END,
                ToolCallEndPayload(
                    tool_call=tc,
                    result=stamped,
                    seq=seq_for_index[index],
                ),
                ctx,
            )
            if register_result and self._deduplicator is not None:
                self._deduplicator.register_result(
                    tc.tool_name,
                    tc.arguments or {},
                    stamped,
                )
            if propagate_followers:
                for follower_index in followers.get(index, []):
                    follower = tool_calls[follower_index]
                    follower_result = stamped.model_copy(
                        update={"call_id": follower.call_id}
                    )
                    await settle_result(
                        follower_index,
                        follower_result,
                        status=status,
                        propagate_followers=False,
                    )
            await commit_ready()

        def cancelled_result(index: int) -> ToolResult:
            tc = tool_calls[index]
            text = "<tool_cancelled>Tool call cancelled.</tool_cancelled>"
            tool = agent_ctx.tool_manager.get_tool(tc.tool_name)
            if tool is not None and tool.cancel_note:
                text = f"{text}\n{tool.cancel_note}"
            return ToolResult.from_text(tc.tool_name, text, call_id=tc.call_id)

        async def worker(index: int) -> None:
            active_indices.add(index)
            try:
                if batch is not None:
                    batch.calls[index].status = ToolCallStatus.EXECUTING
                tc = tool_calls[index]
                result = await self._tool_executor.execute(tc, agent_ctx)
                streak_action = streak_actions.get(index)
                if (
                    streak_action is not None
                    and streak_action.action == StreakDecision.REMIND
                ):
                    existing = result.message_content()
                    appended = (
                        f"{existing}\n{streak_action.reminder}"
                        if existing
                        else streak_action.reminder
                    )
                    result = result.model_copy(update={"content": [TextPart(text=appended)]})
                slots[index] = _SettledResult(result)
            except AgentCancelledError as error:
                slots[index] = _SettledCancellation(error)
                cancellation_cleanup_indices.add(index)
            except asyncio.CancelledError as error:
                slots[index] = _SettledCancellation(error)
                cancellation_cleanup_indices.add(index)
            except Exception as error:  # noqa: BLE001
                slots[index] = _SettledFailure(error)
            finally:
                active_indices.discard(index)
                completion_queue.put_nowait(index)

        async def handle_settled(indices: list[int]) -> tuple[bool, Exception | None]:
            cancellation_seen = False
            first_failure: Exception | None = None
            for index in indices:
                match slots[index]:
                    case _SettledResult(result=result):
                        await settle_result(index, result, register_result=True)
                    case _SettledCancellation():
                        cancellation_seen = True
                    case _SettledFailure(error=error):
                        if batch is not None:
                            batch.calls[index].status = ToolCallStatus.FAILED
                        if first_failure is None:
                            first_failure = error
                    case None:
                        msg = f"tool worker {index} settled without an outcome"
                        raise RuntimeError(msg)
            return cancellation_seen, first_failure

        async def invoke_on_cancel(indices: set[int]) -> None:
            for index in sorted(indices):
                tool = agent_ctx.tool_manager.get_tool(tool_calls[index].tool_name)
                if tool is None:
                    continue
                try:
                    await tool.on_cancel()
                except Exception:  # noqa: BLE001
                    logger.exception("Tool on_cancel failed for %s", tool.name)

        async def drain_failure(
            in_flight: dict[int, asyncio.Task[None]],
            first_failure: Exception,
        ) -> None:
            while in_flight:
                index = await completion_queue.get()
                task = in_flight.pop(index)
                await task
                try:
                    cancellation_seen, _ = await handle_settled([index])
                    if cancellation_seen:
                        cleanup_indices = set(cancellation_cleanup_indices)
                        await invoke_on_cancel(cleanup_indices)
                        cancellation_cleanup_indices.difference_update(cleanup_indices)
                except Exception:  # noqa: BLE001
                    logger.exception("Tool worker failed while draining batch failure")
            if batch is not None:
                if batch.operation_id:
                    state.update_operation(batch.operation_id, OperationStatus.FAILED)
                batch.status = ToolBatchStatus.FAILED
            state.phase = TurnPhase.FAILED
            raise first_failure

        async def cancel_in_flight(
            in_flight: dict[int, asyncio.Task[None]],
        ) -> None:
            # A worker cancelled between create_task() and its first step
            # never runs its finally block, so it never queues a completion
            # entry. Settle strictly from the task map (every cancelled task
            # is awaitable to completion) — waiting on queue entries that no
            # task will produce would hang the unified cancellation path.
            for task in in_flight.values():
                task.cancel()
            for index in sorted(in_flight):
                await in_flight[index]
                if index in completed_indices:
                    continue
                match slots[index]:
                    case _SettledResult(result=result):
                        await settle_result(index, result, register_result=True)
                    case _SettledCancellation() | _SettledFailure() | None:
                        continue
            in_flight.clear()
            while not completion_queue.empty():
                completion_queue.get_nowait()
            cleanup_indices = active_indices | cancellation_cleanup_indices
            await invoke_on_cancel(cleanup_indices)
            cancellation_cleanup_indices.difference_update(cleanup_indices)
            for index in range(len(tool_calls)):
                if index not in completed_indices:
                    await settle_result(
                        index,
                        cancelled_result(index),
                        status=ToolCallStatus.CANCELLED,
                        propagate_followers=False,
                    )

        async def finish_cancelled() -> None:
            if self._deduplicator is not None:
                self._deduplicator.end_step()
            if batch is not None:
                if batch.operation_id:
                    state.update_operation(batch.operation_id, OperationStatus.CANCELLED)
                batch.status = ToolBatchStatus.CANCELLED
            state.phase = TurnPhase.CANCELLED
            await ctx.runtime.dispatch_hook(
                ReActHookPoint.AFTER_TOOL_EXECUTION,
                ctx,
                data={"results": committed_results},
            )
            await ctx.runtime.emit(
                GraphReActEvent.ITERATION_END,
                {"iteration": state.iteration, "has_tool_calls": True},
                ctx,
            )
            self.deliver(None, ReActNode.AFTER, ctx)

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
        try:
            await ctx.runtime.drain_control(ctx)
        except AgentCancelledError:
            await cancel_in_flight({})
            await finish_cancelled()
            return None

        denied_encountered = False
        if self._deduplicator is not None:
            name_counts = Counter(tc.tool_name for tc in tool_calls)
            grouped_leaders: dict[tuple[str, ApprovalDecision], int] = {}
            for index, (tc, decision) in enumerate(
                zip(tool_calls, decisions, strict=False)
            ):
                if name_counts[tc.tool_name] < 2:
                    continue
                key = self._deduplicator.make_key(tc.tool_name, tc.arguments or {})
                group_key = (key, decision)
                leader = grouped_leaders.get(group_key)
                if leader is None:
                    grouped_leaders[group_key] = index
                    continue
                leader_for_index[index] = leader
                followers.setdefault(leader, []).append(index)

        streak_stop = False
        for index, (tc, decision) in enumerate(
            zip(tool_calls, decisions, strict=False)
        ):
            if leader_for_index[index] != index:
                continue
            if decision in (ApprovalDecision.DENIED, ApprovalDecision.PREEMPTED):
                denied_encountered = True
                await settle_result(
                    index,
                    ToolResult(
                        tool_name=tc.tool_name,
                        error=self._denial_message(decision, tc, state),
                    ),
                )
                continue
            if self._deduplicator is None:
                continue
            streak_action = self._deduplicator.check_streak(
                tc.tool_name,
                tc.arguments or {},
            )
            streak_actions[index] = streak_action
            match streak_action.action:
                case StreakDecision.CONTINUE | StreakDecision.REMIND:
                    continue
                case StreakDecision.SKIP:
                    await settle_result(
                        index,
                        ToolResult.from_text(tc.tool_name, streak_action.reminder),
                        register_result=True,
                    )
                case StreakDecision.STOP:
                    await settle_result(
                        index,
                        ToolResult.from_text(tc.tool_name, streak_action.reminder),
                        propagate_followers=False,
                        register_result=True,
                    )
                    streak_stop = True
                    break

        if streak_stop:
            await cancel_in_flight({})
            await finish_cancelled()
            return None

        segments: list[_ToolSegment] = []
        parallel_indices: list[int] = []
        for index, tc in enumerate(tool_calls):
            if leader_for_index[index] != index or slots[index] is not None:
                continue
            tool = agent_ctx.tool_manager.get_tool(tc.tool_name)
            mode = tool.execution_mode if tool is not None else ExecutionMode.EXCLUSIVE
            match mode:
                case ExecutionMode.PARALLEL:
                    parallel_indices.append(index)
                case ExecutionMode.EXCLUSIVE:
                    if parallel_indices:
                        segments.append(
                            _ToolSegment(
                                mode=ExecutionMode.PARALLEL,
                                indices=tuple(parallel_indices),
                            )
                        )
                        parallel_indices = []
                    segments.append(
                        _ToolSegment(mode=ExecutionMode.EXCLUSIVE, indices=(index,))
                    )
        if parallel_indices:
            segments.append(
                _ToolSegment(
                    mode=ExecutionMode.PARALLEL,
                    indices=tuple(parallel_indices),
                )
            )

        configured_max = (
            agent_ctx.runtime.state.custom.get(TurnCustomKey.MAX_PARALLEL_TOOL_CALLS)
            if agent_ctx.runtime is not None
            else None
        )
        if configured_max is None:
            max_parallel = DefaultValues.MAX_PARALLEL_TOOL_CALLS
        elif (
            isinstance(configured_max, bool)
            or not isinstance(configured_max, int)
            or configured_max < 1
        ):
            msg = "max_parallel_tool_calls must be an integer >= 1"
            raise ValueError(msg)
        else:
            max_parallel = configured_max

        for segment_index, segment in enumerate(segments):
            if segment_index > 0:
                try:
                    await ctx.runtime.drain_control(ctx)
                except AgentCancelledError:
                    await cancel_in_flight({})
                    await finish_cancelled()
                    return None
            limit = max_parallel if segment.mode == ExecutionMode.PARALLEL else 1
            next_to_start = 0
            in_flight: dict[int, asyncio.Task[None]] = {}
            try:
                while next_to_start < len(segment.indices) or in_flight:
                    while (
                        next_to_start < len(segment.indices)
                        and len(in_flight) < limit
                    ):
                        index = segment.indices[next_to_start]
                        task = asyncio.create_task(worker(index))
                        in_flight[index] = task
                        next_to_start += 1
                    settled_index = await completion_queue.get()
                    task = in_flight[settled_index]
                    await task
                    in_flight.pop(settled_index)
                    cancellation_seen = False
                    first_failure: Exception | None = None
                    try:
                        cancellation_seen, first_failure = await handle_settled(
                            [settled_index]
                        )
                    except Exception as error:  # noqa: BLE001
                        await drain_failure(in_flight, error)
                    if first_failure is not None:
                        await drain_failure(in_flight, first_failure)
                    if cancellation_seen:
                        await cancel_in_flight(in_flight)
                        await finish_cancelled()
                        return None
            except asyncio.CancelledError:
                # `/stop`, WebUI pause, and busy-input interruption wake the
                # owning turn task. Converge that active cancellation with the
                # channel/streak path so tool-owned resources are interrupted
                # and every pending tool_call receives a result message.
                await cancel_in_flight(in_flight)
                with contextlib.suppress(AgentCancelledError):
                    await ctx.runtime.drain_control(ctx)
                await finish_cancelled()
                return None
            finally:
                for task in in_flight.values():
                    task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.gather(*in_flight.values(), return_exceptions=True)

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
            data={"results": committed_results},
        )
        await ctx.runtime.emit(
            GraphReActEvent.ITERATION_END,
            {"iteration": state.iteration, "has_tool_calls": True},
            ctx,
        )

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
                self.deliver(None, ReActNode.AFTER, ctx)
                return None

        self.deliver(None, ReActNode.LLM, ctx)
        return None

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
