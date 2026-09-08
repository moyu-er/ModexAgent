"""Tool batch settlement — the scheduling/cancellation state machine.

One :class:`ToolBatchExecution` instance owns one batch: parallel segments
and exclusive barriers, a bounded rolling pool settling through a
completion queue, model-order commit cursor, streak pruning, and the
converged channel/streak cancellation path. It consumes stored approval
decisions and denial reasons rather than reclassifying tool calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from modex_agent.agents.react.constants import ReActEvent as GraphReActEvent
from modex_agent.agents.react.constants import (
    ReActHookPoint,
    ReActNode,
    ToolCallEndPayload,
)
from modex_agent.agents.react.message_builder import build_tool_message
from modex_agent.agents.react.tool_dedup import (
    StreakAction,
    StreakDecision,
)
from modex_agent.approval.constants import ApprovalDecision
from modex_agent.control.exceptions import AgentCancelledError
from modex_agent.core.message import TextPart, ToolCall
from modex_agent.core.tool_manager import ExecutionMode, ToolResult
from modex_agent.runtime.enums import (
    ApprovalDenyPolicy,
    MessageDeltaSource,
    OperationStatus,
    ToolBatchStatus,
    ToolCallStatus,
    TurnCustomKey,
    TurnPhase,
)
from modex_agent.runtime.models import MessageDelta, ToolBatchState
from modex_graph.context import GraphContext

if TYPE_CHECKING:
    from modex_agent.agents.react.nodes.tool import ToolNode
    from modex_agent.agents.react.state import ReActTurnState

logger = logging.getLogger(__name__)

_DEFAULT_MAX_PARALLEL_TOOL_CALLS = 5


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


def normalize_batch_decisions(
    decisions: list[ApprovalDecision],
) -> list[ApprovalDecision]:
    """One denial preempts every ALLOWED call in the same batch."""
    has_denial = any(
        decision in (ApprovalDecision.DENIED, ApprovalDecision.PREEMPTED) for decision in decisions
    )
    if not has_denial:
        return decisions
    return [
        ApprovalDecision.PREEMPTED if decision == ApprovalDecision.ALLOWED else decision
        for decision in decisions
    ]


def apply_decisions_to_batch(
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


class ToolBatchExecution:
    """One batch's execution — the indivisible settle/commit/cancel machine."""

    def __init__(
        self,
        node: ToolNode,
        ctx: GraphContext[ReActTurnState],
        *,
        tool_calls: list[ToolCall],
        decisions: list[ApprovalDecision],
        deny_reasons: list[str | None] | None = None,
    ) -> None:
        self._node = node
        self._ctx = ctx
        self._tool_calls = tool_calls
        self._decisions = decisions
        self._deny_reasons = deny_reasons
        self._state = ctx.state
        self._agent_ctx = node.agent_ctx_of(ctx)
        self._batch = self._state.active_tool_batch()
        next_seq = self._state.custom.get(TurnCustomKey.TOOL_SEQ_COUNTER, 0)
        self._seq_for_index = {index: next_seq + index for index in range(len(tool_calls))}
        self._state.custom[TurnCustomKey.TOOL_SEQ_COUNTER] = next_seq + len(tool_calls)
        self._slots: list[_SettledSlot | None] = [None] * len(tool_calls)
        self._leader_for_index = list(range(len(tool_calls)))
        self._followers: dict[int, list[int]] = {}
        self._streak_actions: dict[int, StreakAction] = {}
        self._completed_indices: set[int] = set()
        self._active_indices: set[int] = set()
        self._cancellation_cleanup_indices: set[int] = set()
        self._completion_queue: asyncio.Queue[int] = asyncio.Queue()
        self._committed_results: list[ToolResult] = []
        self._commit_cursor = 0
        self._denied_encountered = False

    async def run(self) -> None:
        if self._node.deduplicator is not None:
            self._node.deduplicator.begin_step()
        await self._ctx.runtime.emit(
            GraphReActEvent.PROGRESS,
            {"hint": self._node.format_hint(self._tool_calls), "tool_hint": True},
            self._ctx,
        )
        await self._ctx.runtime.dispatch_hook(
            ReActHookPoint.BEFORE_TOOL_EXECUTION,
            self._ctx,
            data={"tool_calls": self._tool_calls},
        )
        try:
            await self._ctx.runtime.drain_control(self._ctx)
        except AgentCancelledError:
            await self._cancel_in_flight({})
            await self._finish_cancelled()
            return None

        self._prune_same_step_duplicates()
        streak_stop = await self._settle_denials_and_streaks()
        if streak_stop:
            await self._cancel_in_flight({})
            await self._finish_cancelled()
            return None

        if await self._run_segments():
            return None

        if self._node.deduplicator is not None:
            self._node.deduplicator.end_step()

        if self._batch is not None:
            if self._batch.operation_id:
                self._state.update_operation(self._batch.operation_id, OperationStatus.COMPLETED)
            self._batch.status = (
                ToolBatchStatus.FAILED if self._denied_encountered else ToolBatchStatus.COMPLETED
            )

        await self._ctx.runtime.dispatch_hook(
            ReActHookPoint.AFTER_TOOL_EXECUTION,
            self._ctx,
            data={"results": self._committed_results},
        )
        await self._ctx.runtime.emit(
            GraphReActEvent.ITERATION_END,
            {"iteration": self._state.iteration, "has_tool_calls": True},
            self._ctx,
        )

        if self._denied_encountered:
            # EXTENSION POINT: whether denial cancels the ReAct turn is
            # configured by the runtime's ApprovalDenyPolicy.
            deny_policy = self._deny_policy()
            if deny_policy == ApprovalDenyPolicy.CANCEL_TURN:
                self._state.phase = TurnPhase.CANCELLED
                self._node.deliver(None, ReActNode.AFTER, self._ctx)
                return None

        self._node.deliver(None, ReActNode.LLM, self._ctx)

    def _deny_policy(self) -> ApprovalDenyPolicy:
        approval = self._agent_ctx.runtime.approval if self._agent_ctx.runtime else None
        if approval is None:
            return ApprovalDenyPolicy.TOOL_RESULT_ONLY
        return approval.default_deny_policy

    def _prune_same_step_duplicates(self) -> None:
        deduplicator = self._node.deduplicator
        if deduplicator is None:
            return
        name_counts = Counter(tc.tool_name for tc in self._tool_calls)
        grouped_leaders: dict[tuple[str, ApprovalDecision], int] = {}
        for index, (tc, decision) in enumerate(
            zip(self._tool_calls, self._decisions, strict=False)
        ):
            if name_counts[tc.tool_name] < 2:
                continue
            key = deduplicator.make_key(tc.tool_name, tc.arguments or {})
            group_key = (key, decision)
            leader = grouped_leaders.get(group_key)
            if leader is None:
                grouped_leaders[group_key] = index
                continue
            self._leader_for_index[index] = leader
            self._followers.setdefault(leader, []).append(index)

    async def _settle_denials_and_streaks(self) -> bool:
        """Settle denied/preempted calls and streak decisions; True = stop."""
        deduplicator = self._node.deduplicator
        for index, (tc, decision) in enumerate(
            zip(self._tool_calls, self._decisions, strict=False)
        ):
            if self._leader_for_index[index] != index:
                continue
            if decision in (ApprovalDecision.DENIED, ApprovalDecision.PREEMPTED):
                self._denied_encountered = True
                reason = self._deny_reasons[index] if self._deny_reasons is not None else None
                await self._settle_result(
                    index,
                    ToolResult(
                        tool_name=tc.tool_name,
                        error=self._node.denial_message(decision, tc, self._state, reason),
                    ),
                )
                continue
            if deduplicator is None:
                continue
            streak_action = deduplicator.check_streak(
                tc.tool_name,
                tc.arguments or {},
            )
            self._streak_actions[index] = streak_action
            match streak_action.action:
                case StreakDecision.CONTINUE | StreakDecision.REMIND:
                    continue
                case StreakDecision.SKIP:
                    await self._settle_result(
                        index,
                        ToolResult.from_text(tc.tool_name, streak_action.reminder),
                        register_result=True,
                    )
                case StreakDecision.STOP:
                    await self._settle_result(
                        index,
                        ToolResult.from_text(tc.tool_name, streak_action.reminder),
                        propagate_followers=False,
                        register_result=True,
                    )
                    return True
        return False

    async def _run_segments(self) -> bool:
        segments = self._plan_segments()
        max_parallel = self._resolve_max_parallel()
        for segment_index, segment in enumerate(segments):
            if segment_index > 0:
                try:
                    await self._ctx.runtime.drain_control(self._ctx)
                except AgentCancelledError:
                    await self._cancel_in_flight({})
                    await self._finish_cancelled()
                    return True
            limit = max_parallel if segment.mode == ExecutionMode.PARALLEL else 1
            if await self._run_segment(segment, limit):
                return True
        return False

    def _plan_segments(self) -> list[_ToolSegment]:
        segments: list[_ToolSegment] = []
        parallel_indices: list[int] = []
        for index, tc in enumerate(self._tool_calls):
            if self._leader_for_index[index] != index or self._slots[index] is not None:
                continue
            tool = self._agent_ctx.tool_manager.get_tool(tc.tool_name)
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
                    segments.append(_ToolSegment(mode=ExecutionMode.EXCLUSIVE, indices=(index,)))
        if parallel_indices:
            segments.append(
                _ToolSegment(
                    mode=ExecutionMode.PARALLEL,
                    indices=tuple(parallel_indices),
                )
            )
        return segments

    def _resolve_max_parallel(self) -> int:
        configured_max = (
            self._agent_ctx.runtime.state.custom.get(TurnCustomKey.MAX_PARALLEL_TOOL_CALLS)
            if self._agent_ctx.runtime is not None
            else None
        )
        if configured_max is None:
            return _DEFAULT_MAX_PARALLEL_TOOL_CALLS
        if (
            isinstance(configured_max, bool)
            or not isinstance(configured_max, int)
            or configured_max < 1
        ):
            msg = "max_parallel_tool_calls must be an integer >= 1"
            raise ValueError(msg)
        return configured_max

    async def _run_segment(self, segment: _ToolSegment, limit: int) -> bool:
        next_to_start = 0
        in_flight: dict[int, asyncio.Task[None]] = {}
        try:
            while next_to_start < len(segment.indices) or in_flight:
                while next_to_start < len(segment.indices) and len(in_flight) < limit:
                    index = segment.indices[next_to_start]
                    task = asyncio.create_task(self._worker(index))
                    in_flight[index] = task
                    next_to_start += 1
                settled_index = await self._completion_queue.get()
                task = in_flight[settled_index]
                await task
                in_flight.pop(settled_index)
                cancellation_seen = False
                first_failure: Exception | None = None
                try:
                    cancellation_seen, first_failure = await self._handle_settled([settled_index])
                except Exception as error:  # noqa: BLE001
                    await self._drain_failure(in_flight, error)
                if first_failure is not None:
                    await self._drain_failure(in_flight, first_failure)
                if cancellation_seen:
                    await self._cancel_in_flight(in_flight)
                    await self._finish_cancelled()
                    return True
        except asyncio.CancelledError:
            # `/stop`, WebUI pause, and busy-input interruption wake the
            # owning turn task. Converge that active cancellation with the
            # channel/streak path so tool-owned resources are interrupted
            # and every pending tool_call receives a result message.
            await self._cancel_in_flight(in_flight)
            with contextlib.suppress(AgentCancelledError):
                await self._ctx.runtime.drain_control(self._ctx)
            await self._finish_cancelled()
            return True
        finally:
            for task in in_flight.values():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*in_flight.values(), return_exceptions=True)
        return False

    async def _commit_ready(self) -> None:
        while self._commit_cursor < len(self._tool_calls):
            if self._commit_cursor not in self._completed_indices:
                break
            match self._slots[self._commit_cursor]:
                case _SettledResult(result=result):
                    tc = self._tool_calls[self._commit_cursor]
                    if result.call_id != tc.call_id:
                        msg = f"tool result call_id mismatch for {tc.tool_name}"
                        raise RuntimeError(msg)
                    tool_msg = build_tool_message(result, tc.call_id)
                    await self._agent_ctx.history.append(tool_msg)
                    self._state.message_delta.append(
                        MessageDelta(message=tool_msg, source=MessageDeltaSource.TOOL)
                    )
                    self._committed_results.append(result)
                    self._commit_cursor += 1
                case None | _SettledCancellation() | _SettledFailure():
                    break

    async def _settle_result(
        self,
        index: int,
        result: ToolResult,
        *,
        status: ToolCallStatus | None = None,
        propagate_followers: bool = True,
        register_result: bool = False,
    ) -> None:
        tc = self._tool_calls[index]
        stamped = (
            result
            if result.call_id == tc.call_id
            else result.model_copy(update={"call_id": tc.call_id})
        )
        self._slots[index] = _SettledResult(stamped)
        self._completed_indices.add(index)
        if self._batch is not None:
            call_state = self._batch.calls[index]
            call_state.result = stamped
            call_state.status = status or (
                ToolCallStatus.COMPLETED if stamped.error is None else ToolCallStatus.FAILED
            )
        await self._ctx.runtime.emit(
            GraphReActEvent.TOOL_CALL_END,
            ToolCallEndPayload(
                tool_call=tc,
                result=stamped,
                seq=self._seq_for_index[index],
            ),
            self._ctx,
        )
        if register_result and self._node.deduplicator is not None:
            self._node.deduplicator.register_result(
                tc.tool_name,
                tc.arguments or {},
                stamped,
            )
        if propagate_followers:
            for follower_index in self._followers.get(index, []):
                follower = self._tool_calls[follower_index]
                follower_result = stamped.model_copy(update={"call_id": follower.call_id})
                await self._settle_result(
                    follower_index,
                    follower_result,
                    status=status,
                    propagate_followers=False,
                )
        await self._commit_ready()

    def _cancelled_result(self, index: int) -> ToolResult:
        tc = self._tool_calls[index]
        text = "<tool_cancelled>Tool call cancelled.</tool_cancelled>"
        tool = self._agent_ctx.tool_manager.get_tool(tc.tool_name)
        if tool is not None and tool.cancel_note:
            text = f"{text}\n{tool.cancel_note}"
        return ToolResult.from_text(tc.tool_name, text, call_id=tc.call_id)

    async def _worker(self, index: int) -> None:
        self._active_indices.add(index)
        try:
            if self._batch is not None:
                self._batch.calls[index].status = ToolCallStatus.EXECUTING
            tc = self._tool_calls[index]
            result = await self._node.tool_executor.execute(tc, self._agent_ctx)
            streak_action = self._streak_actions.get(index)
            if streak_action is not None and streak_action.action == StreakDecision.REMIND:
                existing = result.message_content()
                appended = (
                    f"{existing}\n{streak_action.reminder}" if existing else streak_action.reminder
                )
                result = result.model_copy(update={"content": [TextPart(text=appended)]})
            self._slots[index] = _SettledResult(result)
        except AgentCancelledError as error:
            self._slots[index] = _SettledCancellation(error)
            self._cancellation_cleanup_indices.add(index)
        except asyncio.CancelledError as error:
            self._slots[index] = _SettledCancellation(error)
            self._cancellation_cleanup_indices.add(index)
        except Exception as error:  # noqa: BLE001
            self._slots[index] = _SettledFailure(error)
        finally:
            self._active_indices.discard(index)
            self._completion_queue.put_nowait(index)

    async def _handle_settled(self, indices: list[int]) -> tuple[bool, Exception | None]:
        cancellation_seen = False
        first_failure: Exception | None = None
        for index in indices:
            match self._slots[index]:
                case _SettledResult(result=result):
                    await self._settle_result(index, result, register_result=True)
                case _SettledCancellation():
                    cancellation_seen = True
                case _SettledFailure(error=error):
                    if self._batch is not None:
                        self._batch.calls[index].status = ToolCallStatus.FAILED
                    if first_failure is None:
                        first_failure = error
                case None:
                    msg = f"tool worker {index} settled without an outcome"
                    raise RuntimeError(msg)
        return cancellation_seen, first_failure

    async def _invoke_on_cancel(self, indices: set[int]) -> None:
        for index in sorted(indices):
            tool = self._agent_ctx.tool_manager.get_tool(self._tool_calls[index].tool_name)
            if tool is None:
                continue
            try:
                await tool.on_cancel()
            except Exception:  # noqa: BLE001
                logger.exception("Tool on_cancel failed for %s", tool.name)

    async def _drain_failure(
        self,
        in_flight: dict[int, asyncio.Task[None]],
        first_failure: Exception,
    ) -> None:
        while in_flight:
            index = await self._completion_queue.get()
            task = in_flight.pop(index)
            await task
            try:
                cancellation_seen, _ = await self._handle_settled([index])
                if cancellation_seen:
                    cleanup_indices = set(self._cancellation_cleanup_indices)
                    await self._invoke_on_cancel(cleanup_indices)
                    self._cancellation_cleanup_indices.difference_update(cleanup_indices)
            except Exception:  # noqa: BLE001
                logger.exception("Tool worker failed while draining batch failure")
        if self._batch is not None:
            if self._batch.operation_id:
                self._state.update_operation(self._batch.operation_id, OperationStatus.FAILED)
            self._batch.status = ToolBatchStatus.FAILED
        self._state.phase = TurnPhase.FAILED
        raise first_failure

    async def _cancel_in_flight(
        self,
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
            with contextlib.suppress(asyncio.CancelledError):
                await in_flight[index]
            if index in self._completed_indices:
                continue
            match self._slots[index]:
                case _SettledResult(result=result):
                    await self._settle_result(index, result, register_result=True)
                case _SettledCancellation() | _SettledFailure() | None:
                    continue
        in_flight.clear()
        while not self._completion_queue.empty():
            self._completion_queue.get_nowait()
        cleanup_indices = self._active_indices | self._cancellation_cleanup_indices
        await self._invoke_on_cancel(cleanup_indices)
        self._cancellation_cleanup_indices.difference_update(cleanup_indices)
        for index in range(len(self._tool_calls)):
            if index not in self._completed_indices:
                await self._settle_result(
                    index,
                    self._cancelled_result(index),
                    status=ToolCallStatus.CANCELLED,
                    propagate_followers=False,
                )

    async def _finish_cancelled(self) -> None:
        if self._node.deduplicator is not None:
            self._node.deduplicator.end_step()
        if self._batch is not None:
            if self._batch.operation_id:
                self._state.update_operation(self._batch.operation_id, OperationStatus.CANCELLED)
            self._batch.status = ToolBatchStatus.CANCELLED
        self._state.phase = TurnPhase.CANCELLED
        await self._ctx.runtime.dispatch_hook(
            ReActHookPoint.AFTER_TOOL_EXECUTION,
            self._ctx,
            data={"results": self._committed_results},
        )
        await self._ctx.runtime.emit(
            GraphReActEvent.ITERATION_END,
            {"iteration": self._state.iteration, "has_tool_calls": True},
            self._ctx,
        )
        self._node.deliver(None, ReActNode.AFTER, self._ctx)


async def run_tool_batch(
    node: ToolNode,
    ctx: GraphContext[ReActTurnState],
    *,
    tool_calls: list[ToolCall],
    decisions: list[ApprovalDecision],
    deny_reasons: list[str | None] | None = None,
) -> None:
    normalized = normalize_batch_decisions(decisions)
    batch = ctx.state.active_tool_batch()
    if batch is not None:
        apply_decisions_to_batch(batch, normalized)
    execution = ToolBatchExecution(
        node,
        ctx,
        tool_calls=tool_calls,
        decisions=normalized,
        deny_reasons=deny_reasons,
    )
    await execution.run()


__all__ = [
    "ToolBatchExecution",
    "apply_decisions_to_batch",
    "normalize_batch_decisions",
    "run_tool_batch",
]
