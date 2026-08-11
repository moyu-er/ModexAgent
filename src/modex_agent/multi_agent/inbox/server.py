"""InboxMQ ABC — the agent inbox message-queue contract (T11).

Evolved from the legacy :class:`InboxServer` ABC. The contract formalizes
the topic lifecycle (``pending → active → idle → expired``) per PRD story 44
and adds two surfaces:

- :meth:`InboxMQ.deliver` — **sync** cross-process delivery for CLI use
  (SQLite ``deliver()`` owns a DB path and opens its own short-lived stdlib
  ``sqlite3`` connection; it never reuses the server's async connection).
  The FILE backend writes directly to the pending file. NOTE: the production
  CLI (``modexctl send``) currently enters the bot process via HTTP and
  converges on ``LocalAgentMessageBus.send`` → :meth:`receive`, so
  :meth:`deliver` is retained as the ABC contract for direct-CLI backends
  but is not on the hot path of the reference bot.
- :meth:`InboxMQ.reap_expired` — TTL cleanup of expired messages and
  delivered-id records.

Between-turn delivery latency is driven by the ``InboxPoller``, which is
event-driven via a pool-level ``Event`` signalled from
``LocalAgentMessageBus.send`` (the single convergence point of all inbox
writers); the poller still ticks as a defensive fallback.

Delivered-id tracking is **internal** to the MQ transaction (PRD story 23):
the standalone :class:`~modex_agent.multi_agent.inbox.tracker.DeliveredIdTracker`
ABC is deprecated; concrete MQ implementations own their dedup store.

``InboxServer`` is kept as a deprecated alias for :class:`InboxMQ` during the
transition (T11 expand phase). New code should depend on ``InboxMQ``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .types import InboxMessage

__all__ = ["InboxMQ", "InboxServer"]


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

        Removes and returns up to ``limit`` messages from the pending queue.
        If ``only_types`` is non-empty, only messages whose ``message_type``
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
