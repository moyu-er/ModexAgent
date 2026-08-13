"""DispatchDeadline — renewable dispatch timeout with a sliding forward ceiling.

Design
------

The LLM call chain has three layers that could each impose a timeout:

  1. Provider HTTP timeout (request_timeout / stream_idle_timeout)
  2. Per-iteration renewal (agent_run_timeout, called by nodes/llm.py)
  3. Pool watchdog (polls DispatchDeadline.is_expired)

Layers 1 and 2 are intentionally disabled at the provider level (None) so
that the **pool watchdog** (layer 3) is the sole termination mechanism.
This lets a long-running LLM generation (e.g. a 50K-token document) run to
completion without the provider killing the HTTP stream mid-output.

DispatchDeadline is a **renewable monotonic-clock deadline**:

  - ``pool._run_dispatch`` creates one at dispatch start and injects it via
    ContextVar.
  - ``llm_client`` streaming chunk callbacks call ``renew()`` (default +3s)
    on every content/reasoning delta — so as long as the LLM is actively
    producing output, the deadline keeps sliding forward.
  - ``nodes/llm.py`` calls ``renew(agent_run_timeout)`` after each completed
    LLM iteration — a larger renewal that covers tool execution + next
    iteration.
  - The pool watchdog coroutine polls ``is_expired`` and kills the turn
    only when the deadline is truly past.

Sliding ceiling (max_ahead_seconds)
-----------------------------------

``renew(seconds)`` sets ``expires_at = max(old_expires, now + seconds)``,
but caps it at ``now + max_ahead_seconds`` (default 1200s = 20min).

This is a **sliding** ceiling, not a fixed absolute ceiling:

  - Each ``renew`` re-evaluates the cap relative to the *current* clock
    time, so continuous activity (frequent chunk renewals) can keep the
    turn alive indefinitely — the cap slides forward with each renewal.
  - The cap only prevents a single ``renew(huge)`` from pushing the
    deadline excessively far ahead. Without it, ``renew(999999)`` would
    make the watchdog ineffective if activity later stops.

In short: **as long as there is ongoing activity (chunks arriving), the
turn never times out. The ceiling only bounds how far ahead a single
renewal can reach, not how long the turn can live in total.**
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
    """Renewable monotonic-clock deadline with a sliding forward ceiling.

    * pool._run_dispatch creates and injects via ContextVar
    * llm_client chunk callbacks call renew() (default 3s)
    * nodes/llm.py calls renew(agent_run_timeout) after each LLM iteration
    * pool watchdog polls is_expired

    renew() never shortens the existing deadline (takes max) and never lets
    remaining exceed max_ahead_seconds from now (the sliding ceiling).
    """

    __slots__ = ("_expires_at", "_max_ahead")

    DEFAULT_RENEW_SECONDS: float = 3.0
    DEFAULT_MAX_AHEAD_SECONDS: float = 1200.0

    def __init__(
        self,
        initial_timeout: float,
        *,
        max_ahead_seconds: float | None = None,
    ) -> None:
        now = time.monotonic()
        self._expires_at: float = now + initial_timeout
        self._max_ahead: float = (
            max_ahead_seconds
            if max_ahead_seconds is not None
            else self.DEFAULT_MAX_AHEAD_SECONDS
        )

    def renew(self, seconds: float = DEFAULT_RENEW_SECONDS) -> None:
        """Extend the deadline by *seconds* from now.

        Never shortens the existing deadline (takes max);
        never lets remaining exceed max_ahead_seconds from now (sliding ceiling).
        """
        now = time.monotonic()
        self._expires_at = min(
            max(self._expires_at, now + seconds),
            now + self._max_ahead,
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
