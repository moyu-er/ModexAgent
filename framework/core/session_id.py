"""First-class SessionId object + factory.

`SessionId` is the single identity object across the framework. Its fields are
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
    """Shorten an arbitrary raw id (IM id, invocation_id, conversation id) into
    a compact, filesystem-safe base58 string.

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


class SessionId(BaseModel):
    """First-class session identifier.

    Required: ``session_id`` (complete display id ``snowflake.agentName``),
    ``agent_name``. All other fields default to ``None`` / empty.

    Frozen so it is hash-safe as a dict key / set member. ``__hash__`` derives
    from the immutable ``session_id`` string. Updates go through
    ``model_copy(update={...})`` (see ``touch()``).
    """

    model_config = ConfigDict(frozen=True)

    session_id: str = Field(..., description="Complete display id: snowflake.agentName")
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
    # a SessionId to a non-SessionId must return NotImplemented (not False) so
    # Python falls back to the other operand's __eq__. This is the standard
    # dataclass/pydantic equality idiom, not a runtime duck-typing check.
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SessionId):
            return NotImplemented
        return self.session_id == other.session_id

    @property
    def snowflake(self) -> str:
        """The snowflake part (segment before the first '.')."""
        return self.session_id.split(".", 1)[0] if "." in self.session_id else self.session_id

    @property
    def is_subagent(self) -> bool:
        """True when this session has a recorded parent."""
        return self.parent_session_id is not None

    # deprecated, don't use touch()
    def touch(self) -> SessionId:
        """Return a copy with ``updated_at`` refreshed to now."""
        return self.model_copy(update={"updated_at": now_ms()})

    @classmethod
    def from_str(
        cls,
        value: str,
        *,
        default_agent_name: str | None = None,
    ) -> SessionId:
        """Recover a SessionId from a display string (last-resort fallback).

        Emits a UserWarning when the value has no separator or an empty
        agent_name suffix. Callers should query the registry first.
        """
        if "." not in value:
            warnings.warn(
                f"SessionId {value!r} has no separator; treating as bare snowflake",
                UserWarning,
                stacklevel=2,
            )
            agent_name = default_agent_name or "unknown"
        else:
            _snowflake, _, suffix = value.rpartition(".")
            agent_name = suffix or default_agent_name or "unknown"
            if not suffix:
                warnings.warn(
                    f"SessionId {value!r} has empty agent_name suffix",
                    UserWarning,
                    stacklevel=2,
                )
        now = now_ms()
        return cls(session_id=value, agent_name=agent_name,
                   created_at=now, updated_at=now)


class SessionIdFactory:
    """Generates new SessionId instances.

    The snowflake is ``encode_snowflake(external_id or uuid4)``. ``external_id``
    is an IM-provided id or an existing invocation_id; it forms the snowflake
    part only, never the complete session id.
    """

    def __init__(self) -> None:
        pass

    def create(
        self,
        agent_name: str,
        *,
        parent_session_id: SessionId | str | None = None,
        external_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionId:
        raw = external_id if external_id is not None else self._generate_raw()
        encoded = encode_snowflake(raw)
        session_id = f"{encoded}.{agent_name}"
        now = now_ms()
        parent_str = str(parent_session_id) if parent_session_id else None
        return SessionId(
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
