"""Session title business module — the single owner of title writes.

DESIGN §2 (personal-assistant-webui): ``SessionInfo.metadata.title`` is the
ONLY persisted title value. Three paths converge here:

- the manual rename HTTP adapter (``PATCH /api/sessions/{id}/title``),
- the automatic ``session_title`` hook's background naming task (PA-03),
- the GC deletion path (cancel naming task eligibility + the same runtime
  registry's ``cleanup``).

All three must reference the SAME runtime ``SessionRegistry`` the workspace
uses — never a second cache. This module owns the short write lock that
brackets "check no title → write", the pending-task table keyed by
``(pool, session_id)`` (the instance itself is per-workspace, so the
workspace dimension of the DESIGN key is the instance identity), and title
normalization. The registry's own lock keeps its cache/store coherent;
ours only serializes the two-step check-then-write against manual saves and
task cancellations.

The class is a plain runtime object (no Pydantic — it holds asyncio state
per rule 12's runtime-object exception).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Final

from modex_agent.core.session_id import SessionInfo

if TYPE_CHECKING:
    from modex_agent.persistence.session_registry import SessionRegistry

logger = logging.getLogger(__name__)

#: metadata key holding the one and only title value.
TITLE_METADATA_KEY: Final = "title"

#: maximum title length after trimming (DESIGN §2.6).
TITLE_MAX_LENGTH: Final = 80


class TitleValidationError(ValueError):
    """Raised when a proposed title fails the validation rules.

    The HTTP adapter maps this to ``400``; the background naming task
    treats it as "do not save".
    """


def normalize_title(raw: str) -> str:
    """Validate and normalize a raw title string.

    Rules (DESIGN §2.6): trim first, then require non-empty, single-line,
    at most :data:`TITLE_MAX_LENGTH` characters. Returns the trimmed title.
    """
    title = raw.strip()
    if not title:
        raise TitleValidationError("title must not be empty after trimming")
    if "\n" in title or "\r" in title:
        raise TitleValidationError("title must be a single line")
    if len(title) > TITLE_MAX_LENGTH:
        raise TitleValidationError(f"title must be at most {TITLE_MAX_LENGTH} characters")
    return title


def title_of(session: SessionInfo) -> str | None:
    """Read the display title from a session record, ``None`` when absent.

    A stored title that is not a non-empty trimmed string reads as absent
    (the frontend then shows the session id — DESIGN §2.1).
    """
    value = session.metadata.get(TITLE_METADATA_KEY)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


class SessionTitleOps:
    """Per-workspace title operations over the workspace's real registry.

    Owns the short title write lock and the pending background naming
    tasks (PA-03). Instantiated once per workspace resource bundle; HTTP,
    hook, and GC wiring all receive the SAME instance.
    """

    def __init__(self, *, registry: SessionRegistry, on_changed: Callable[[], None] | None = None) -> None:
        self._registry = registry
        self._on_changed = on_changed
        self._lock = asyncio.Lock()
        # Pending naming tasks keyed by (pool, session_id). Values are the
        # owning asyncio.Task identities; a task's write-back must re-check
        # it is still the registered entry (cancellation revokes first).
        self._pending: dict[tuple[str, str], asyncio.Task[None]] = {}

    @property
    def registry(self) -> SessionRegistry:
        return self._registry

    async def read_title(self, session_id: str) -> str | None:
        """Return the session's trimmed title, or ``None`` when absent."""
        session = await self._registry.get(session_id)
        if session is None:
            return None
        return title_of(session)

    async def set_title(self, session_id: str, raw_title: str) -> str:
        """Validate and persist ``metadata.title`` for an EXISTING session.

        Merge semantics: only the title key is submitted to
        ``registry.register`` — the registry merges other metadata keys,
        parent, and timestamps. A missing record raises ``LookupError``
        (HTTP 404); register is never used to implicitly create.

        Same-value saves are no-op successes (no store write, no
        ``updated_at`` bump).
        """
        title = normalize_title(raw_title)
        async with self._lock:
            existing = await self._registry.get(session_id)
            if existing is None:
                raise LookupError(f"session {session_id!r} not found")
            if title_of(existing) == title:
                return title
            await self._registry.register(
                SessionInfo(
                    session_id=existing.session_id,
                    agent_name=existing.agent_name,
                    metadata={TITLE_METADATA_KEY: title},
                )
            )
            self._notify_changed()
            return title

    def _notify_changed(self) -> None:
        if self._on_changed is not None:
            try:
                self._on_changed()
            except Exception:
                logger.warning("Session title saved but notification failed", exc_info=True)

    # ── Pending background naming tasks (PA-03 / PA-05) ─────────────────

    def bind_naming_task(
        self, pool: str, session_id: str, task: asyncio.Task[None]
    ) -> None:
        """Attach the real background task to a claimed naming slot."""
        self._pending[(pool, session_id)] = task

    def finish_naming(self, pool: str, session_id: str, task: asyncio.Task[None]) -> None:
        """Drop a pending entry — only when it still belongs to *task*.

        A cancelled old task must never remove a newer task's entry
        (DESIGN §2.4 rule 5).
        """
        key = (pool, session_id)
        if self._pending.get(key) is task:
            self._pending.pop(key, None)

    def cancel_naming(self, pool: str, session_id: str) -> asyncio.Task[None] | None:
        """Revoke naming eligibility and request cancellation.

        Called by the GC deletion path INSIDE the title lock's critical
        section (the caller holds it via :meth:`deletion_critical_section`);
        awaiting the task's exit happens OUTSIDE the lock (DESIGN §2.4
        rule 5). Returns the cancelled task for the caller to await.
        """
        key = (pool, session_id)
        task = self._pending.pop(key, None)
        if task is not None and not task.done():
            task.cancel()
        return task

    def cancel_naming_by_session(self, session_id: str) -> list[asyncio.Task[None]]:
        """Cancel every pending naming entry for *session_id* across pools.

        The GC path does not know the pool dimension; the session is being
        deleted as a whole, so all its pending entries go. Callers hold the
        deletion critical section; awaiting the returned tasks happens
        outside it.
        """
        cancelled: list[asyncio.Task[None]] = []
        for key in [k for k in self._pending if k[1] == session_id]:
            task = self._pending.pop(key)
            if not task.done():
                task.cancel()
                cancelled.append(task)
        return cancelled

    def has_pending_naming(self, pool: str, session_id: str) -> bool:
        return (pool, session_id) in self._pending

    async def auto_title(
        self, pool: str, session_id: str, raw_title: str, task: asyncio.Task[None]
    ) -> str | None:
        """PA-03 background-task write-back: only when still eligible.

        Eligibility (re-checked under the same short lock as manual saves):
        the task is still the pending entry for ``(pool, session_id)``,
        the session exists, and it has no title yet. Otherwise nothing is
        written and ``None`` is returned (manual rename wins, deleted
        session stays deleted, cancelled task stays silent).
        """
        title = normalize_title(raw_title)
        async with self._lock:
            if self._pending.get((pool, session_id)) is not task:
                return None
            existing = await self._registry.get(session_id)
            if existing is None or title_of(existing) is not None:
                self._pending.pop((pool, session_id), None)
                return None
            await self._registry.register(
                SessionInfo(
                    session_id=existing.session_id,
                    agent_name=existing.agent_name,
                    metadata={TITLE_METADATA_KEY: title},
                )
            )
            self._pending.pop((pool, session_id), None)
            self._notify_changed()
            return title

    def deletion_critical_section(self) -> asyncio.Lock:
        """Expose the shared short lock to the GC title-coordination entry."""
        return self._lock

    async def cleanup(self, session_id: str) -> None:
        """Revoke background writes and delete the durable/cache record together."""
        async with self._lock:
            cancelled = self.cancel_naming_by_session(session_id)
            await self._registry.cleanup(session_id)
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)


__all__ = [
    "SessionTitleOps",
    "TITLE_MAX_LENGTH",
    "TITLE_METADATA_KEY",
    "TitleValidationError",
    "normalize_title",
    "title_of",
]
