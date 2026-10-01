"""DispatchDeadline — the unified watchdog deadline for agent turns.

Design
------

The pool watchdog is the **sole termination mechanism** for a turn. Provider
HTTP timeouts (``request_timeout`` / ``stream_idle_timeout``) intentionally
default to ``None`` so a long-running LLM generation (e.g. a 50K-token
document) is never killed mid-stream. Every phase that owns an internal
timeout instead **declares its budget into the deadline at entry**; phases
without one renew on activity signals. The watchdog therefore only fires on
genuine no-progress — never mid-phase, never racing an inner deadline.

Phase-budget protocol
---------------------

+---------------------------+---------------------------+---------------------------+------------------------------+
| Phase                     | Inner budget (graceful)   | Declaration site          | Amount                       |
+---------------------------+---------------------------+---------------------------+------------------------------+
| Tool call                 | ToolTimeoutInterceptor    | interceptor entry         | ``tool_timeout + margin``    |
|                           | ``asyncio.timeout`` → XML |                           |                              |
| Hook dispatch             | HookRunner ``wait_for``   | ``HookRunner.dispatch``   | ``hook_timeout×n + margin``  |
| Turn tail (flush + end)   | per-step ``wait_for``     | ``turn_runner`` finally   | ``flush + hook + margin``    |
| LLM call (react)          | none (watchdog is it)     | ``LLMNode`` pre-call      | ``dispatch_timeout``         |
| LLM stream chunk (react)  | — activity signal         | chunk callback            | ``chunk_renew_seconds``      |
| Provider event (external) | — activity signal         | ``on_emission`` callback  | ``chunk_renew_seconds``      |
+---------------------------+---------------------------+---------------------------+------------------------------+

``margin`` = ``2 × watchdog_poll_seconds`` (``DeadlinePolicy.phase_margin_seconds``):
the inner deadline always fires at least one full poll interval before the
outer watchdog can observe expiry, so the graceful inner path (XML timeout
result, hook timeout log, flush completion) is always reachable.

``renew()`` never shortens the deadline (takes ``max``) — declaration is a
floor, not a reset. A no-op when no deadline is set (clean mode /
``dispatch_timeout_seconds=0`` opt-out).

Sliding ceiling (max_ahead_seconds)
-----------------------------------

``renew(seconds)`` sets ``expires_at = max(old_expires, now + seconds)``,
capped at ``now + max_ahead_seconds`` (default 1200s = 20min).

This is a **sliding** ceiling, not a fixed absolute ceiling:

  - Each ``renew`` re-evaluates the cap relative to the *current* clock
    time, so continuous activity (frequent chunk renewals) can keep the
    turn alive indefinitely — the cap slides forward with each renewal.
  - The cap only prevents a single ``renew(huge)`` from pushing the
    deadline excessively far ahead. Without it, ``renew(999999)`` would
    make the watchdog ineffective if activity later stops.

With the phase-budget protocol, all legal declarations are config-derived
and validated at startup (``RuntimeSafetyPolicy`` model validator:
``max_ahead_seconds >= every phase budget + margin``), so the ceiling is a
panic fuse against unit bugs, not a behaviour knob.

In short: **as long as there is ongoing activity (chunks arriving, phases
declaring their budgets), the turn never times out. The watchdog fires only
when nothing has renewed the deadline for longer than its remaining budget.**
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
    * phase owners declare their full budget at entry (tool/hook/finalize
      interceptors) via ``renew(own_budget + margin)``
    * llm_client chunk callbacks renew() per chunk (instance default)
    * pool watchdog polls is_expired — the sole termination mechanism

    renew() never shortens the existing deadline (takes max) and never lets
    remaining exceed max_ahead_seconds from now (the sliding ceiling).
    """

    __slots__ = ("_expires_at", "_max_ahead", "_default_renew")

    DEFAULT_RENEW_SECONDS: float = 3.0
    DEFAULT_MAX_AHEAD_SECONDS: float = 1200.0

    def __init__(
        self,
        initial_timeout: float,
        *,
        max_ahead_seconds: float | None = None,
        default_renew_seconds: float | None = None,
    ) -> None:
        now = time.monotonic()
        self._expires_at: float = now + initial_timeout
        self._max_ahead: float = (
            max_ahead_seconds
            if max_ahead_seconds is not None
            else self.DEFAULT_MAX_AHEAD_SECONDS
        )
        self._default_renew: float = (
            default_renew_seconds
            if default_renew_seconds is not None
            else self.DEFAULT_RENEW_SECONDS
        )

    def renew(self, seconds: float | None = None) -> None:
        """Extend the deadline by *seconds* from now (None = instance default).

        Never shortens the existing deadline (takes max);
        never lets remaining exceed max_ahead_seconds from now (sliding ceiling).
        """
        amount = seconds if seconds is not None else self._default_renew
        now = time.monotonic()
        self._expires_at = min(
            max(self._expires_at, now + amount),
            now + self._max_ahead,
        )

    @property
    def is_expired(self) -> bool:
        return time.monotonic() >= self._expires_at

    @property
    def remaining(self) -> float:
        return max(0.0, self._expires_at - time.monotonic())


def renew_dispatch_deadline(seconds: float | None = None) -> None:
    """Renew the dispatch deadline on the current ContextVar (no-op if unset).

    ``seconds=None`` uses the deadline instance's default renewal amount
    (``DeadlinePolicy.chunk_renew_seconds`` on pool-created deadlines).
    """
    deadline = current_dispatch_deadline.get()
    if deadline is not None:
        deadline.renew(seconds)
