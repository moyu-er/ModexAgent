"""Memory ownership and configurable isolation scopes."""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from modex_agent.core.scope import RecordScope


class MemoryAgentRole(StrEnum):
    """Agent role for memory ownership and background processing."""

    MAIN = "main"
    SUBAGENT = "subagent"


class MemoryLayerName(StrEnum):
    """Canonical memory layer names used in metadata and config.

    ``CORE`` names the Core Memory layer (per ADR-0035; formerly "knowledge").
    The string value ``"core"`` is used as a dict key, a scope segment, and a
    filesystem path segment (``<root>/core/<scope_key>/`` on disk). For
    historical reasons the on-disk directory is ``core/`` rather than
    ``core_memory/``; they refer to the same concept.
    """

    SESSION = "session"
    ARCHIVE = "archive"
    CORE = "core"
    PROVIDER = "provider"


class MemoryContext(BaseModel):
    """Unified context containing every supported memory grouping field."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str | None = None
    user_id: str | None = None
    tenant_id: str | None = None
    agent_id: str | None = None
    agent_role: str | MemoryAgentRole | None = None
    channel: str | None = None
    chat_id: str | None = None
    sender_agent: str | None = None
    receiver_agent: str | None = None

    def with_defaults(self, **defaults: Any) -> MemoryContext:
        """Return a new MemoryContext with default values for missing fields."""
        current = {key: getattr(self, key) for key in type(self).model_fields}
        for key, default_value in defaults.items():
            if key in current and current[key] is None and default_value is not None:
                current[key] = default_value
        return type(self)(**current)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> MemoryContext:
        """Restore context from persisted scope metadata."""
        if not data:
            return cls()
        kwargs = {key: data.get(key) for key in cls.model_fields}
        return cls(**kwargs)


class ScopeRecord(BaseModel):
    """Recoverable metadata for a persisted memory scope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope_key: str
    layer: str | MemoryLayerName
    context: MemoryContext
    storage_path: str
    agent_role: str | MemoryAgentRole = MemoryAgentRole.MAIN
    agent_id: str | None = None
    created_at: float | None = None
    updated_at: float | None = None


def infer_agent_role(context: MemoryContext) -> MemoryAgentRole:
    """Infer role for persisted scope metadata."""
    candidates = [
        context.agent_role,
        context.agent_id,
        context.sender_agent,
        context.receiver_agent,
    ]
    normalized = {str(value).lower() for value in candidates if value}
    if MemoryAgentRole.SUBAGENT.value in normalized:
        return MemoryAgentRole.SUBAGENT
    return MemoryAgentRole.MAIN


class Scope(ABC):
    """Structured memory-scope extraction contract."""

    @abstractmethod
    def extract(self, context: MemoryContext) -> RecordScope:
        """Extract a RecordScope from context."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Short dimension name, used for debugging and metadata."""
        ...

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}()"


class SessionScope(Scope):
    """Group memory by session."""

    @property
    def name(self) -> str:
        return "session"

    def extract(self, context: MemoryContext) -> RecordScope:
        return RecordScope(session_id=context.session_id)


class UserScope(Scope):
    """Group memory by user."""

    @property
    def name(self) -> str:
        return "user"

    def extract(self, context: MemoryContext) -> RecordScope:
        return RecordScope(user_id=context.user_id)


class TenantScope(Scope):
    """Group memory by tenant."""

    @property
    def name(self) -> str:
        return "tenant"

    def extract(self, context: MemoryContext) -> RecordScope:
        return RecordScope(tenant_id=context.tenant_id)


class AgentScope(Scope):
    """Group memory by agent identity and role."""

    @property
    def name(self) -> str:
        return "agent:agent_role"

    def extract(self, context: MemoryContext) -> RecordScope:
        return RecordScope(agent_id=context.agent_id, agent_role=context.agent_role)


class ChannelScope(Scope):
    """Group memory by channel."""

    @property
    def name(self) -> str:
        return "channel"

    def extract(self, context: MemoryContext) -> RecordScope:
        return RecordScope(channel=context.channel)


class ChatScope(Scope):
    """Group memory by chat."""

    @property
    def name(self) -> str:
        return "chat"

    def extract(self, context: MemoryContext) -> RecordScope:
        return RecordScope(chat_id=context.chat_id)


class GlobalScope(Scope):
    """Share memory globally without a user-level path segment."""

    @property
    def name(self) -> str:
        return "global"

    def extract(self, context: MemoryContext) -> RecordScope:
        _ = context
        return RecordScope()


class CompositeScope(Scope):
    """Combine multiple scopes into one record and path key."""

    def __init__(self, *scopes: Scope) -> None:
        self.scopes = scopes

    def extract(self, context: MemoryContext) -> RecordScope:
        record = RecordScope()
        for scope in self.scopes:
            record = record.merge(scope.extract(context))
        return record

    @property
    def name(self) -> str:
        return ":".join(s.name for s in self.scopes)

    def __repr__(self) -> str:
        return f"CompositeScope({', '.join(s.name for s in self.scopes)})"


_DIMENSION_SCOPES: dict[str, type[Scope]] = {
    "session": SessionScope,
    "user": UserScope,
    "tenant": TenantScope,
    "agent": AgentScope,
    "channel": ChannelScope,
    "chat": ChatScope,
    "global": GlobalScope,
}

_PATH_DIMENSIONS = frozenset(
    {
        "pool",
        "workspace",
        "session",
        "session_prefix",
        "agent",
        "agent_role",
        "user",
        "tenant",
        "channel",
        "chat",
        "invocation",
        "parent_session",
    }
)


def build_scope(dims: list[str] | str) -> Scope:
    """Build a Scope from dimension short names."""
    if isinstance(dims, str):
        dims = [dims]
    if len(dims) == 0:
        return GlobalScope()

    resolved: list[Scope] = []
    for dim in dims:
        cls = _DIMENSION_SCOPES.get(dim)
        if cls is None:
            raise ValueError(f"Unknown scope dimension: {dim!r}")
        resolved.append(cls())

    if len(resolved) == 1:
        return resolved[0]
    return CompositeScope(*resolved)


def scope_path_key(scope: Scope, context: MemoryContext) -> str:
    """Return the filesystem path segment for a scope and context."""
    record = scope.extract(context)
    dimensions = [
        dimension for dimension in scope.name.split(":") if dimension in _PATH_DIMENSIONS
    ]
    return record.to_path_segment(*dimensions)


__all__ = [
    "AgentScope",
    "ChannelScope",
    "ChatScope",
    "CompositeScope",
    "GlobalScope",
    "MemoryAgentRole",
    "MemoryContext",
    "MemoryLayerName",
    "Scope",
    "ScopeRecord",
    "SessionScope",
    "TenantScope",
    "UserScope",
    "build_scope",
    "infer_agent_role",
    "scope_path_key",
]
