"""Turn observation seam — the single emitter face for runtime turn events.

This module owns the result road (:class:`AgentResult`) and the observation
road (:class:`TurnEventSink` + :class:`TurnBinding`). Every execution plane
(native ReAct graph, external coding-agent harness) and every consumer
(WebUI, IM channels, ACP editors) meets on the closed :class:`TurnEvent`
union from ``core/turn_events.py`` — there is one ``emit(event)`` method
and one ``flush()``; per-method channels (deltas, completion, errors) no
longer exist.

- :class:`KindGate` — declarative per-kind filtering (replaces the old
  ``EmitterConfig`` on the enum-event channel).
- :class:`TurnEventSink` — the per-session observation port; the base
  ``emit`` applies the gate before dispatch.
- :class:`CompositeTurnEventSink` — ordered fan-out; every child receives
  every gate-passing event (no silent drop path).
- :class:`TurnBinding` — typed identity a sink factory receives instead of
  a bare session id.
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from modex_agent.core.message import ChatMessage
from modex_agent.core.turn_events import (
    StopReason,
    TurnEvent,
    TurnFinishedEvent,
    turn_event_kind_literals,
)

logger = logging.getLogger(__name__)


class AgentResult(BaseModel):
    """Agent 执行结果

    包含最终输出内容和可选的推理/思考过程。
    reasoning 字段用于存储 DeepSeek R1、Kimi 等模型返回的推理内容。
    messages 字段用于存储本次执行生成的所有历史消息（包括 assistant 的 tool_calls 和 tool 结果消息）。
    """

    model_config = ConfigDict(extra="forbid")

    content: str | None = None  # 最终输出内容
    reasoning: str | None = None  # 推理/思考过程（新增）
    stop_reason: StopReason = StopReason.COMPLETED
    error: str | None = None
    messages: Sequence[ChatMessage | dict[str, Any]] = Field(
        default_factory=list
    )  # 本次执行生成的历史消息
    partial_content: str | None = None  # 取消时保留的部分内容
    attachments: list[str] = Field(default_factory=list)  # 要发送给用户的附件路径列表

    @field_validator("messages", mode="before")
    @classmethod
    def _coerce_messages(cls, v: Any) -> Any:
        if isinstance(v, list):
            return [ChatMessage.from_dict(item) if isinstance(item, dict) else item for item in v]
        return v

    def __repr__(self) -> str:
        if self.error:
            return f"AgentResult(error={self.error!r}, stop_reason={self.stop_reason!r})"
        return f"AgentResult(content={self.content!r}, reasoning={'...' if self.reasoning else None}, stop_reason={self.stop_reason!r})"


class KindGate(BaseModel):
    """Declarative filter over ``TurnEvent`` kind literals.

    ``enabled_kinds`` narrows the delivered set (``None`` = every kind);
    ``disabled_kinds`` always wins. Mirrors the semantics of the retired
    ``EmitterConfig`` on the old enum-event channel.

    Construction is loud: every kind string must be a member of the
    closed ``TurnEvent`` union's kind literals — a typo'd kind would
    otherwise silently filter everything.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled_kinds: frozenset[str] | None = None
    disabled_kinds: frozenset[str] = Field(default_factory=frozenset)

    @model_validator(mode="after")
    def _kinds_must_be_union_literals(self) -> Self:
        valid = turn_event_kind_literals()
        unknown_enabled = (
            sorted(self.enabled_kinds - valid) if self.enabled_kinds else []
        )
        unknown_disabled = sorted(self.disabled_kinds - valid)
        if unknown_enabled:
            raise ValueError(
                f"KindGate.enabled_kinds names kinds that are not in the "
                f"TurnEvent union: {unknown_enabled} (valid kinds: {sorted(valid)})"
            )
        if unknown_disabled:
            raise ValueError(
                f"KindGate.disabled_kinds names kinds that are not in the "
                f"TurnEvent union: {unknown_disabled} (valid kinds: {sorted(valid)})"
            )
        return self

    def is_enabled(self, kind: str) -> bool:
        """Whether an event of ``kind`` should be delivered to the sink."""
        if kind in self.disabled_kinds:
            return False
        if self.enabled_kinds is not None:
            return kind in self.enabled_kinds
        return True


