"""Typed exceptions for ``modexbot`` routing.

All exceptions inherit from a single ``ModexbotRoutingError`` base so callers
can catch every modexbot-specific failure with one ``except`` while still
matching each subtype via the standard ``except`` order. Each concrete
exception additionally mixes in the corresponding stdlib base
(``ValueError`` / ``KeyError``) so callers expecting those semantics (e.g.
generic validation handlers) still receive a recognisable error.
"""

from __future__ import annotations


class ModexbotRoutingError(Exception):
    """Base class for every routing / wiring failure raised by ``modexbot``."""


class MalformedSessionIdError(ModexbotRoutingError, ValueError):
    """Raised when ``MODEX_SESSION_ID`` lacks the ``"."`` separator.

    The prefix-reuse rule (ADR-0019) requires ``"{prefix}.{agent_name}"``;
    a bare ``"prefix"`` carries no agent segment and cannot mint a target
    session id.
    """


class UnknownTargetError(ModexbotRoutingError, KeyError):
    """Raised when ``target_name`` is not present in ``MODEX_AGENT_POOL_MAP``.

    The pool map is the single source of truth for routable agents;
    a name absent from it is unreachable.
    """


class SelfSendRejectedError(ModexbotRoutingError, ValueError):
    """Raised when ``--to`` resolves to the caller's own ``MODEX_AGENT_NAME``.

    Sending to oneself produces a useless round trip and may deadlock
    a busy inbox; reject at routing time so the failure surfaces before
    any filesystem mutation.
    """


__all__ = [
    "ModexbotRoutingError",
    "MalformedSessionIdError",
    "UnknownTargetError",
    "SelfSendRejectedError",
]
