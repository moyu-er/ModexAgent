"""AcpSessionBackend — the async session seam replacing the old driver factory.

SDK-free ABCs (DESIGN.md §8): the bot supplies the project-bound production
backend, the framework ships a scripted one (``scripted.py``) for the bare
stdio entry and tests. ``backend.open`` covers both new and load; the returned
handle drives prompts/cancel and (for load-capable backends) exposes the typed
history snapshot. There is deliberately no configure/mode/model stub — later
capabilities extend this same seam.

Lifecycle contract:

- ``open`` raises :class:`~modex_agent.acp.types.AcpBackendError` instead of
  publishing a session (invalid cwd, project mismatch, boot failure, unknown
  session, unsupported input).
- ``handle.cancel`` must make the in-flight ``prompt`` return promptly with a
  cancelled ``AgentResult`` so the pending prompt RPC answers
  ``stop_reason="cancelled"`` (ACP hard requirement).
- ``backend.close`` releases process-level resources and closes its handles;
  the framework async serve's ``finally`` awaits it exactly once.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from modex_agent.core.emitter import AgentResult
from modex_agent.core.message import ChatMessage
from modex_agent.core.turn_events import TurnEvent

from .types import AcpOpenRequest, AcpPromptInput, PermissionChoice, PermissionPrompt

__all__ = ["AcpInteraction", "AcpSessionBackend", "AcpSessionHandle"]


class AcpInteraction(ABC):
    """Per-turn channel handed to ``AcpSessionHandle.prompt``.

    Framework facts flow out through ``emit`` (raw ``TurnEvent`` stream — the
    server maps them to wire updates); permission decisions flow in through
    ``request_decision`` (the server's ``session/request_permission``
    round-trip).
    """

    @abstractmethod
    async def emit(self, event: TurnEvent) -> None:
        """Emit one framework turn event for the in-flight prompt."""

    @abstractmethod
    async def request_decision(self, prompt: PermissionPrompt) -> PermissionChoice:
        """Ask the front-end to pick one of ``prompt.options`` (or cancel)."""


class AcpSessionHandle(ABC):
    """One open ACP session: id mapping plus serialized turn execution."""

    @property
    @abstractmethod
    def session_id(self) -> str:
        """Framework session id — equals the protocol ``sessionId``."""

    @abstractmethod
    async def prompt(self, input: AcpPromptInput, interaction: AcpInteraction) -> AgentResult:
        """Drive one turn to completion for the given typed prompt.

        The server serializes prompts per session (busy is rejected, never
        queued), so the handle sees at most one in-flight prompt.
        """

    @abstractmethod
    async def cancel(self) -> None:
        """Cancel the in-flight turn (ACP ``session/cancel`` notification)."""

    @abstractmethod
    async def read_history(self) -> list[ChatMessage]:
        """Typed source-history snapshot for load replay, in true order.

        Only called when the backend declares ``supports_load``. The server
        replays user/assistant text and tool calls/results from these facts;
        it never reads a store directly.
        """

    @abstractmethod
    async def close(self) -> None:
        """Release session-scoped resources (process teardown is the backend's)."""


class AcpSessionBackend(ABC):
    """Process-level session factory seam (new + load, project binding)."""

    @property
    @abstractmethod
    def supports_load(self) -> bool:
        """Whether ``open`` accepts load requests (drives the load capability)."""

    @abstractmethod
    async def open(self, request: AcpOpenRequest) -> AcpSessionHandle:
        """Open a session for one new/load request.

        Raises :class:`~modex_agent.acp.types.AcpBackendError` on rejection —
        the session is only published to the client when this returns.
        """

    @abstractmethod
    async def close(self) -> None:
        """Release process-level resources; must also close open handles.

        Awaited exactly once by the framework async serve's ``finally``;
        a no-op when nothing was ever opened.
        """
