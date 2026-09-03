"""First-class SessionInfo object + factory.

`SessionInfo` is the single identity object across the framework. Its fields are
authoritative; the string is opaque and never parsed except via the
last-resort `from_str` fallback.
"""

from __future__ import annotations

import hashlib
import logging
import warnings
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.utils.time import now_ms as _now_ms

logger = logging.getLogger(__name__)

# base58 alphabet (Bitcoin), stdlib-only implementation.
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


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
        return self.model_copy(update={"updated_at": _now_ms()})

    @classmethod
    def from_str(cls, value: str) -> SessionInfo:
        """Recover a SessionInfo from a display string (last-resort fallback).

        Emits a UserWarning when the value has no separator or an empty
        agent_name suffix. A bare prefix (no separator) produces
        ``agent_name=""`` — the session genuinely has no bound agent, and
        callers (PoolRouter's ownership lookup, etc.) must treat empty agent_name
        as "trust the routing store" rather than inventing a fake default.
        Callers that know the agent should construct SessionInfo directly.
        """
        if "." not in value:
            warnings.warn(
                f"SessionInfo {value!r} has no separator; treating as a bare prefix",
                UserWarning,
                stacklevel=2,
            )
            agent_name = ""
        else:
            _prefix, _, suffix = value.rpartition(".")
            agent_name = suffix
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

    ``create()`` always encodes ``external_id`` via ``encode_snowflake``.
    Use ``create_with_prefix()`` when you already have a verbatim prefix
    (e.g. an invocation_id) that must not contain ``"."``.
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
    ) -> SessionInfo:
        """Create a SessionInfo with *external_id* encoded into the prefix.

        ``external_id`` is a raw conversation / invocation identifier (IM
        user_id, uuid_prefix, etc.) and is ALWAYS run through
        ``encode_snowflake`` to produce the session id prefix.
        """
        raw = external_id if external_id is not None else self._generate_raw()
        encoded = encode_snowflake(raw)
        return self._build(agent_name, encoded, parent_session_id, metadata)

    def create_with_prefix(
        self,
        agent_name: str,
        prefix: str,
        *,
        parent_session_id: SessionInfo | str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionInfo:
        """Create a SessionInfo using *prefix* verbatim — no encoding.

        *prefix* must be a clean segment without ``"."`` (e.g. an invocation_id
        hex string).  Raises ``ValueError`` if it contains a separator.
        """
        if "." in prefix:
            # A "." would make the resulting session_id ambiguous when
            # parsed via rpartition(".") — the agent_name segment would
            # drift.  Callers with a full session_id MUST use
            # SessionInfo.from_str() instead.
            raise ValueError(
                f"Prefix must not contain '.': {prefix!r}. "
                f"If you have a full session_id, use SessionInfo.from_str(). "
                f"If you have a raw external_id, use create(external_id=...)."
            )
        return self._build(agent_name, prefix, parent_session_id, metadata)

    def _build(
        self,
        agent_name: str,
        prefix: str,
        parent_session_id: SessionInfo | str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionInfo:
        session_id = f"{prefix}.{agent_name}"
        now = _now_ms()
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
