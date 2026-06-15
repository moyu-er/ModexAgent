"""First-class SessionInfo object + factory.

`SessionInfo` is the single identity object across the framework. Its fields are
authoritative; the string is opaque and never parsed except via the
last-resort `from_str` fallback.
"""

from __future__ import annotations

import hashlib
import logging
import time
import warnings
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# base58 alphabet (Bitcoin), stdlib-only implementation.
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def now_ms() -> int:
    """Current Unix time in milliseconds."""
    return int(time.time() * 1000)


def encode_snowflake(raw: str) -> str:
    """Shorten an arbitrary raw id (external id, invocation_id) into
    a compact, filesystem-safe base58 string — the session id prefix.

    Deterministic: same input always yields the same output. Length is ~16 chars
    for a 12-byte digest, well within filesystem path limits.
    """
    digest = hashlib.sha256(raw.encode("utf-8")).digest()[:12]
    num = int.from_bytes(digest, "big")
    if num == 0:
        return _BASE58_ALPHABET[0]
    out: list[str] = []
    while num > 0:
        num, rem = divmod(num, 58)
        out.append(_BASE58_ALPHABET[rem])
    # preserve leading zeros (sha256 digest won't start with zero bytes often,
    # but handle for correctness)
    return "".join(reversed(out))


class SessionInfo(BaseModel):
    """First-class session identifier.

    Required: ``session_id`` (complete display id ``{prefix}.{agentName}``),
    ``agent_name``. All other fields default to ``None`` / empty.

    Frozen so it is hash-safe as a dict key / set member. ``__hash__`` derives
    from the immutable ``session_id`` string. Updates go through
    ``model_copy(update={...})`` (see ``touch()``).
    """

    model_config = ConfigDict(frozen=True)

    session_id: str = Field(..., description="Complete display id: {session_id_prefix}.{agentName}")
    agent_name: str = Field(..., description="Bound agent name")
    parent_session_id: str | None = None
    created_at: int | None = Field(default=None, description="ms Unix epoch")
    updated_at: int | None = Field(default=None, description="ms Unix epoch")
    metadata: dict[str, Any] = Field(default_factory=dict)

    def __str__(self) -> str:
        return self.session_id

    def __hash__(self) -> int:
        return hash(self.session_id)

    # NOTE: isinstance here is required for value-equality semantics — comparing
    # a SessionInfo to a non-SessionInfo must return NotImplemented (not False) so
    # Python falls back to the other operand's __eq__. This is the standard
    # dataclass/pydantic equality idiom, not a runtime duck-typing check.
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SessionInfo):
            return NotImplemented
        return self.session_id == other.session_id

    @property
    def session_id_prefix(self) -> str:
        """The segment before the first '.' — the agent-independent prefix."""
        return self.session_id.split(".", 1)[0] if "." in self.session_id else self.session_id

    @property
    def is_subagent(self) -> bool:
        """True when this session has a recorded parent."""
        return self.parent_session_id is not None

    # deprecated, don't use touch()
    def touch(self) -> SessionInfo:
        """Return a copy with ``updated_at`` refreshed to now."""
        return self.model_copy(update={"updated_at": now_ms()})

    @classmethod
    def from_str(
        cls,
        value: str,
        *,
        default_agent_name: str | None = None,
    ) -> SessionInfo:
        """Recover a SessionInfo from a display string (last-resort fallback).

        Emits a UserWarning when the value has no separator or an empty
        agent_name suffix. Callers should query the registry first.
        """
        if "." not in value:
            warnings.warn(
                f"SessionInfo {value!r} has no separator; treating as a bare prefix",
                UserWarning,
                stacklevel=2,
            )
            agent_name = default_agent_name or "unknown"
        else:
            _prefix, _, suffix = value.rpartition(".")
            agent_name = suffix or default_agent_name or "unknown"
            if not suffix:
                warnings.warn(
                    f"SessionInfo {value!r} has empty agent_name suffix",
                    UserWarning,
                    stacklevel=2,
                )
        return cls(session_id=value, agent_name=agent_name)


def session_id_prefix_of(session_id: str) -> str:
    """Extract the prefix segment (before the first ``.``) from a display id.

    Single source of truth for prefix extraction from a string. Use this
    instead of ad-hoc ``session_id.split('.', 1)[0]``.
    """
    return session_id.split(".", 1)[0] if "." in session_id else session_id


def agent_of(session_id: str, *, default: str = "unknown") -> str:
    """Extract the agent_name segment (2nd ``.``-separated component) of a display id.

    For the canonical ``"{prefix}.{agent_name}"`` format returns ``agent_name``;
    for legacy ``"{conv}.{agent}.{invocation_id}"`` returns the agent (middle
    segment); for a bare prefix returns ``default``.
    """
    parts = session_id.split(".", 2)
    return parts[1] if len(parts) >= 2 else default


class SessionIdFactory:
    """Generates new SessionInfo instances.

    The prefix is ``encode_snowflake(external_id or uuid4)``. ``external_id``
    is an IM-provided id or an existing invocation_id; it forms the prefix
    part only, never the complete session id.
    """

    def __init__(self) -> None:
        pass

    def create(
        self,
        agent_name: str,
        *,
        parent_session_id: SessionInfo | str | None = None,
        external_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        encode_external_id: bool = True,
    ) -> SessionInfo:
        raw = external_id if external_id is not None else self._generate_raw()
        if encode_external_id:
            encoded = encode_snowflake(raw)
        elif "." in raw:
            # Raw prefixes must not contain the session separator; fall back
            # to a deterministic safe encoding to keep parsing unambiguous.
            encoded = encode_snowflake(raw)
        else:
            encoded = raw
        session_id = f"{encoded}.{agent_name}"
        now = now_ms()
        parent_str = str(parent_session_id) if parent_session_id else None
        return SessionInfo(
            session_id=session_id,
            agent_name=agent_name,
            parent_session_id=parent_str,
            created_at=now,
            updated_at=now,
            metadata=metadata or {},
        )

    def _generate_raw(self) -> str:
        import uuid

        return uuid.uuid4().hex
