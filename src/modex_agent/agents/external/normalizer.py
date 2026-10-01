"""``ExternalEventNormalizer`` — the external plane's shared event normalizer.

Wraps ANY transport's raw core-event stream and owns every
cross-transport concern, so no transport implements these itself:

- **Child-session routing** — events observed on a provider-minted child
  session route to that child's sink: the
  :class:`~modex_agent.agents.external.child_discovery.ChildSessionDiscoverySink`
  mapping resolves the deterministic modex session id, the child sink
  factory is called with a :class:`~modex_agent.core.emitter.TurnBinding`
  for the child session, and lineage registration fires as a tracked
  background task gathered at :meth:`ExternalEventNormalizer.cleanup`.
  The first event of a child triggers discovery synchronously (no await
  race window); a per-provider-session child env snapshot is written so
  the child's ``modexctl`` calls resolve the child identity.
- **``seq`` stamping** — every delivered
  :class:`~modex_agent.core.turn_events.TurnToolResultEvent` carries a
  per-session turn counter in ``seq`` (the union field stays
  ``int | None`` for the native resumed-approval case; the external
  plane always fills it).
- **Orphan policy** — a tool result whose ``call_id`` has no preceding
  tool call in the same session this turn is dropped with a warning.
- **Turn lifecycle synthesis** — :meth:`begin` emits ``TurnStartedEvent``
  and exactly one terminal ``TurnFinishedEvent`` is emitted via
  :meth:`finish` (terminal status) or :meth:`fail` (turn exception),
  reusing the terminal-status → ``StopReason`` mapping.
- **Watchdog renewal** — every transport event renews the pool dispatch
  deadline (the same activity-signal protocol as native stream chunks).

The normalizer also accumulates the main session's text so the harness
can build its :class:`~modex_agent.core.emitter.AgentResult` content.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping

from modex_agent.core.agent import AgentCommKind
from modex_agent.core.emitter import TurnBinding, TurnEventSink, TurnEventSinkFactory
from modex_agent.core.session_id import agent_of
from modex_agent.core.turn.dispatch import renew_dispatch_deadline
from modex_agent.core.turn_events import (
    StopReason,
    TurnErroredEvent,
    TurnEvent,
    TurnFinishedEvent,
    TurnStartedEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)

from .child_discovery import ChildSessionDiscoverySink
from .env_builder import ExternalEnvBuilder, write_env_snapshot_for_session
from .paths import ExternalPaths
from .transports.abc import TurnEventCallback
from .types import BackendResult, BackendStatus, ExternalEnvSpec

logger = logging.getLogger(__name__)

_CHILD_AGENT_NAME = "external-subagent"

__all__ = [
    "ExternalEventNormalizer",
    "error_of_backend_result",
    "stop_reason_of",
]


def stop_reason_of(status: BackendStatus) -> StopReason:
    """Map a terminal :class:`BackendStatus` onto the provider-neutral stop reason."""
    match status:
        case BackendStatus.COMPLETED:
            return StopReason.COMPLETED
        case BackendStatus.FAILED:
            return StopReason.ERROR
        case BackendStatus.TIMEOUT:
            return StopReason.TIMEOUT
        case BackendStatus.ABORTED:
            return StopReason.CANCELLED


def error_of_backend_result(result: BackendResult) -> str | None:
    """The failure text a terminal result carries (``None`` when healthy).

    A non-completed status without provider error text still describes a
    failure — synthesize the status description so the terminal event and
    the ``AgentResult`` agree.
    """
    if result.status is BackendStatus.COMPLETED:
        return result.error
    return result.error or f"provider exited with status {result.status}"


class _SessionRoute:
    """Per-session delivery state for one turn (main session or one child).

    Regular class, not a frozen value object: it holds mutable per-turn
    counters and buffers (rules/type-safety.md rule 11).
    """

    def __init__(self, sink: TurnEventSink) -> None:
        self.sink = sink
        self.text: list[str] = []
        self.open_call_ids: set[str] = set()
        self._seq = 0

    def next_seq(self) -> int:
        seq = self._seq
        self._seq += 1
        return seq


class ExternalEventNormalizer:
    """One external turn's delivery pipeline: transport events → sinks.

    Constructed per turn by the harness. The transport-facing surface is
    :meth:`on_event` (main session) and :meth:`on_child_event` (factory
    for provider child sessions); the harness-facing surface is
    :meth:`begin` / :meth:`finish` / :meth:`fail` / :meth:`cleanup` and
    the :attr:`text` accumulator.
    """

    def __init__(
        self,
        *,
        parent_sink: TurnEventSink,
        modex_sid: str,
        paths: ExternalPaths,
        spec: ExternalEnvSpec,
        base_env: Mapping[str, str],
        child_discovery_sink: ChildSessionDiscoverySink | None = None,
        child_sink_factory: TurnEventSinkFactory | None = None,
    ) -> None:
        self._main = _SessionRoute(sink=parent_sink)
        self._modex_sid = modex_sid
        self._paths = paths
        self._spec = spec
        self._base_env: dict[str, str] = dict(base_env)
        self._child_discovery_sink = child_discovery_sink
        self._child_sink_factory = child_sink_factory
        self._child_routes: dict[str, _SessionRoute] = {}
        self._pending_tasks: set[asyncio.Task[str]] = set()
        self._finished = False

    # -- Transport-facing surface -------------------------------------------

    async def on_event(self, event: TurnEvent) -> None:
        """Deliver one event observed on the main provider session."""
        renew_dispatch_deadline()
        await self._deliver(self._main, event)

    def on_child_event(self, provider_child_sid: str) -> TurnEventCallback:
        """Build the delivery callback for one provider child session.

        Called by the transport at most once per child per execution.
        The first event of the child runs discovery synchronously (resolve
        + sink creation + env snapshot) before delivery — no await race
        window; the async lineage registration fires as a background task
        gathered at :meth:`cleanup`.
        """

        async def _handler(event: TurnEvent) -> None:
            renew_dispatch_deadline()
            route = self._child_route(provider_child_sid)
            if route is None:
                return
            await self._deliver(route, event)

        return _handler

    # -- Harness-facing surface ----------------------------------------------

    async def begin(self) -> None:
        """Emit the turn's ``TurnStartedEvent`` (before any content event)."""
        await self._main.sink.emit(TurnStartedEvent())

    async def finish(self, result: BackendResult) -> None:
        """Emit the exactly-once terminal event for a terminal backend status."""
        await self._emit_finished_once(
            stop_reason_of(result.status), error_of_backend_result(result)
        )

    async def fail(self, error: str) -> None:
        """Emit the exactly-once terminal pair for a turn that raised.

        An error observation (``TurnErroredEvent``) followed by the
        terminal ``TurnFinishedEvent`` — mirrors the harness's historic
        exception path.
        """
        if self._finished:
            return
        self._finished = True
        await self._main.sink.emit(TurnErroredEvent(message=error))
        await self._main.sink.emit(TurnFinishedEvent(stop_reason=StopReason.ERROR, error=error))

    async def cleanup(self) -> None:
        """Gather background child-registration tasks and drop per-turn state.

        Called by the harness in the turn's ``finally`` block: a blocked
        registration holds the turn open; a failure is logged, never
        raised (the turn outcome is already decided).
        """
        if self._pending_tasks:
            results = await asyncio.gather(*self._pending_tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, BaseException):
                    logger.warning("Child discovery side-effect failed: %s", r)
        self._pending_tasks.clear()
        self._child_routes.clear()

    @property
    def text(self) -> str:
        """The main session's accumulated assistant text for this turn."""
        return "".join(self._main.text)

    # -- Internals -------------------------------------------------------------

    async def _deliver(self, route: _SessionRoute, event: TurnEvent) -> None:
        match event:
            case TurnTextEvent(text=text) if text:
                route.text.append(text)
                await route.sink.emit(event)
            case TurnToolCallEvent(call_id=call_id):
                route.open_call_ids.add(call_id)
                await route.sink.emit(event)
            case TurnToolResultEvent(call_id=call_id):
                if call_id not in route.open_call_ids:
                    logger.warning(
                        "Tool result with call_id=%s has no preceding tool call; dropping",
                        call_id,
                    )
                    return
                route.open_call_ids.discard(call_id)
                await route.sink.emit(event.model_copy(update={"seq": route.next_seq()}))
            case _:
                await route.sink.emit(event)

    async def _emit_finished_once(self, stop_reason: StopReason, error: str | None) -> None:
        if self._finished:
            return
        self._finished = True
        await self._main.sink.emit(TurnFinishedEvent(stop_reason=stop_reason, error=error))

    def _child_route(self, provider_child_sid: str) -> _SessionRoute | None:
        """Resolve (and on first sight, create) the route for a child session.

        Returns ``None`` when the child cannot be routed (discovery
        collaborators not configured) — the event is dropped with a
        warning, matching the historic no-collaborators behavior.
        """
        route = self._child_routes.get(provider_child_sid)
        if route is not None:
            return route
        if self._child_discovery_sink is None or self._child_sink_factory is None:
            logger.warning(
                "Child emission from provider session %s dropped — "
                "no discovery collaborators configured",
                provider_child_sid,
            )
            return None
        child_modex_sid = self._child_discovery_sink.resolve_child_modex_session_id(
            provider_child_sid
        )
        child_binding = TurnBinding(
            session_id=child_modex_sid,
            agent_name=agent_of(child_modex_sid, default="external-child"),
        )
        route = _SessionRoute(sink=self._child_sink_factory(child_binding))
        self._child_routes[provider_child_sid] = route
        task = asyncio.create_task(
            self._child_discovery_sink.on_child_discovered(provider_child_sid, self._modex_sid)
        )
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)
        self._write_child_env_snapshot(provider_child_sid, child_modex_sid)
        return route

    def _write_child_env_snapshot(
        self,
        provider_child_sid: str,
        child_modex_sid: str,
    ) -> None:
        """Write a per-provider-session env snapshot for a discovered child.

        The child spec mirrors the parent's workspace / inbox / workdir /
        pool-map / targets / modexctl paths, but overrides session_id,
        provider_session_id, agent_name, comm_kind, and parent_session_id
        for the child identity. ``comm_kind=SUBAGENT`` triggers
        ``MODEX_PARENT_SESSION_ID`` injection so modexctl routes child
        sends to the parent verbatim.
        """
        child_spec = self._spec.model_copy(
            update={
                "session_id": child_modex_sid,
                "provider_session_id": provider_child_sid,
                "agent_name": _CHILD_AGENT_NAME,
                "comm_kind": AgentCommKind.SUBAGENT,
                "parent_session_id": self._modex_sid,
            }
        )
        child_env = ExternalEnvBuilder.build(child_spec, self._base_env)
        write_env_snapshot_for_session(self._paths, child_env, provider_child_sid)
