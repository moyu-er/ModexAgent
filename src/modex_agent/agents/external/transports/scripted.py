"""``ScriptedTransport`` — deterministic in-memory transport test double.

Lives under ``src/`` (not ``tests/``) because integration tests import it
from production code paths to stand in for a real CLI subprocess — "the
same routing code path the real transport uses, only the subprocess
boundary is faked". Steps carry core :class:`TurnEvent` records directly
(no provider wire format), optionally tagged with a provider child
session id so child-session routing can be exercised without a provider
that mints children.

Behavior:

- Records each ``execute`` call's ``ExecOptions`` and env
  (``recorded_opts`` / ``recorded_envs``) so tests can assert routing
  and env correctness without a real subprocess.
- Plays back the step list in order: each step's event is delivered to
  ``on_event`` (main session) or to the callback obtained from
  ``on_child_event(step.source_session_id)``; side-effect-marked steps
  invoke the registered callable with the captured ``ExecOptions`` —
  mirroring the real provider's "emit an event, then the LLM triggers
  ``modexctl send``" cadence.
- Returns a ``BackendResult`` whose ``status`` and ``session_id`` come
  from the :class:`ScriptedProgramme`.
- Holds NO state between ``execute`` calls beyond the recordings —
  concurrent calls would race, but every test uses one-and-done.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping

from pydantic import BaseModel, ConfigDict

from modex_agent.core.turn_events import TurnEvent

from ..types import BackendResult, BackendStatus, ExecOptions
from .abc import (
    ChildTurnEventCallbackFactory,
    ExternalTransport,
    TurnEventCallback,
)

__all__ = [
    "ScriptedProgramme",
    "ScriptedStep",
    "ScriptedTransport",
    "SendSideEffect",
]


class ScriptedStep(BaseModel):
    """A single step in a :class:`ScriptedTransport` playback.

    Attributes:
        event: The core turn event the transport "observes" at this step.
            ``None`` marks a side-effect-only step (no event delivered).
        source_session_id: Provider session the event originates from.
            ``None`` (default) is the main session; a string marks a
            provider child session, delivered through the callback from
            the ``on_child_event`` factory.
        side_effect: When ``True``, the transport awaits the registered
            side-effect callable with the captured ``ExecOptions`` after
            delivering the event — BEFORE the next step plays.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event: TurnEvent | None = None
    source_session_id: str | None = None
    side_effect: bool = False


class ScriptedProgramme(BaseModel):
    """The closed-loop event sequence for :class:`ScriptedTransport`.

    Attributes:
        steps: Ordered tuple of :class:`ScriptedStep` records. Empty
            means a no-event "happy completion" run.
        status: ``BackendResult.status`` value to return on completion.
        session_id: ``BackendResult.session_id`` to return (e.g. the
            "next" provider session id for resume support, or ``None``
            for fresh sessions / empty programmes).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    steps: tuple[ScriptedStep, ...] = ()
    status: BackendStatus = BackendStatus.COMPLETED
    session_id: str | None = None


SendSideEffect = Callable[[ExecOptions], Awaitable[None]]
"""Async callable invoked at steps marked ``side_effect=True``."""


class ScriptedTransport(ExternalTransport):
    """Transport test double — records, plays back, side-effects."""

    def __init__(
        self,
        programme: ScriptedProgramme,
        send_side_effect: SendSideEffect | None = None,
    ) -> None:
        super().__init__()
        self._programme = programme
        self._send_side_effect: SendSideEffect | None = send_side_effect
        self.recorded_opts: list[ExecOptions] = []
        self.recorded_envs: list[dict[str, str]] = []

    @property
    def programme(self) -> ScriptedProgramme:
        """The programme this transport is replaying."""
        return self._programme

    def register_send_side_effect(self, fn: SendSideEffect) -> None:
        """Register the async side-effect callable invoked at marked steps."""
        self._send_side_effect = fn

    async def execute(
        self,
        opts: ExecOptions,
        env: Mapping[str, str],
        on_event: TurnEventCallback,
        on_child_event: ChildTurnEventCallbackFactory | None = None,
    ) -> BackendResult:
        self.recorded_opts.append(opts)
        self.recorded_envs.append(dict(env))
        child_callbacks: dict[str, TurnEventCallback] = {}
        for step in self._programme.steps:
            # Deliver the step's event first (mirrors the real provider: an
            # event lands on the stream, THEN the LLM may trigger a tool).
            if step.event is not None:
                if step.source_session_id is None:
                    await on_event(step.event)
                else:
                    callback = child_callbacks.get(step.source_session_id)
                    if callback is None:
                        if on_child_event is None:
                            raise ValueError(
                                "Scripted step targets child session "
                                f"{step.source_session_id!r} but no on_child_event "
                                "factory was supplied"
                            )
                        callback = on_child_event(step.source_session_id)
                        child_callbacks[step.source_session_id] = callback
                    await callback(step.event)
            if step.side_effect and self._send_side_effect is not None:
                await self._send_side_effect(opts)
            # Yield so downstream coroutines interleave deterministically.
            await asyncio.sleep(0)
        return BackendResult(
            status=self._programme.status,
            session_id=self._programme.session_id,
        )
