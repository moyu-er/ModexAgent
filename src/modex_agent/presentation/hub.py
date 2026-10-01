"""SessionEventHub — the per-turn station of the presentation layer.

The hub is the framework-generic composition point between the runtime
stream and UI-facing consumers: it receives core ``TurnEvent``s (it IS a
``TurnEventSink``), runs the default projection (identity envelope, lazy
turn bookkeeping, tool-card pairing, latency), and fans each resulting
``PresentationEvent`` out to the registered :class:`PresentationSink`
consumers — a wire projection, a transcript tap, a console renderer — in
registration order.

Deployment wiring keeps only its own concerns: an IM channel that needs
the raw core stream stays a plain ``TurnEventSink`` composed alongside
the hub (e.g. via ``CompositeTurnEventSink``); a consumer that only needs
projected events registers here. The bot's transcript persistence and
ServerEvent wire projection are both registered as ``PresentationSink``s
on their session's hub (ADR-0054).

Turn identity comes from the :class:`~modex_agent.core.emitter.TurnBinding`
the hub was built from: a non-empty ``binding.turn_id`` is adopted as the
projector's turn id, an empty one keeps the projector's lazy factory. A
``resumed=True`` binding (an approval resume re-invocation carrying the
suspended attempt's ``turn_id``) pre-activates the projector, so the
resumed leg continues the SAME turn id and emits no second ``TurnStarted``
— neither for a replayed eager ``turn_started`` (idempotent) nor for the
lazy first-content announcement.
"""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence

from modex_agent.core.emitter import KindGate, TurnBinding, TurnEventSink
from modex_agent.core.turn_events import TurnEvent

from .events import PresentationEvent
from .projector import DefaultTurnEventProjector

logger = logging.getLogger(__name__)


class PresentationSink(ABC):
    """Consumer of projected presentation events for one session turn.

    Concrete sinks translate the closed ``PresentationEvent`` union onto
    their own surface (wire frames, transcript records, terminal
    rendering). ``handle`` is the single abstract data method; buffering
    sinks override ``flush`` to push their buffers.
    """

    @abstractmethod
    async def handle(self, event: PresentationEvent) -> None:
        """Consume one projected presentation event."""
        raise NotImplementedError

    async def flush(self) -> None:
        """Force-deliver any buffered output (consumer escape hatch).

        The primary flush boundary is the terminal event — buffering
        consumers push their buffers on ``turn_finished`` themselves.
        Concrete no-op default.
        """
        return None


class SessionEventHub(TurnEventSink):
    """Per-turn station: core ``TurnEvent``s in, ``PresentationEvent``s out.

    ``emit`` applies the gate, feeds the projector, then awaits each
    consumer's ``handle`` for every produced presentation event — in
    registration order, sequentially (delivery order is the observable
    contract; a slow consumer delays later ones by design). One hub
    serves one bound turn.

    Fan-out failure policy (shared with
    :class:`~modex_agent.core.emitter.CompositeTurnEventSink`, the other
    multi-consumer event sink): event delivery is observation, not turn
    semantics. A failure in one consumer's ``handle`` / ``flush`` is
    isolated and logged with the consumer's identity (class name) — it
    never propagates to the emitting runtime node, never converts a
    completed turn into an errored one, and never starves the remaining
    consumers.
    """

    def __init__(
        self,
        binding: TurnBinding,
        consumers: Sequence[PresentationSink],
        *,
        gate: KindGate | None = None,
        turn_id_factory: Callable[[], str] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if binding.resumed and not binding.turn_id:
            raise ValueError("a resumed TurnBinding must carry the suspended attempt's turn_id")
        super().__init__(gate)
        self._binding = binding
        self._consumers: tuple[PresentationSink, ...] = tuple(consumers)
        self._projector = DefaultTurnEventProjector(
            binding.session_id,
            # The binding owns the envelope's agent identity — one
            # assignment path, never a second derived one that could
            # disagree with it.
            agent_name=binding.agent_name,
            pool=binding.pool,
            workspace=binding.workspace,
            turn_id_factory=turn_id_factory or self._turn_id_from_binding,
            clock=clock,
        )
        if binding.resumed:
            # The suspended attempt already announced this turn: adopt the
            # binding's id and mark the turn active up front, so the resumed
            # leg produces no second TurnStarted (eager or lazy).
            self._projector.ensure_turn_started()

    @property
    def binding(self) -> TurnBinding:
        """The turn identity this hub is bound to."""
        return self._binding

    @property
    def projector(self) -> DefaultTurnEventProjector:
        """The hub's projection engine.

        Exposed for transcript-record helpers that produce records without
        a presentation event (``ensure_turn_started`` ahead of a folded
        record) — projection logic itself stays inside the hub.
        """
        return self._projector

    async def _dispatch(self, event: TurnEvent) -> None:
        for presentation in self._projector.feed(event):
            for consumer in self._consumers:
                # Failure isolation (the shared fan-out policy, see class
                # docstring): one consumer's handler defect must not
                # corrupt the turn outcome or starve the remaining
                # consumers — log and continue in registration order.
                try:
                    await consumer.handle(presentation)
                except Exception:
                    logger.exception(
                        "SessionEventHub consumer %s failed on %s event",
                        type(consumer).__name__,
                        presentation.kind,
                    )

    async def flush(self) -> None:
        for consumer in self._consumers:
            try:
                await consumer.flush()
            except Exception:
                logger.exception(
                    "SessionEventHub consumer %s flush failed",
                    type(consumer).__name__,
                )

    def _turn_id_from_binding(self) -> str:
        """Binding turn id when present; the projector's lazy uuid otherwise."""
        return self._binding.turn_id or uuid.uuid4().hex[:12]


__all__ = [
    "PresentationSink",
    "SessionEventHub",
]
