"""ReActAgent 实现

提供 ReActAgent 类，实现 Thought → Action → Observation 循环。
所有运行时观察都以核心 ``TurnEvent`` 流的形式经 ``TurnEventSink`` 发出。
"""

import asyncio
import logging
from typing import Any, Literal

from modex_agent.agents.react.constants import InterruptReason
from modex_agent.agents.react.state import get_react_state
from modex_agent.core.control import (
    AgentCancelledError,
    AgentControlError,
    AgentTimeoutError,
    LoopDetectedError,
    PolicyViolationError,
)
from modex_agent.core.turn.enums import TurnCustomKey, TurnPhase
from modex_agent.hook import HookPayload, HookPoint

from ...core.agent import Agent, AgentContext, current_agent_context
from ...core.emitter import AgentResult, TurnEventSink, turn_finished_event
from ...core.provider import LLMProvider
from ...core.turn_events import StopReason, TurnErroredEvent, TurnTextEvent
from .message_builder import build_interrupted_assistant_message
from .tool_dedup import ToolCallDeduplicator

logger = logging.getLogger(__name__)

def _get_turn_messages(ctx: AgentContext) -> list[dict[str, Any]]:
    """Extract current-turn messages from typed state or metadata fallback."""
    state = get_react_state(ctx)
    if state is not None:
        return [md.message.to_dict() for md in state.message_delta]
    return []


def _interrupt_reason_from(exc: BaseException) -> InterruptReason:
    """Map a cancel/error exception to a short, non-leaky interrupt category."""
    if isinstance(exc, AgentCancelledError):
        return InterruptReason.USER_STOP
    if isinstance(exc, AgentTimeoutError):
        return InterruptReason.TIMEOUT
    if isinstance(exc, PolicyViolationError):
        return InterruptReason.POLICY
    if isinstance(exc, LoopDetectedError):
        return InterruptReason.LOOP_DETECTED
    if isinstance(exc, asyncio.CancelledError):
        return InterruptReason.CANCELLED
    return InterruptReason.ERROR


async def _persist_interrupted_partial(ctx: AgentContext, reason: str) -> None:
    """Persist a partially-produced assistant response as an XML-marked message.

    Reads (and clears) the partial content stashed by the ReactLlmClient
    event loop when an LLM stream was interrupted mid-flight. Appends an
    interrupted assistant message to both ``ctx.history`` (memory) and
    ``message_delta``
    (so ``_get_turn_messages`` mirrors it), keeping memory aligned with the
    transcript. No-op when no partial was captured (normal completion, or an
    interrupt that produced nothing).
    """
    from modex_agent.core.turn.enums import MessageDeltaSource
    from modex_agent.core.turn.models import MessageDelta

    state = get_react_state(ctx)
    if state is None:
        return
    partial = state.custom.pop(TurnCustomKey.INTERRUPTED_PARTIAL, None)
    if not partial:
        return
    content = partial.get("content") or ""
    tool_names = partial.get("tool_names") or []
    if not content and not tool_names:
        return
    msg = build_interrupted_assistant_message(content, tool_names, reason)
    if ctx.history is not None:
        await ctx.history.append(msg)
    state.message_delta.append(MessageDelta(message=msg, source=MessageDeltaSource.ASSISTANT))


