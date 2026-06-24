"""Memory scope abstractions.

Scope identity types moved to framework.core.scope. This module re-exports
them and retains memory-specific types (MemoryLayerName, ScopeRecord).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from modex_agent.core.scope import (
    AgentScope,
    ChannelScope,
    ChatScope,
    CompositeScope,
    GlobalScope,
    MemoryAgentRole,
    MemoryContext,
    MemoryScope,
    SessionScope,
    TenantScope,
    UserScope,
    infer_agent_role,
)


class MemoryLayerName(StrEnum):
    """Canonical memory layer names used in metadata and config."""

    SESSION = "session"
    ARCHIVE = "archive"
    KNOWLEDGE = "knowledge"
    PROVIDER = "provider"
    USER_RETENTION = "user_retention"


@dataclass
class ScopeRecord:
    """Recoverable metadata for a persisted memory scope."""

    scope_key: str
    layer: str | MemoryLayerName
    context: MemoryContext
    storage_path: str
    agent_role: str | MemoryAgentRole = MemoryAgentRole.MAIN
    agent_id: str | None = None
    created_at: float | None = None
    updated_at: float | None = None


__all__ = [
    "AgentScope",
    "ChannelScope",
    "ChatScope",
    "CompositeScope",
    "GlobalScope",
    "MemoryAgentRole",
    "MemoryContext",
    "MemoryLayerName",
    "MemoryScope",
    "ScopeRecord",
    "SessionScope",
    "TenantScope",
    "UserScope",
    "infer_agent_role",
]