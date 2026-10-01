"""Inbox MQ contract — the agent inbox message-queue ABC and message types.

The ABC and the :class:`InboxMessage` record sank to core (W3b) so the
persistence adapters (SQLite) and the multi-agent inbox backends (file,
memory) both import the contract from the one place below both packages.
The concrete backends stay in ``modex_agent.multi_agent.inbox``.

Topic lifecycle (``pending → active → idle → expired``, PRD story 44) and
the sync cross-process ``deliver()`` surface are documented on the ABC.

``InboxServer`` is kept as a deprecated alias for ``InboxMQ`` during the T11
transition; new code should depend on ``InboxMQ``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any, Final
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

SESSION_WORK_METADATA_KEY: Final = "session_tree_work"


class InboxMessage(BaseModel):
    """A single message carried in — and persisted by — the Inbox."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    source: str
    content: str
    message_type: str
    message_id: str = Field(default_factory=lambda: uuid4().hex)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionWork(BaseModel):
    """Reserved inbox messages, retired only after their receiver processes them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pending: tuple[InboxMessage, ...] = ()


class InboxMQ(ABC):
    """Agent inbox message-queue abstraction.

    Topic lifecycle (PRD story 44):

    ``pending → active → idle → expired``

    - **pending**: message received via :meth:`receive` or :meth:`deliver`,
      awaiting :meth:`consume`.
    - **active**: message consumed by a turn in progress.
    - **idle**: no pending messages, no active turn for the session.
    - **expired**: message or delivered-id record past its TTL, removed by
      :meth:`reap_expired`.

    All async methods are safe to call from a single event loop. The sync
    :meth:`deliver` is the **only** method safe to call from non-async
    (CLI) code; it must not share the async server's connection or locks.
    """

    # ------------------------------------------------------------------ #
    # Async MQ surface (server-side, framework process)
    # ------------------------------------------------------------------ #

    @abstractmethod
    async def receive(self, session_id: str, message: InboxMessage) -> bool:
        """Idempotent intake: same ``message_id`` never enters pending twice.

        Returns ``True`` if the message is new and persisted; ``False`` if it
        was a duplicate (already pending or already delivered).
        """
        ...

    @abstractmethod
    async def consume(
        self,
        session_id: str,
        limit: int = 100,
        *,
        only_types: set[str] | None = None,
    ) -> list[InboxMessage]:
        """Atomic FIFO consume with exactly-once delivery.

        Removes and returns up to *limit* messages from the pending queue.
        If *only_types* is non-empty, only messages whose ``message_type``
        is in the set are consumed; non-matching messages stay pending (FIFO
        order preserved). Delivered ids are recorded in the same transaction.
        """
        ...

    @abstractmethod
    async def peek(self, session_id: str) -> list[InboxMessage]:
        """Non-destructive read of the pending queue (no state change)."""
        ...

    @abstractmethod
    async def contains_pending(self, session_id: str, message_id: str) -> bool:
        """Return whether ``message_id`` is pending for ``session_id``."""
        ...

    @abstractmethod
    async def count(self, session_id: str) -> int:
        """Return the number of pending messages for ``session_id``."""
        ...

    @abstractmethod
    async def clear(self, session_id: str) -> None:
        """Clear pending queue and delivered-id records for ``session_id``."""
        ...

    @abstractmethod
    async def sessions_with_pending(self) -> list[str]:
        """Return session ids with ≥1 pending message (``count > 0``).

        Distinct from :meth:`list_sessions` (which includes now-empty sessions).
        """
        ...

    # ------------------------------------------------------------------ #
    # Sync delivery surface (CLI cross-process)
    # ------------------------------------------------------------------ #

    @abstractmethod
    def deliver(self, session_id: str, message: InboxMessage) -> bool:
        """**Sync** cross-process delivery — for CLI use (``modexctl send``).

        Contract:

        - **SQLite backend**: owns the DB path and opens its own short-lived
          stdlib ``sqlite3`` connection (``BEGIN IMMEDIATE`` … ``COMMIT`` …
          ``close``). It **never** reuses the server's long-lived async
          ``aiosqlite`` connection.
        - **FILE backend**: writes directly to ``pending.jsonl`` (best-effort;
          cross-process atomicity is a known gap that the SQLite backend
          closes).

        Same idempotency semantics as :meth:`receive`: returns ``True`` if the
        message is new, ``False`` if duplicate.
        """
        ...

    # ------------------------------------------------------------------ #
    # Lifecycle maintenance
    # ------------------------------------------------------------------ #

    @abstractmethod
    async def reap_expired(self) -> int:
        """Delete expired messages and stale delivered-id records (TTL).

        Returns the number of items removed. Implementations without a TTL
        policy (FILE, in-memory) return ``0``.
        """
        ...

    # ------------------------------------------------------------------ #
    # Non-abstract convenience (kept for backwards compatibility)
    # ------------------------------------------------------------------ #

    async def list_sessions(self) -> list[str]:
        """Return all known session ids (default empty; override for real).

        Distinct from :meth:`sessions_with_pending` (which filters by
        ``count > 0``). Not part of the formal ``InboxMQ`` contract; kept
        for implementations that already expose it.
        """
        return []


# Deprecated alias — new code should use ``InboxMQ``. Kept during the T11
# transition so existing imports and type hints continue to work.
InboxServer = InboxMQ

__all__ = [
    "InboxMQ",
    "InboxMessage",
    "InboxServer",
    "SESSION_WORK_METADATA_KEY",
    "SessionWork",
]
