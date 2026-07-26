"""Per-task environment ContextVars for the env-injection hook (Phase 2).

This module declares ONLY the two ContextVars the env-injection hook will
populate and the tool layer will read. No builder, hook, or store logic lives
here — Phase 2 wires the hook by calling ``_modex_env.set(...)`` /
``_current_session_id.set(...)`` directly. No setter helpers are exported.

Invariants:
1. The dict stored in ``_modex_env`` is read by multiple tools within the same
   ``asyncio.Task``. Readers MUST treat it as immutable.
   ``build_full_env``'s ``env.update(overrides)`` already complies — it copies
   the dict rather than mutating ``overrides`` in place.
2. ``_modex_env.get()`` / ``_current_session_id.get()`` are read ONLY at the
   tool layer (``CommandTool.execute`` / ``SubprocessExecutor.execute``). They
   MUST NOT be read inside backend / executor lambda / session internals.

Both ContextVars default to ``None``: with no hook installed, ``get()`` returns
``None`` and the tool layer falls back to its pre-hook behaviour — zero
breakage for existing callers.
"""
from __future__ import annotations

from contextvars import ContextVar

_modex_env: ContextVar[dict[str, str] | None] = ContextVar("modex_env", default=None)
_current_session_id: ContextVar[str | None] = ContextVar("current_session_id", default=None)