class ReActAgent(Agent):
    """ReAct 推理模式实现

    执行 Thought → Action → Observation 循环。
    所有输出通过 ``TurnEventSink``（核心 ``TurnEvent`` 流）。

    事件流示例（单次工具调用）：
    1. turn_started
    2. iteration_started / text (流式) -> "让我查一下天气..."
    3. tool_call -> {"tool": "weather", "args": {"city": "北京"}}
    4. tool_result -> {"tool": "weather", "result": "晴天 25°C"}
    5. iteration_finished
    6. text (流式) -> "北京的天气是..."
    7. turn_finished (stop_reason=completed)
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        mode: Literal["clean", "full"] = "full",
    ) -> None:
        from modex_agent.agents.react.injection_drainer import InjectionDrainer
        from modex_agent.agents.react.llm_client import ReactLlmClient
        from modex_agent.agents.react.tool_executor import ToolExecutor

        self.provider = provider
        self.mode: Literal["clean", "full"] = mode
        self._llm_client = ReactLlmClient(provider)
        self._injection_drainer = InjectionDrainer()
        self._tool_executor = ToolExecutor()

    @property
    def name(self) -> str:
        return "ReActAgent"

    async def run(
        self,
        context: AgentContext,
        emitter: TurnEventSink,
    ) -> AgentResult:
        """Delegate to GraphEngine (thin shell).

        Args:
            context: Agent execution context
            emitter: turn-event sink

        Returns:
            AgentResult: execution result
        """
        from modex_graph.exceptions import GraphInterrupt

        # 每轮开始时清空 attachments，避免跨轮污染
        context.attachments = []
        context.emitter = emitter

        # Use prebuilt runtime if already set on context; otherwise build clean runtime.
        if context.runtime is None:
            from modex_agent.agents.react.state import ReActTurnState
            from modex_agent.core.turn.enums import AgentKind
            from modex_agent.core.turn.models import TurnIdentity
            from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices

            state = ReActTurnState(
                identity=context.identity
                or TurnIdentity(agent_id="react", session=context.session, turn_id="default"),
                agent_kind=AgentKind.REACT,
                phase=TurnPhase.CREATED,
            )
            context.identity = state.identity
            context.runtime = AgentRuntime(services=AgentRuntimeServices(), state=state)
        runtime = context.runtime

        # ADR-0033 D5 + D13 Stage 4: construct ``ReactGraphRuntime`` (AOP bridge)
        # and pass it as ``GraphContext.runtime``. ``ReactGraphRuntime`` methods
        # handle ``None`` services as no-ops, so clean mode (no services) is fine.
        # The graph runtime lives exclusively on ``GraphContext.runtime`` for
        # the duration of ``engine.run_async``.
        from modex_agent.agents.react.context import ReActGraphContext
        from modex_agent.agents.react.graph import build_react_graph
        from modex_agent.agents.react.runtime import ReactGraphRuntime
        from modex_agent.agents.react.state import ReActSnapshotPolicy
        from modex_graph.engine import GraphEngine
        from modex_graph.scheduler.bootstrap import BootstrapMode

        graph_runtime = ReactGraphRuntime(
            hook_runner=runtime.services.hooks,
            interceptor_chain=runtime.services.interceptors,
            governance=runtime.services.governance,
            control_channel=runtime.services.control_channel,
            snapshot_policy=ReActSnapshotPolicy(),
            turn_state_store=runtime.services.turn_store,
        )
        # The turn sink reaches the nodes through ``context.emitter``
        # (set above) — the graph runtime no longer carries its own copy.

        ctx_token = current_agent_context.set(context)

        # ``result`` stays None on a GraphInterrupt (approval suspend) so the
        # FINALLY_GRAPH notification hook skips -- suspend is an expected pause,
        # not a turn end. Every other path (success / cancel / error) reassigns
        # it to a concrete AgentResult before the ``finally`` runs.
        result: AgentResult | None = AgentResult(content="", stop_reason=StopReason.ERROR)

        async def actual_turn() -> AgentResult:
            nonlocal result
            if runtime.hooks:
                await runtime.hooks.dispatch(HookPoint.BEFORE_GRAPH, context)

            # Drain control commands before starting turn
            if context.runtime and context.runtime.control_channel:
                from modex_agent.hook.builtin.control_drain import drain_control_channel

                await drain_control_channel(
                    context.runtime.control_channel,
                    context,
                    turn_uuid=context.runtime.turn_uuid,
                )

            # ADR-0033 D13 Stage 4: construct the new ``modex_graph`` engine.
            # Engine limit is opt-in and ReAct does not set it — the
            # business-level iteration gate in ``LLMNode`` (routing to AFTER
            # via a static edge) is the sole iteration cap, and the pool
            # watchdog is the sole time-based termination mechanism.
            graph = build_react_graph(
                llm_client=self._llm_client,
                injection_drainer=self._injection_drainer,
                tool_executor=self._tool_executor,
                mode=self.mode,
                deduplicator=ToolCallDeduplicator(),
            ).compile()
            engine = GraphEngine(graph)
            react_state = get_react_state(context)
            assert react_state is not None  # constructed just above if previously None
            # Per-turn Null coordinator: node-invocation layer is orthogonal to
            # AgentContext (which holds agent turn state).
            from modex_graph import create_null_coordinator

            coordinator = create_null_coordinator()
            for node in graph.nodes.values():
                coordinator.register_node(node.node_id)
            graph_ctx = ReActGraphContext(
                state=react_state,
                runtime=graph_runtime,
                user_data=context,
                coordinator=coordinator,
            )
            returned_state = await engine.run_async(graph_ctx, mode=BootstrapMode.FRESH)
            # ADR-0033 D9.3: the terminal ``EndNode`` writes ``state.result``;
            # ``engine.run_async`` returns the final state. Read the typed
            # ``result`` field — the old ``custom[GRAPH_RESULT]`` dual-write is
            # gone.
            if returned_state is not None:
                react_state = returned_state
            if react_state.result is not None:
                result = react_state.result
            if runtime.hooks and react_state.iteration > 0:
                await runtime.hooks.dispatch(HookPoint.AFTER_ITERATION, context)
            if runtime.hooks:
                await runtime.hooks.dispatch(
                    HookPoint.AFTER_GRAPH,
                    context,
                    HookPayload(data={"result": result}),
                )
            assert result is not None
            return result

        try:
            if runtime.interceptors is not None:
                from modex_agent.core.interceptor import InterceptorScope

                if runtime.interceptors.has_scope(InterceptorScope.TURN):
                    result = await runtime.interceptors.around_turn(context, actual_turn)
                else:
                    result = await actual_turn()
            else:
                result = await actual_turn()
            return result
        except GraphInterrupt:
            # Approval suspend is an EXPECTED pause (a tool awaited human
            # approval), not a turn end -- and definitely not an error. The
            # ``finally`` below dispatches FINALLY_GRAPH with whatever ``result``
            # holds; leaving the initial ``AgentResult(stop_reason=ERROR)``
            # default here would make TurnOutcomeNotifyHook misreport every
            # approval suspend as "The turn ended unexpectedly due to an error".
            # None signals "no turn outcome" so the notification hook skips.
            result = None
            raise
        except AgentControlError as e:
            # Controlled exit (cancel / timeout / policy / loop-detected) is an
            # expected turn outcome. The exception carries its own user-facing
            # content + stop_reason (see control.exceptions), so a single handler
            # renders every subclass uniformly — no per-subclass catch branches.
            logger.info("ReActAgent control exit: %s", str(e) or "error")
            await _persist_interrupted_partial(context, _interrupt_reason_from(e))
            all_new = _get_turn_messages(context)
            user_content = e.user_content or ""
            stop_reason = e.stop_reason or StopReason.CANCELLED
            # Loop-detected content is constructed in the catch block (not
            # streamed); ensure it reaches the user even when the streaming
            # emitter considers the stream already ended.
            if user_content and emitter is not None:
                try:
                    await emitter.emit(TurnTextEvent(text=user_content))
                except Exception:
                    logger.exception("emit of control-exit content failed")
            result = AgentResult(
                content=user_content,
                stop_reason=stop_reason,
                messages=all_new,
                attachments=context.attachments,
            )
            await emitter.emit(turn_finished_event(result))
            return result
        except asyncio.CancelledError:
            # Task cancellation (e.g. from control-channel CANCEL_TURN that
            # uses task.cancel() to interrupt an in-flight LLM call) is a
            # controlled stop, not a crash. Emit the terminal signal and
            # return a cancelled result so the turn closes cleanly.
            logger.warning("ReActAgent cancelled")
            await _persist_interrupted_partial(context, "cancelled")
            all_new = _get_turn_messages(context)
            result = AgentResult(
                content="",
                stop_reason=StopReason.CANCELLED,
                messages=all_new,
                attachments=context.attachments,
            )
            await emitter.emit(turn_finished_event(result))
            return result
        except Exception as e:
            logger.exception("Agent execution error")
            await emitter.emit(TurnErroredEvent(message=str(e)))
            await _persist_interrupted_partial(context, "error")
            all_new = _get_turn_messages(context)
            result = AgentResult(
                error=str(e),
                stop_reason=StopReason.ERROR,
                messages=all_new,
                attachments=context.attachments,
            )
            await emitter.emit(turn_finished_event(result))
            return result
        finally:
            # FINALLY_GRAPH: fires regardless of success/error/cancel.
            # SubagentAutoSendHook and cleanup hooks always execute.
            if runtime.hooks:
                try:
                    await runtime.hooks.dispatch(
                        HookPoint.FINALLY_GRAPH,
                        context,
                        HookPayload(data={"result": result}),
                    )
                except Exception:
                    logger.exception("FINALLY_GRAPH hook dispatch failed")
            # Clean up typed state
            final_state = get_react_state(context)
            if final_state is not None:
                # GraphInterrupt (approval suspend) already persisted a
                # SUSPENDED snapshot in ToolNode._suspend_for_approval.
                # Do NOT overwrite it with a terminal snapshot here —
                # otherwise list_active_turns(phase=SUSPENDED) returns empty
                # and the resume path cannot find the turn.
                if final_state.phase == TurnPhase.SUSPENDED:
                    pass
                else:
                    final_state.phase = (
                        TurnPhase.COMPLETED
                        if final_state.phase not in (TurnPhase.COMPLETED, TurnPhase.FAILED)
                        else final_state.phase
                    )
                    if runtime is not None and runtime.services.turn_store is not None:
                        try:
                            from modex_agent.agents.react.state import ReActSnapshotPolicy
                            from modex_agent.core.turn.enums import SnapshotReason

                            terminal_snapshot = ReActSnapshotPolicy().capture(
                                final_state, SnapshotReason.TURN_INTERRUPTED
                            )
                            await runtime.services.turn_store.save_turn(terminal_snapshot)
                        except Exception:
                            logger.warning(
                                "Failed to persist terminal turn snapshot for turn %s",
                                final_state.identity.turn_id,
                                exc_info=True,
                            )
            context.emitter = None
            current_agent_context.reset(ctx_token)

    # Message construction helpers live in this package's message_builder module
    # (build_assistant_message, build_tool_message, build_interrupted_assistant_message)
    # to keep ReActAgent focused on orchestration rather than data-formatting details.
