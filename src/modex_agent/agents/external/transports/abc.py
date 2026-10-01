"""``ExternalTransport`` — the access-form seam of the external plane.

A transport owns ONE concern: driving the external coding agent for a
single turn and delivering what it observes as core
:class:`~modex_agent.core.turn_events.TurnEvent` records. Every
cross-transport concern (child-session routing, ``seq`` stamping, orphan
tool-result policy, turn lifecycle synthesis) belongs to
:class:`~modex_agent.agents.external.normalizer.ExternalEventNormalizer`
— transports never implement those themselves.

The seam is an ABC because two further access forms are planned besides
today's CLI subprocess transport (``OpenCodeTransport``, which spawns /
reuses an ``opencode serve`` process and reads its event stream):

1. **An editor-protocol client transport** — the external agent runs in
   an editor process and exchanges structured editor-protocol messages
   with the harness over a bidirectional channel. Same
   :class:`ExecOptions`, same terminal :class:`BackendResult`, same
   event callback — different wire.
2. **An in-process SDK bridge** — the provider exposes a Python SDK, so
   the "subprocess" collapses into direct awaitable calls. The ABC keeps
   the execution semantics (options in, terminal result out, events
   streamed through the callback) so the agent and normalizer are
   unchanged by either access form.

Only the contract is defined here today; no speculative implementations
accompany it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping

from modex_agent.core.turn_events import TurnEvent

from ..types import BackendResult, ExecOptions

TurnEventCallback = Callable[[TurnEvent], Awaitable[None]]
"""Delivers one core turn event observed on the main provider session."""

ChildTurnEventCallbackFactory = Callable[[str], TurnEventCallback]
"""Given a provider child session id, return that child's event callback.

Transports that observe provider-minted child sessions (runtime
subagents) call this factory at most once per child per ``execute`` and
route that child's events through the returned callback — never through
``on_event``. Core turn events carry no session identity, so the
transport's per-session demux is the only place the source session can
be attached.
"""


class StaleSessionError(Exception):
    """Raised by a transport when the provider session id is no longer valid.

    The harness catches this, invalidates the stored mapping via
    :meth:`~modex_agent.agents.external.session_store.ExternalSessionMapStore.invalidate`,
    and retries once with a fresh session (``resume_session_id=None``). A
    second failure propagates.
    """


class ExternalTransport(ABC):
    """One access form's execution contract for a single external turn.

    ``execute`` drives the external coding agent once: it receives the
    per-spawn options, the resolved spawn environment (``MODEX_*`` vars
    included), and the event callbacks produced by the normalizer; it
    returns the terminal :class:`BackendResult`. Process/connection
    lifecycle management beyond a single ``execute`` (shared servers,
    client pools) belongs to the concrete transport — including its
    :meth:`close`, released by the pool provider at shutdown.
    """

    @abstractmethod
    async def execute(
        self,
        opts: ExecOptions,
        env: Mapping[str, str],
        on_event: TurnEventCallback,
        on_child_event: ChildTurnEventCallbackFactory | None = None,
    ) -> BackendResult:
        """Execute one turn and stream its events as core ``TurnEvent``s.

        Args:
            opts: Per-spawn execution options. ``resume_session_id``
                continues a known provider session; the next session id
                is echoed back on the returned result.
            env: Resolved spawn environment (all ``MODEX_*`` vars).
            on_event: Delivery target for events observed on the main
                provider session.
            on_child_event: Factory for delivery targets of provider
                child sessions. ``None`` means the caller observes no
                child sessions — child events may then be dropped by
                the transport.

        Returns:
            The terminal ``BackendResult`` (``completed`` / ``failed`` /
            ``timeout`` / ``aborted``).

        Raises:
            StaleSessionError: ``opts.resume_session_id`` is no longer
                valid; the harness retries once with a fresh session.
        """
        raise NotImplementedError

    async def close(self) -> None:
        """Release resources held beyond a single execute.

        Default no-op. Transports that hold external resources across
        turns (shared server processes, connection pools) override this.
        Called by :meth:`BackendProvider.close_all
        <modex_agent.agents.external.backend_provider.BackendProvider.close_all>`
        during pool shutdown.
        """
        return None


__all__ = [
    "ChildTurnEventCallbackFactory",
    "ExternalTransport",
    "StaleSessionError",
    "TurnEventCallback",
]
