# ruff: noqa: ANN001, ANN003, ANN201, ARG002, SLF001
"""Deterministic gated tests for the ToolNode batch scheduler."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from modex_agent.agents.react.constants import ReActEvent, ReActHookPoint, ReActNode
from modex_agent.agents.react.nodes.tool import ToolNode
from modex_agent.agents.react.runtime import ReactGraphRuntime
from modex_agent.agents.react.tool_dedup import ToolCallDeduplicator
from modex_agent.agents.react.tool_executor import ToolExecutor
from modex_agent.approval.constants import ApprovalDecision, ApprovalTier
from modex_agent.approval.runtime import ApprovalClassifier, ApprovalRuntime
from modex_agent.control.channel import InMemoryControlChannel
from modex_agent.control.exceptions import AgentCancelledError
from modex_agent.control.types import ControlCommand, ControlCommandType, ControlScope
from modex_agent.core.agent import AgentContext
from modex_agent.core.message import ChatMessage, TextPart
from modex_agent.core.tool_manager import (
    ExclusiveTool,
    ParallelTool,
    ToolResult,
)
from modex_agent.core.types import MessageRole, ToolCall
from modex_agent.runtime.enums import (
    ToolBatchStatus,
    ToolCallStatus,
    TurnCustomKey,
    TurnPhase,
)
from modex_graph.context import GraphContext


@dataclass(slots=True)
class _RunLog:
    started: list[str] = field(default_factory=list)
    finished: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)


class _ImmediateParallelTool(ParallelTool):
    def __init__(self, name: str, log: _RunLog) -> None:
        super().__init__(name=name, description=name, parameters={})
        self._log = log

    async def execute(self, **kwargs) -> str:
        id = kwargs["id"]
        self._log.started.append(id)
        self._log.finished.append(id)
        return f"done-{id}"


class _GatedParallelTool(ParallelTool):
    def __init__(self, name: str, log: _RunLog) -> None:
        super().__init__(name=name, description=name, parameters={})
        self._log = log
        self._gates: dict[str, asyncio.Event] = {}
        self.worker_tasks: list[asyncio.Task[None]] = []

    async def execute(self, **kwargs) -> str:
        id = kwargs["id"]
        self._log.started.append(id)
        worker_task = asyncio.current_task()
        assert worker_task is not None
        self.worker_tasks.append(worker_task)
        gate = asyncio.Event()
        self._gates[id] = gate
        await gate.wait()
        self._log.finished.append(id)
        return f"done-{id}"

    async def on_cancel(self) -> None:
        self._log.cancelled.append(self.name)

    def release(self, id: str) -> None:
        self._gates[id].set()

    def release_all(self) -> None:
        for gate in self._gates.values():
            gate.set()


class _GatedExclusiveTool(ExclusiveTool):
    def __init__(self, name: str, log: _RunLog) -> None:
        super().__init__(name=name, description=name, parameters={})
        self._log = log
        self._gates: dict[str, asyncio.Event] = {}

    async def execute(self, **kwargs) -> str:
        id = kwargs["id"]
        self._log.started.append(id)
        gate = asyncio.Event()
        self._gates[id] = gate
        await gate.wait()
        self._log.finished.append(id)
        return f"done-{id}"

    def release(self, id: str) -> None:
        self._gates[id].set()

    def release_all(self) -> None:
        for gate in self._gates.values():
            gate.set()


class _CancellingParallelTool(ParallelTool):
    cancel_note = "cleanup note"

    def __init__(self, name: str, log: _RunLog) -> None:
        super().__init__(name=name, description=name, parameters={})
        self._log = log
        self._gate = asyncio.Event()

    async def execute(self, **kwargs) -> str:
        id = kwargs["id"]
        self._log.started.append(id)
        if id == "cancel":
            raise AgentCancelledError("cancel from worker")
        await self._gate.wait()
        return f"done-{id}"

    async def on_cancel(self) -> None:
        self._log.cancelled.append(self.name)


class _DecisionClassifier(ApprovalClassifier):
    def classify(self, tool_call: ToolCall, ctx: AgentContext) -> ApprovalTier:
        if tool_call.call_id == "c1":
            return ApprovalTier.HARDLINE
        return ApprovalTier.NORMAL


class _InjectedToolExecutor(ToolExecutor):
    def __init__(
        self,
        execute: Callable[[ToolCall, AgentContext], Awaitable[ToolResult]],
    ) -> None:
        self._execute = execute

    async def execute(self, tool_call: ToolCall, ctx: AgentContext) -> ToolResult:
        return await self._execute(tool_call, ctx)


class _CountingReactGraphRuntime(ReactGraphRuntime):
    def __init__(
        self,
        control_channel: InMemoryControlChannel,
        drain_calls: list[int],
    ) -> None:
        super().__init__(control_channel=control_channel)
        self._drain_calls = drain_calls

    async def drain_control(self, ctx: GraphContext[Any]) -> None:
        self._drain_calls.append(len(self._drain_calls) + 1)
        await super().drain_control(ctx)


async def _until(predicate: Callable[[], bool]) -> None:
    for _ in range(1000):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


async def _finish_task(task: asyncio.Task[None], *tools) -> None:
    for _ in range(1000):
        for tool in tools:
            tool.release_all()
        if task.done():
            await task
            return
        await asyncio.sleep(0)
    raise AssertionError("scheduler task did not finish")


async def _start_batch(
    make_runtime,
    make_graph_ctx,
    tools: Sequence[ParallelTool | ExclusiveTool],
    calls: list[ToolCall],
    *,
    max_parallel: int | str | None = None,
    deduplicator: ToolCallDeduplicator | None = None,
    tool_executor: ToolExecutor | None = None,
    drain_error: AgentCancelledError | None = None,
    approval_runtime: ApprovalRuntime | None = None,
    control_channel: InMemoryControlChannel | None = None,
    drain_calls: list[int] | None = None,
):
    runtime = make_runtime()
    runtime.state.iteration = 1
    runtime.services.approval = approval_runtime
    if max_parallel is not None:
        runtime.state.custom[TurnCustomKey.MAX_PARALLEL_TOOL_CALLS] = max_parallel
    ctx = make_graph_ctx(runtime=runtime)
    if control_channel is not None:
        ctx.runtime = (
            _CountingReactGraphRuntime(control_channel, drain_calls)
            if drain_calls is not None
            else ReactGraphRuntime(control_channel=control_channel)
        )
    for tool in tools:
        ctx.agent_ctx.tool_manager.register(tool)
    await ctx.agent_ctx.history.append(
        ChatMessage(role=MessageRole.ASSISTANT, content="", tool_calls=calls)
    )
    events: list[tuple[ReActEvent, Any]] = []

    async def _capture(event_type, data, ctx) -> None:
        events.append((event_type, data))

    ctx.runtime.emit = _capture
    hooks: list[tuple[Any, Any]] = []

    async def _capture_hook(hook_point, ctx, data=None) -> None:
        hooks.append((hook_point, data))

    ctx.runtime.dispatch_hook = _capture_hook
    if drain_error is not None:

        async def _cancel_at_drain(ctx) -> None:
            raise drain_error

        ctx.runtime.drain_control = _cancel_at_drain
    task = asyncio.create_task(
        ToolNode(tool_executor or ToolExecutor(), deduplicator).run(ctx)
    )
    return ctx, events, hooks, task


async def _assert_cancelled_batch(ctx, events, hooks, expected_ids: list[str]) -> None:
    history = await ctx.agent_ctx.history.to_list()
    tool_messages = [message for message in history if message.role == MessageRole.TOOL]
    batch = ctx.state.tool_batches[-1]
    end_payloads = [data for event, data in events if event == ReActEvent.TOOL_CALL_END]
    after_payloads = [
        data for hook, data in hooks if hook == ReActHookPoint.AFTER_TOOL_EXECUTION
    ]
    assert [message.tool_call_id for message in tool_messages] == expected_ids
    assert len(tool_messages) == len(expected_ids)
    assert [payload.tool_call.call_id for payload in end_payloads] == expected_ids
    assert [payload.seq for payload in end_payloads] == list(range(len(expected_ids)))
    assert all(call.result is not None for call in batch.calls)
    assert all(
        call.status
        in (
            ToolCallStatus.COMPLETED,
            ToolCallStatus.FAILED,
            ToolCallStatus.CANCELLED,
        )
        for call in batch.calls
    )
    assert ctx.state.phase == TurnPhase.CANCELLED
    assert batch.status == ToolBatchStatus.CANCELLED
    assert len(after_payloads) == 1
    assert [result.call_id for result in after_payloads[0]["results"]] == expected_ids
    assert ctx.coordinator.collect_consumable_delivers(ReActNode.AFTER, 0)


async def test_max_one_matches_recorded_serial_golden(make_runtime, make_graph_ctx) -> None:
    log = _RunLog()
    first = _GatedParallelTool("first", log)
    second = _GatedParallelTool("second", log)
    calls = [
        ToolCall(tool_name="first", arguments={"id": "1"}, call_id="c1"),
        ToolCall(tool_name="second", arguments={"id": "2"}, call_id="c2"),
    ]
    ctx, events, _hooks, task = await _start_batch(
        make_runtime,
        make_graph_ctx,
        [first, second],
        calls,
        max_parallel=1,
    )

    await _until(lambda: log.started == ["1"])
    first.release("1")
    await _until(lambda: log.started == ["1", "2"])
    second.release("2")
    await task

    history = await ctx.agent_ctx.history.to_list()
    tool_messages = [message for message in history if message.role == MessageRole.TOOL]
    end_payloads = [data for event, data in events if event == ReActEvent.TOOL_CALL_END]
    batch = ctx.state.tool_batches[-1]
    assert [(message.tool_call_id, message.content) for message in tool_messages] == [
        ("c1", [TextPart(text="done-1")]),
        ("c2", [TextPart(text="done-2")]),
    ]
    assert [delta.message.tool_call_id for delta in ctx.state.message_delta] == ["c1", "c2"]
    assert [
        (payload.tool_call.call_id, payload.result.call_id, payload.seq)
        for payload in end_payloads
    ] == [
        ("c1", "c1", 0),
        ("c2", "c2", 1),
    ]
    assert [payload.result.message_content() for payload in end_payloads] == [
        "done-1",
        "done-2",
    ]
    assert [call.status for call in batch.calls] == [
        ToolCallStatus.COMPLETED,
        ToolCallStatus.COMPLETED,
    ]
    assert [call.result.call_id for call in batch.calls if call.result is not None] == [
        "c1",
        "c2",
    ]
    assert batch.status == ToolBatchStatus.COMPLETED
    assert ctx.state.phase == TurnPhase.CREATED
    assert ctx.coordinator.collect_consumable_delivers(ReActNode.LLM, 0)


@pytest.mark.parametrize(
    "invalid_max_parallel",
    [
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param(True, id="boolean"),
        pytest.param("3", id="string"),
    ],
)
async def test_invalid_max_parallel_is_rejected(
    make_runtime, make_graph_ctx, invalid_max_parallel
) -> None:
    log = _RunLog()
    tool = _ImmediateParallelTool("read", log)
    calls = [ToolCall(tool_name="read", arguments={"id": "1"}, call_id="c1")]
    _ctx, _events, _hooks, task = await _start_batch(
        make_runtime,
        make_graph_ctx,
        [tool],
        calls,
        max_parallel=invalid_max_parallel,
    )
    try:
        await _until(lambda: task.done() or bool(log.started))
    except AssertionError:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        pytest.fail("scheduler did not reject invalid max_parallel_tool_calls")
    if not task.done():
        await _until(task.done)

    with pytest.raises(ValueError, match="must be an integer >= 1"):
        await task
    assert log.started == []


async def test_tool_seq_counter_continues_across_batches(
    make_runtime, make_graph_ctx
) -> None:
    log = _RunLog()
    first_calls = [
        ToolCall(tool_name="read", arguments={"id": "1"}, call_id="c1"),
        ToolCall(tool_name="read", arguments={"id": "2"}, call_id="c2"),
    ]
    ctx, events, _hooks, first_task = await _start_batch(
        make_runtime,
        make_graph_ctx,
        [_ImmediateParallelTool("read", log)],
        first_calls,
        max_parallel=1,
    )
    await first_task
    second_calls = [
        ToolCall(tool_name="read", arguments={"id": "3"}, call_id="c3"),
        ToolCall(tool_name="read", arguments={"id": "4"}, call_id="c4"),
    ]
    await ctx.agent_ctx.history.append(
        ChatMessage(role=MessageRole.ASSISTANT, content="", tool_calls=second_calls)
    )
    ctx.state.iteration = 2

    await ToolNode(ToolExecutor()).run(ctx)

    end_payloads = [data for event, data in events if event == ReActEvent.TOOL_CALL_END]
    assert [payload.tool_call.call_id for payload in end_payloads] == [
        "c1",
        "c2",
        "c3",
        "c4",
    ]
    assert [payload.seq for payload in end_payloads] == [0, 1, 2, 3]
    assert ctx.state.custom[TurnCustomKey.TOOL_SEQ_COUNTER] == 4


async def test_parallel_siblings_start_before_any_completes(make_runtime, make_graph_ctx) -> None:
    log = _RunLog()
    tool = _GatedParallelTool("read", log)
    calls = [
        ToolCall(tool_name="read", arguments={"id": str(index)}, call_id=f"c{index}")
        for index in range(1, 4)
    ]
    ctx, _events, _hooks, task = await _start_batch(
        make_runtime, make_graph_ctx, [tool], calls, max_parallel=3
    )

    started_together = True
    try:
        await _until(lambda: len(log.started) == 3)
    except AssertionError:
        started_together = False
    batch = ctx.state.tool_batches[-1]
    executing = [call.status for call in batch.calls]
    await _finish_task(task, tool)

    assert started_together
    assert log.started == ["1", "2", "3"]
    assert executing == [ToolCallStatus.EXECUTING] * 3


async def test_exclusive_call_forms_barrier_between_parallel_segments(
    make_runtime, make_graph_ctx
) -> None:
    log = _RunLog()
    parallel = _GatedParallelTool("read", log)
    exclusive = _GatedExclusiveTool("write", log)
    calls = [
        ToolCall(tool_name="read", arguments={"id": "before-1"}, call_id="c1"),
        ToolCall(tool_name="read", arguments={"id": "before-2"}, call_id="c2"),
        ToolCall(tool_name="write", arguments={"id": "barrier"}, call_id="c3"),
        ToolCall(tool_name="read", arguments={"id": "after"}, call_id="c4"),
    ]
    _ctx, _events, _hooks, task = await _start_batch(
        make_runtime,
        make_graph_ctx,
        [parallel, exclusive],
        calls,
        max_parallel=2,
    )

    await _until(lambda: log.started == ["before-1", "before-2"])
    assert "barrier" not in log.started
    parallel.release("before-2")
    await _until(lambda: "before-2" in log.finished)
    assert "barrier" not in log.started
    parallel.release("before-1")
    await _until(lambda: log.started == ["before-1", "before-2", "barrier"])
    assert "after" not in log.started
    exclusive.release("barrier")
    await _until(
        lambda: log.started == ["before-1", "before-2", "barrier", "after"]
    )
    parallel.release("after")
    await task

    assert log.finished == ["before-2", "before-1", "barrier", "after"]


async def test_rolling_pool_replenishes_without_waiting_for_earlier_slot(
    make_runtime, make_graph_ctx
) -> None:
    log = _RunLog()
    tool = _GatedParallelTool("read", log)
    calls = [
        ToolCall(tool_name="read", arguments={"id": str(index)}, call_id=f"c{index}")
        for index in range(1, 5)
    ]
    _ctx, _events, _hooks, task = await _start_batch(
        make_runtime, make_graph_ctx, [tool], calls, max_parallel=2
    )

    reached_cap = True
    try:
        await _until(lambda: len(log.started) == 2)
    except AssertionError:
        reached_cap = False
    first_window = list(log.started)
    if "2" in log.started:
        tool.release("2")
    replenished = True
    try:
        await _until(lambda: len(log.started) == 3)
    except AssertionError:
        replenished = False
    rolling_window = list(log.started)
    await _finish_task(task, tool)

    assert reached_cap
    assert first_window == ["1", "2"]
    assert replenished
    assert rolling_window == ["1", "2", "3"]


async def test_commit_cursor_waits_for_contiguous_model_order_slots(
    make_runtime, make_graph_ctx
) -> None:
    log = _RunLog()
    tool = _GatedParallelTool("read", log)
    calls = [
        ToolCall(tool_name="read", arguments={"id": "1"}, call_id="c1"),
        ToolCall(tool_name="read", arguments={"id": "2"}, call_id="c2"),
    ]
    ctx, events, hooks, task = await _start_batch(
        make_runtime, make_graph_ctx, [tool], calls, max_parallel=2
    )

    parallel_start = True
    try:
        await _until(lambda: len(log.started) == 2)
    except AssertionError:
        parallel_start = False
        tool.release("1")
        await _until(lambda: len(log.started) == 2)
        tool.release("2")
        await task
    assert parallel_start
    tool.release("2")
    await _until(
        lambda: any(event == ReActEvent.TOOL_CALL_END for event, _data in events)
    )
    before_first = [
        message
        for message in await ctx.agent_ctx.history.to_list()
        if message.role == MessageRole.TOOL
    ]
    tool.release("1")
    await task

    history = await ctx.agent_ctx.history.to_list()
    committed = [message.tool_call_id for message in history if message.role == MessageRole.TOOL]
    assert before_first == []
    assert committed == ["c1", "c2"]
    assert [delta.message.tool_call_id for delta in ctx.state.message_delta] == ["c1", "c2"]
    after_payload = next(
        data for hook, data in hooks if hook == ReActHookPoint.AFTER_TOOL_EXECUTION
    )
    assert [result.call_id for result in after_payload["results"]] == ["c1", "c2"]


async def test_end_events_follow_completion_order(make_runtime, make_graph_ctx) -> None:
    log = _RunLog()
    tool = _GatedParallelTool("read", log)
    calls = [
        ToolCall(tool_name="read", arguments={"id": "1"}, call_id="c1"),
        ToolCall(tool_name="read", arguments={"id": "2"}, call_id="c2"),
    ]
    _ctx, events, _hooks, task = await _start_batch(
        make_runtime, make_graph_ctx, [tool], calls, max_parallel=2
    )

    parallel_start = True
    try:
        await _until(lambda: len(log.started) == 2)
    except AssertionError:
        parallel_start = False
        tool.release("1")
        await _until(lambda: len(log.started) == 2)
        tool.release("2")
        await task
    assert parallel_start
    tool.release("2")
    await _until(
        lambda: len([event for event, _data in events if event == ReActEvent.TOOL_CALL_END])
        == 1
    )
    tool.release("1")
    await task

    ends = [data for event, data in events if event == ReActEvent.TOOL_CALL_END]
    assert [(payload.tool_call.call_id, payload.seq) for payload in ends] == [
        ("c2", 1),
        ("c1", 0),
    ]
    assert [payload.result.message_content() for payload in ends] == [
        "done-2",
        "done-1",
    ]


async def test_end_events_match_same_tick_worker_settlement_order(
    make_runtime, make_graph_ctx
) -> None:
    log = _RunLog()
    tool = _GatedParallelTool("read", log)
    calls = [
        ToolCall(tool_name="read", arguments={"id": str(index)}, call_id=f"c{index}")
        for index in range(1, 6)
    ]
    _ctx, events, _hooks, task = await _start_batch(
        make_runtime, make_graph_ctx, [tool], calls, max_parallel=5
    )

    await _until(lambda: len(log.started) == 5)
    tool.release_all()
    await task

    ends = [data for event, data in events if event == ReActEvent.TOOL_CALL_END]
    assert [payload.tool_call.arguments["id"] for payload in ends] == log.finished


async def test_duplicate_calls_execute_leader_once_and_complete_each_follower(
    make_runtime, make_graph_ctx
) -> None:
    log = _RunLog()
    tool = _GatedParallelTool("read", log)
    calls = [
        ToolCall(tool_name="read", arguments={"id": "same"}, call_id="c1"),
        ToolCall(tool_name="read", arguments={"id": "same"}, call_id="c2"),
        ToolCall(tool_name="read", arguments={"id": "same"}, call_id="c3"),
    ]
    ctx, events, _hooks, task = await _start_batch(
        make_runtime,
        make_graph_ctx,
        [tool],
        calls,
        deduplicator=ToolCallDeduplicator(),
    )

    await _until(lambda: bool(log.started))
    started_before_release = list(log.started)
    tool.release("same")
    await task

    ends = [data for event, data in events if event == ReActEvent.TOOL_CALL_END]
    history = await ctx.agent_ctx.history.to_list()
    assert started_before_release == ["same"]
    assert [payload.tool_call.call_id for payload in ends] == ["c1", "c2", "c3"]
    assert [payload.result.call_id for payload in ends] == ["c1", "c2", "c3"]
    assert [payload.seq for payload in ends] == [0, 1, 2]
    assert [message.tool_call_id for message in history if message.role == MessageRole.TOOL] == [
        "c1",
        "c2",
        "c3",
    ]


@pytest.mark.parametrize(
    "worker_error",
    [
        pytest.param(AgentCancelledError("cancel from worker"), id="agent-cancelled"),
        pytest.param(asyncio.CancelledError(), id="asyncio-cancelled"),
    ],
)
async def test_worker_cancellation_cancels_pool_and_synthesizes_every_result(
    make_runtime, make_graph_ctx, worker_error
) -> None:
    log = _RunLog()
    tool = _CancellingParallelTool("read", log)
    pending_gate = asyncio.Event()

    async def _execute(tool_call: ToolCall, ctx: AgentContext) -> ToolResult:
        id = str((tool_call.arguments or {})["id"])
        log.started.append(id)
        if id == "cancel":
            raise worker_error
        await pending_gate.wait()
        return ToolResult.from_text(tool_call.tool_name, f"done-{id}")

    executor = _InjectedToolExecutor(_execute)
    calls = [
        ToolCall(tool_name="read", arguments={"id": "cancel"}, call_id="c1"),
        ToolCall(tool_name="read", arguments={"id": "pending"}, call_id="c2"),
        ToolCall(tool_name="read", arguments={"id": "unstarted"}, call_id="c3"),
    ]
    ctx, events, hooks, task = await _start_batch(
        make_runtime,
        make_graph_ctx,
        [tool],
        calls,
        max_parallel=2,
        tool_executor=executor,
    )

    await task

    await _assert_cancelled_batch(ctx, events, hooks, ["c1", "c2", "c3"])
    history = await ctx.agent_ctx.history.to_list()
    cancelled_text = "".join(str(message.content) for message in history[1:])
    assert "<tool_cancelled>" in cancelled_text
    assert "cleanup note" in cancelled_text
    assert log.cancelled == ["read", "read"]


async def test_outer_task_cancellation_converges_to_cancelled_batch(
    make_runtime, make_graph_ctx
) -> None:
    log = _RunLog()
    tool = _GatedParallelTool("read", log)
    calls = [
        ToolCall(tool_name="read", arguments={"id": "1"}, call_id="c1"),
        ToolCall(tool_name="read", arguments={"id": "2"}, call_id="c2"),
    ]
    ctx, events, hooks, task = await _start_batch(
        make_runtime,
        make_graph_ctx,
        [tool],
        calls,
        max_parallel=2,
    )
    await _until(lambda: log.started == ["1", "2"])
    worker_tasks = list(tool.worker_tasks)

    task.cancel()
    await task
    await asyncio.sleep(0)
    workers_drained = all(worker_task.done() for worker_task in worker_tasks)

    tool.release_all()
    await asyncio.gather(*worker_tasks, return_exceptions=True)
    assert workers_drained
    await _assert_cancelled_batch(ctx, events, hooks, ["c1", "c2"])
    assert log.cancelled == ["read", "read"]
    history = await ctx.agent_ctx.history.to_list()
    tool_messages = [message for message in history if message.role == MessageRole.TOOL]
    assert all("<tool_cancelled>" in str(message.content) for message in tool_messages)


async def test_outer_cancel_with_undispatched_pool_slot_synthesizes_result(
    make_runtime, make_graph_ctx
) -> None:
    """A pool-limit-queued call that never got a worker task must still
    receive its synthesized <tool_cancelled> result on outer cancellation.
    The unified path settles from the task map and never assumes every
    in-flight index produced a completion-queue entry (a worker cancelled
    before its first step runs no finally block and queues nothing)."""
    log = _RunLog()
    tool = _GatedParallelTool("read", log)
    calls = [
        ToolCall(tool_name="read", arguments={"id": "1"}, call_id="c1"),
        ToolCall(tool_name="read", arguments={"id": "2"}, call_id="c2"),
        ToolCall(tool_name="read", arguments={"id": "3"}, call_id="c3"),
    ]
    ctx, events, hooks, task = await _start_batch(
        make_runtime,
        make_graph_ctx,
        [tool],
        calls,
        max_parallel=2,
    )

    # Only slots 1+2 start (limit 2); slot 3 is still queued.
    await _until(lambda: log.started == ["1", "2"])
    # Saturate the loop so the scheduler is parked on completion_queue.get()
    # when the outer cancel lands.
    await asyncio.sleep(0)

    task.cancel()
    # Before the fix this hung forever waiting for slot 3's queue entry.
    await asyncio.wait_for(asyncio.shield(task), timeout=5)

    tool.release_all()
    await asyncio.gather(*tool.worker_tasks, return_exceptions=True)
    await _assert_cancelled_batch(ctx, events, hooks, ["c1", "c2", "c3"])


async def test_batch_head_control_cancellation_synthesizes_without_dispatch(
    make_runtime, make_graph_ctx
) -> None:
    log = _RunLog()
    tool = _ImmediateParallelTool("read", log)
    calls = [
        ToolCall(tool_name="read", arguments={"id": "1"}, call_id="c1"),
        ToolCall(tool_name="read", arguments={"id": "2"}, call_id="c2"),
    ]
    ctx, events, hooks, task = await _start_batch(
        make_runtime,
        make_graph_ctx,
        [tool],
        calls,
        drain_error=AgentCancelledError("cancel before dispatch"),
    )

    await task

    await _assert_cancelled_batch(ctx, events, hooks, ["c1", "c2"])
    assert log.started == []


async def test_control_cancellation_is_observed_before_next_segment(
    make_runtime, make_graph_ctx
) -> None:
    log = _RunLog()
    parallel = _GatedParallelTool("read", log)
    exclusive = _GatedExclusiveTool("write", log)
    channel = InMemoryControlChannel()
    drain_calls: list[int] = []
    calls = [
        ToolCall(tool_name="read", arguments={"id": "before-1"}, call_id="c1"),
        ToolCall(tool_name="read", arguments={"id": "before-2"}, call_id="c2"),
        ToolCall(tool_name="write", arguments={"id": "barrier"}, call_id="c3"),
        ToolCall(tool_name="read", arguments={"id": "after"}, call_id="c4"),
    ]
    ctx, events, hooks, task = await _start_batch(
        make_runtime,
        make_graph_ctx,
        [parallel, exclusive],
        calls,
        max_parallel=2,
        control_channel=channel,
        drain_calls=drain_calls,
    )
    await _until(lambda: log.started == ["before-1", "before-2"])
    assert drain_calls == [1]
    await channel.send(
        ControlCommand(
            command_id="cancel-between-segments",
            type=ControlCommandType.CANCEL_TURN,
            scope=ControlScope(session_id=str(ctx.agent_ctx.session)),
        )
    )

    parallel.release("before-1")
    await _until(lambda: log.finished == ["before-1"])
    assert drain_calls == [1]
    parallel.release("before-2")
    await _until(lambda: task.done() or "barrier" in log.started)
    if not task.done():
        exclusive.release("barrier")
        await _until(lambda: "after" in log.started)
        parallel.release("after")
    await task

    await _assert_cancelled_batch(ctx, events, hooks, ["c1", "c2", "c3", "c4"])
    assert drain_calls == [1, 2]
    assert log.started == ["before-1", "before-2"]


async def test_streak_stop_cancels_follower_with_tool_cancelled_result(
    make_runtime, make_graph_ctx
) -> None:
    deduplicator = ToolCallDeduplicator()
    for _ in range(12):
        deduplicator.begin_step()
        deduplicator.check_streak("read", {"id": "same"})
        deduplicator.register_result(
            "read", {"id": "same"}, ToolResult.from_text("read", "prior")
        )
        deduplicator.end_step()
    log = _RunLog()
    tool = _ImmediateParallelTool("read", log)
    calls = [
        ToolCall(tool_name="read", arguments={"id": "same"}, call_id="c1"),
        ToolCall(tool_name="read", arguments={"id": "same"}, call_id="c2"),
    ]
    ctx, events, hooks, task = await _start_batch(
        make_runtime,
        make_graph_ctx,
        [tool],
        calls,
        deduplicator=deduplicator,
    )

    await task

    await _assert_cancelled_batch(ctx, events, hooks, ["c1", "c2"])
    history = await ctx.agent_ctx.history.to_list()
    follower = next(message for message in history if message.tool_call_id == "c2")
    assert "<tool_cancelled>" in str(follower.content)
    assert log.started == []


async def test_internal_scheduler_failure_drains_started_workers_then_rethrows(
    make_runtime, make_graph_ctx
) -> None:
    tools = [
        _ImmediateParallelTool("first", _RunLog()),
        _ImmediateParallelTool("second", _RunLog()),
        _ImmediateParallelTool("third", _RunLog()),
    ]
    started: list[str] = []
    fail_gate = asyncio.Event()
    sibling_gate = asyncio.Event()

    async def _execute(tool_call: ToolCall, ctx: AgentContext) -> ToolResult:
        started.append(tool_call.call_id or "")
        if tool_call.call_id == "c1":
            await fail_gate.wait()
            raise ValueError("scheduler exploded")
        await sibling_gate.wait()
        return ToolResult.from_text(tool_call.tool_name, "settled")

    executor = _InjectedToolExecutor(_execute)
    calls = [
        ToolCall(tool_name="first", arguments={"id": "1"}, call_id="c1"),
        ToolCall(tool_name="second", arguments={"id": "2"}, call_id="c2"),
        ToolCall(tool_name="third", arguments={"id": "3"}, call_id="c3"),
    ]
    ctx, events, _hooks, task = await _start_batch(
        make_runtime,
        make_graph_ctx,
        tools,
        calls,
        max_parallel=2,
        tool_executor=executor,
    )

    await _until(lambda: "c1" in started)
    for _ in range(10):
        await asyncio.sleep(0)
    parallel_start = started == ["c1", "c2"]
    fail_gate.set()
    if parallel_start:
        await _until(
            lambda: ctx.state.tool_batches[-1].calls[0].status == ToolCallStatus.FAILED
        )
        drained_before_rethrow = not task.done()
        third_started = "c3" in started
        sibling_gate.set()
        with pytest.raises(ValueError, match="scheduler exploded"):
            await task
    else:
        drained_before_rethrow = False
        third_started = False
        with pytest.raises(ValueError, match="scheduler exploded"):
            await task

    history = await ctx.agent_ctx.history.to_list()
    batch = ctx.state.tool_batches[-1]
    assert parallel_start
    assert drained_before_rethrow
    assert not third_started
    assert [message for message in history if message.role == MessageRole.TOOL] == []
    assert not any(event == ReActEvent.ITERATION_END for event, _data in events)
    assert ctx.state.phase == TurnPhase.FAILED
    assert batch.status == ToolBatchStatus.FAILED
    assert batch.calls[0].status == ToolCallStatus.FAILED
    assert batch.calls[1].status == ToolCallStatus.COMPLETED
    assert batch.calls[2].result is None


async def test_failure_drain_cleans_up_cancelled_sibling_without_synthesis(
    make_runtime, make_graph_ctx
) -> None:
    log = _RunLog()
    failing = _GatedParallelTool("failing", log)
    cancelled = _GatedParallelTool("cancelled", log)
    started: list[str] = []
    workers: dict[str, asyncio.Task[None]] = {}
    fail_gate = asyncio.Event()
    sibling_gate = asyncio.Event()

    async def _execute(tool_call: ToolCall, ctx: AgentContext) -> ToolResult:
        call_id = tool_call.call_id or ""
        worker_task = asyncio.current_task()
        assert worker_task is not None
        workers[call_id] = worker_task
        started.append(call_id)
        if call_id == "c1":
            await fail_gate.wait()
            raise ValueError("scheduler exploded")
        await sibling_gate.wait()
        return ToolResult.from_text(tool_call.tool_name, "settled")

    executor = _InjectedToolExecutor(_execute)
    calls = [
        ToolCall(tool_name="failing", arguments={"id": "1"}, call_id="c1"),
        ToolCall(tool_name="cancelled", arguments={"id": "2"}, call_id="c2"),
    ]
    ctx, events, _hooks, task = await _start_batch(
        make_runtime,
        make_graph_ctx,
        [failing, cancelled],
        calls,
        max_parallel=2,
        tool_executor=executor,
    )
    await _until(lambda: started == ["c1", "c2"])

    fail_gate.set()
    await _until(
        lambda: ctx.state.tool_batches[-1].calls[0].status == ToolCallStatus.FAILED
    )
    workers["c2"].cancel()
    with pytest.raises(ValueError, match="scheduler exploded"):
        await task

    history = await ctx.agent_ctx.history.to_list()
    batch = ctx.state.tool_batches[-1]
    assert log.cancelled == ["cancelled"]
    assert [message for message in history if message.role == MessageRole.TOOL] == []
    assert not any(event == ReActEvent.TOOL_CALL_END for event, _data in events)
    assert all(call.result is None for call in batch.calls)
    assert ctx.state.phase == TurnPhase.FAILED
    assert batch.status == ToolBatchStatus.FAILED


async def test_start_events_precede_batch_hooks_drain_and_invocation(
    make_runtime, make_graph_ctx
) -> None:
    order: list[str] = []
    runtime = make_runtime()
    runtime.state.iteration = 1
    ctx = make_graph_ctx(runtime=runtime)
    tool = _ImmediateParallelTool("read", _RunLog())
    ctx.agent_ctx.tool_manager.register(tool)
    calls = [
        ToolCall(tool_name="read", arguments={"id": "1"}, call_id="c1"),
        ToolCall(tool_name="read", arguments={"id": "2"}, call_id="c2"),
    ]
    await ctx.agent_ctx.history.append(
        ChatMessage(role=MessageRole.ASSISTANT, content="", tool_calls=calls)
    )

    async def _emit(event, data, _ctx) -> None:
        if event == ReActEvent.TOOL_CALL_START:
            order.append(f"start:{data.call_id}")
        elif event == ReActEvent.PROGRESS:
            order.append("progress")

    async def _hook(hook_point, _ctx, data=None) -> None:
        order.append(hook_point.value)

    async def _drain(_ctx) -> None:
        order.append("drain")

    async def _execute(tool_call: ToolCall, ctx: AgentContext) -> ToolResult:
        order.append(f"execute:{tool_call.call_id}")
        return ToolResult.from_text(tool_call.tool_name, "done")

    executor = _InjectedToolExecutor(_execute)
    ctx.runtime.emit = _emit
    ctx.runtime.dispatch_hook = _hook
    ctx.runtime.drain_control = _drain

    await ToolNode(executor).run(ctx)

    assert order[:6] == [
        "start:c1",
        "start:c2",
        "progress",
        ReActHookPoint.BEFORE_TOOL_EXECUTION.value,
        "drain",
        "execute:c1",
    ]


async def test_same_key_with_different_decisions_is_not_pruned(
    make_runtime, make_graph_ctx
) -> None:
    log = _RunLog()
    tool = _ImmediateParallelTool("read", log)
    calls = [
        ToolCall(tool_name="read", arguments={"id": "same"}, call_id="c1"),
        ToolCall(tool_name="read", arguments={"id": "same"}, call_id="c2"),
    ]
    ctx, events, _hooks, task = await _start_batch(
        make_runtime,
        make_graph_ctx,
        [tool],
        calls,
        deduplicator=ToolCallDeduplicator(),
        approval_runtime=ApprovalRuntime(classifier=_DecisionClassifier()),
    )

    await task

    ends = [data for event, data in events if event == ReActEvent.TOOL_CALL_END]
    history = await ctx.agent_ctx.history.to_list()
    tool_messages = [message for message in history if message.role == MessageRole.TOOL]
    assert [payload.tool_call.call_id for payload in ends] == ["c1", "c2"]
    assert [call.decision for call in ctx.state.tool_batches[-1].calls] == [
        ApprovalDecision.DENIED,
        ApprovalDecision.PREEMPTED,
    ]
    assert [payload.result.call_id for payload in ends] == ["c1", "c2"]
    assert "Denied by user" in str(tool_messages[0].content)
    assert "Skipped" in str(tool_messages[1].content)
    assert log.started == []