class TurnEventSink(ABC):
    """Per-session observation port for runtime turn events.

    Concrete sinks translate the closed ``TurnEvent`` union onto their own
    transport (output adapters, WebSocket frames, transcript records, ACP
    session updates). The base ``emit`` applies the ``gate`` before
    dispatching to :meth:`_dispatch` — subclasses override ``_dispatch``,
    never ``emit``.
    """

    def __init__(self, gate: KindGate | None = None) -> None:
        self.gate = gate or KindGate()

    def wants_streaming(self) -> bool:
        """Whether this sink wants the runtime to use the streaming LLM API.

        Concrete override point; default ``False``.
        """
        return False

    async def emit(self, event: TurnEvent) -> None:
        """Deliver one turn event; gated before dispatch."""
        if not self.gate.is_enabled(event.kind):
            return
        await self._dispatch(event)

    @abstractmethod
    async def _dispatch(self, event: TurnEvent) -> None:
        """Handle one gate-passing event (the subclass override point)."""
        raise NotImplementedError

    async def flush(self) -> None:
        """Force-deliver any buffered output (consumer escape hatch).

        Contract: the PRIMARY flush boundary is the terminal event —
        buffering sinks (:class:`BufferingSink` and its subclasses) flush
        their residual buffers on ``turn_finished`` themselves, so a
        well-formed turn needs no external flush. This method exists for
        the consumer that must push partial output outside a turn's
        lifecycle (e.g. an operator-driven drain); it has no production
        caller on the framework's happy paths. Concrete no-op default:
        the interface has exactly one abstract data method, so
        non-buffering sinks need not override.
        """
        return None


class CompositeTurnEventSink(TurnEventSink):
    """Ordered fan-out: every child receives every gate-passing event.

    Children are awaited IN ORDER (sequential await — delivery order is the
    observable contract; a slow child delays later ones by design). Each
    child applies its own gate. There is no silent drop path: a child that
    ignores an event kind must declare it via its own gate, not by
    omitting an override.

    Fan-out failure policy (shared with
    :class:`~modex_agent.presentation.SessionEventHub`, the other
    multi-consumer event sink): event delivery is observation, not turn
    semantics. A failure in one child's ``emit`` / ``flush`` is isolated
    and logged with the child's identity (class name) — it never
    propagates to the emitting runtime node, never converts a completed
    turn into an errored one, and never starves the remaining children.
    """

    def __init__(self, children: tuple[TurnEventSink, ...], gate: KindGate | None = None) -> None:
        super().__init__(gate)
        self.children: tuple[TurnEventSink, ...] = tuple(children)

    def wants_streaming(self) -> bool:
        """``True`` when ANY child wants streaming (the runtime streams if
        one consumer can observe deltas)."""
        return any(child.wants_streaming() for child in self.children)

    async def _dispatch(self, event: TurnEvent) -> None:
        for child in self.children:
            # Failure isolation: one channel's delivery error must not
            # starve the remaining children — log and continue in order.
            try:
                await child.emit(event)
            except Exception:
                logger.exception(
                    "CompositeTurnEventSink child %s failed on %s event",
                    type(child).__name__,
                    event.kind,
                )

    async def flush(self) -> None:
        for child in self.children:
            try:
                await child.flush()
            except Exception:
                logger.exception(
                    "CompositeTurnEventSink child %s flush failed",
                    type(child).__name__,
                )


class TurnBinding(BaseModel):
    """Typed turn identity a sink factory binds a new sink to.

    The factory contract replaces the bare ``(session_id)`` / ``(session_id,
    pool)`` signatures: a sink learns which session, agent, pool, and turn
    it observes. ``turn_id`` is the runtime's per-turn uuid (``""`` when the
    caller has none at creation time); ``resumed`` marks an approval-resume
    re-invocation carrying the SAME ``turn_id`` as the suspended attempt.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    agent_name: str
    pool: str | None = None
    workspace: str | None = None
    turn_id: str = ""
    resumed: bool = False


TurnEventSinkFactory = Callable[[TurnBinding], TurnEventSink]
"""Creates a sink for one bound turn — a sync closure, never a coroutine."""


def turn_finished_event(result: AgentResult) -> TurnFinishedEvent:
    """Project an ``AgentResult`` onto its exactly-once terminal event."""
    return TurnFinishedEvent(
        stop_reason=result.stop_reason,
        error=result.error,
        attachments=tuple(result.attachments),
    )


__all__ = [
    "AgentResult",
    "CompositeTurnEventSink",
    "KindGate",
    "TurnBinding",
    "TurnEventSink",
    "TurnEventSinkFactory",
    "turn_finished_event",
]
