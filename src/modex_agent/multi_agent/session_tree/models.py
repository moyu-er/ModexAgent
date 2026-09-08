"""Session-tree data models: tree / node / message-track records.

Three frozen Pydantic value objects plus their status enums backing the
session-tree persistence layer (ADR-pending). These are pure records — no
behavior, no stores, no managers. Stores consume them via ``model_dump()`` /
``model_validate()`` per rule 13.

Timestamps are epoch-ms integers (ADR-0029).

Invariant: track_id == message_id (simplifies lookups, idempotent delivery).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.multi_agent.message_type import AgentMessageType


class SessionTreeStatus(StrEnum):
    """Lifecycle state of a session tree."""

    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class NodeVersionStatus(StrEnum):
    """Lifecycle state of a single node version within a tree."""

    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class SessionTreeMetadata(StrEnum):
    """SessionRegistry metadata owned by the session-tree lifecycle."""

    BINDING = "session_tree_binding"
    PAUSED = "session_tree_paused"


class MessageTrackStatus(StrEnum):
    """Delivery state of a tracked message between tree nodes."""

    DISPATCHED = "dispatched"
    CONSUMED = "consumed"
    CANCELLED = "cancelled"


class SessionTreeRecord(BaseModel):
    """A session tree: the root identity + pool/workspace it belongs to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tree_id: str
    root_node_session_id: str
    pool_name: str
    workspace_root: str
    status: SessionTreeStatus
    created_at: int = Field(description="Epoch-ms (ADR-0029).")
    updated_at: int = Field(description="Epoch-ms (ADR-0029).")
    completed_at: int | None = Field(
        default=None,
        description="Epoch-ms (ADR-0029). Set when the tree reaches a terminal status.",
    )


class TreeNodeRecord(BaseModel):
    """A node within a session tree: one agent session at a given version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tree_id: str
    session_id: str
    parent_session_id: str | None = Field(
        description="None for the root node; the parent session id otherwise.",
    )
    agent_name: str
    version: int = Field(description="Monotonic version number for this node.")
    parent_version: int | None = Field(
        description="Version of the parent node this version was forked from.",
    )
    status: NodeVersionStatus
    created_at: int = Field(description="Epoch-ms (ADR-0029).")
    updated_at: int = Field(description="Epoch-ms (ADR-0029).")


class MessageTrack(BaseModel):
    """A message dispatched between two tree nodes, tracked for delivery.

    Invariant: track_id == message_id (simplifies lookups, idempotent delivery).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    track_id: str = Field(
        description="Track identifier. Equal to message_id (see invariant).",
    )
    tree_id: str
    message_id: str = Field(
        description="Underlying message id. Equal to track_id (see invariant).",
    )
    message_type: AgentMessageType = Field(
        description="The AgentMessageType of the tracked message.",
    )
    invocation_id: str | None = Field(
        default=None,
        description="Source subagent snowflake, for trace correlation only.",
    )
    target_session_id: str
    source_session_id: str
    status: MessageTrackStatus
    dispatched_at: int = Field(description="Epoch-ms (ADR-0029).")
    consumed_at: int | None = Field(
        default=None,
        description="Epoch-ms (ADR-0029). Set when the target consumes the message.",
    )
