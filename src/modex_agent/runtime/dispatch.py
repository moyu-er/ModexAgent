"""DispatchDeadline — renewable dispatch timeout with a hard ceiling.

Pool creates a DispatchDeadline at dispatch start and propagates it via ContextVar.
LLM streaming chunk callbacks renew 3s per chunk; each completed LLM iteration
renews by agent_run_timeout. The pool watchdog coroutine checks for expiry.

Hard ceiling: renew() will never push the deadline past born_at + max_total_seconds
(default 600s = 10min). This prevents indefinite turn extension from continuous
chunk renewals.
"""

from __future__ import annotations

import time
from contextvars import ContextVar

__all__ = ["DispatchDeadline", "current_dispatch_deadline", "renew_dispatch_deadline"]

current_dispatch_deadline: ContextVar[DispatchDeadline | None] = ContextVar(
    "current_dispatch_deadline",
    default=None,
)


class DispatchDeadline:
    """Renewable monotonic-clock deadline with a hard ceiling.

    * pool._run_dispatch creates and injects via ContextVar
    * llm_client chunk callbacks call renew() (default 3s)
    * nodes/llm.py calls renew(agent_run_timeout) after each LLM iteration
    * pool watchdog polls is_expired

    renew() never shortens the existing deadline (takes max) and never exceeds
    born_at + max_total_seconds (the hard ceiling).
    """

    __slots__ = ("_born_at", "_expires_at", "_max_expires_at")

    DEFAULT_RENEW_SECONDS: float = 3.0
    DEFAULT_MAX_TOTAL_SECONDS: float = 600.0

    def __init__(
        self,
        initial_timeout: float,
        *,
        max_total_seconds: float | None = None,
    ) -> None:
        self._born_at: float = time.monotonic()
        self._expires_at: float = self._born_at + initial_timeout
        ceiling = (
            max_total_seconds
            if max_total_seconds is not None
            else self.DEFAULT_MAX_TOTAL_SECONDS
        )
        self._max_expires_at: float = self._born_at + ceiling

    def renew(self, seconds: float = DEFAULT_RENEW_SECONDS) -> None:
        """Extend the deadline by *seconds* from now.

        Never shortens the existing deadline (takes max);
        never exceeds born_at + max_total_seconds (hard ceiling).
        """
        self._expires_at = min(
            max(self._expires_at, time.monotonic() + seconds),
            self._max_expires_at,
        )

    @property
    def is_expired(self) -> bool:
        return time.monotonic() >= self._expires_at

    @property
    def remaining(self) -> float:
        return max(0.0, self._expires_at - time.monotonic())


def renew_dispatch_deadline(seconds: float = DispatchDeadline.DEFAULT_RENEW_SECONDS) -> None:
    """Renew the dispatch deadline on the current ContextVar (no-op if unset)."""
    deadline = current_dispatch_deadline.get()
    if deadline is not None:
        deadline.renew(seconds)
