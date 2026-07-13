"""Pure routing functions for ``modexbot send``.

This module is now a thin typed facade over :mod:`modexctl.main`.  The
env-driven routing decisions (target session id, target pool, JSONL line
shape) are delegated to the production CLI so the two CLIs share a
single source of truth for the on-disk format.
"""

from __future__ import annotations

from modex_agent.agents.external_coding import ExternalEnvSpec
from modexctl.main import (
    _build_inbox_line as _modexctl_build_inbox_line,
)
from modexctl.main import (
    _compute_target_session_id as _modexctl_compute_target_session_id,
)
from modexctl.main import (
    _MalformedSessionIdError as _ModexctlMalformedSessionIdError,
)
from modexctl.main import (
    _resolve_target_pool as _modexctl_resolve_target_pool,
)
from modexctl.main import (
    _UnknownTargetError as _ModexctlUnknownTargetError,
)

from .errors import MalformedSessionIdError, UnknownTargetError


def _compute_target_session_id(env: ExternalEnvSpec) -> str:
    """Extract the sender's session prefix per ADR-0019's prefix-reuse rule.

    Delegates to :func:`modexctl.main._compute_target_session_id` and
    translates the private exception into the public
    :class:`MalformedSessionIdError`.
    """
    try:
        return _modexctl_compute_target_session_id(env.session_id)
    except _ModexctlMalformedSessionIdError as exc:
        raise MalformedSessionIdError(str(exc)) from exc


def _resolve_target_pool(env: ExternalEnvSpec, target_name: str) -> str:
    """Resolve ``target_name`` to its pool name via ``MODEX_AGENT_POOL_MAP``.

    Delegates to :func:`modexctl.main._resolve_target_pool` and
    translates the private exception into the public
    :class:`UnknownTargetError`.
    """
    try:
        return _modexctl_resolve_target_pool(env.agent_pool_map, target_name)
    except _ModexctlUnknownTargetError as exc:
        raise UnknownTargetError(str(exc)) from exc


def _build_inbox_line(
    env: ExternalEnvSpec,
    target_sid: str,
    content: str,
) -> str:
    """Assemble a fully-formed JSONL record for the receiver's inbox.

    Delegates to :func:`modexctl.main._build_inbox_line` so the JSONL
    shape is owned by the production CLI. The returned string is a
    complete JSONL record (no trailing newline; the caller/writer adds it).
    """
    return _modexctl_build_inbox_line(
        session_id=env.session_id,
        agent_name=env.agent_name,
        target_sid=target_sid,
        content=content,
    )


__all__ = [
    "_compute_target_session_id",
    "_resolve_target_pool",
    "_build_inbox_line",
]
