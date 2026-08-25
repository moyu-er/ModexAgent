"""Pydantic models for the bot control API (T04).

These models are the wire contract between the ``modexctl`` CLI (T04), the
``POST /api/control/history`` route, and ``BotControlFacade.history()``.

All models are frozen (``extra="forbid"``) per ``rules/type-safety.md`` rule 12.
``HistoryRequest.limit`` is constrained to ``ge=1, le=10`` at the bot boundary
so Pydantic rejects out-of-range values before the facade runs.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AgentSessionRef(BaseModel):
    """Caller identity: workspace + pool + session + agent_name.

    ``workspace`` is the workspace root path (absolute or relative-to-home).
    ``pool`` is the pool name owning the session. ``session_id`` is the full
    ``{prefix}.{agent_name}`` display id. ``agent_name`` is the bound agent.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace: Path
    pool: str
    session_id: str
    agent_name: str


class HistoryRequest(BaseModel):
    """Request payload for ``POST /api/control/history``.

    ``limit`` defaults to 3 and is clamped to ``[1, 10]`` by Pydantic.
    The CLI performs its own clamping before sending, but the server
    re-validates so a malformed direct request cannot bypass the bound.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    caller: AgentSessionRef
    limit: int = Field(default=3, ge=1, le=10)


class HistorySource(StrEnum):
    """Where the history items were read from.

    ``MESSAGE_STORE`` — native ReAct agent session messages (SQLite or file).
    ``OBSERVABLE_TRANSCRIPT`` — external coding agent transcript (T05, deferred).
    """

    MESSAGE_STORE = "message_store"
    OBSERVABLE_TRANSCRIPT = "observable_transcript"


class HistoryMessage(BaseModel):
    """Eight-field Server Projection of one session message.

    Internal markers (``_deleted``, ``_pinned``, ``token_count``,
    ``is_content_json``) are stripped before construction. ``content`` is
    preserved verbatim — ``str`` stays ``str``, ``list`` (multimodal
    ContentPart) stays ``list``. ``None`` fields are excluded at serialization
    time (``model_dump(exclude_none=True)``) so the wire shape is compact.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str
    content: str | list[dict[str, Any]] | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    name: str | None = None
    created_at: str | None = None
    message_id: str | None = None


class HistoryResult(BaseModel):
    """Response payload for ``POST /api/control/history``.

    ``effective_limit`` echoes the validated limit so the CLI can detect
    server-side clamping (defence-in-depth). ``execution_strategy`` is the
    pool's main-agent strategy string (``react`` / ``external`` / ...).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: HistorySource
    session_id: str
    agent_name: str
    pool: str
    execution_strategy: str
    items: list[HistoryMessage]
    effective_limit: int


class ControlError(BaseModel):
    """Structured error body returned for 4xx/5xx control route responses.

    ``code`` is a stable machine-readable string (e.g. ``session_not_found``).
    ``message`` is a human-readable diagnostic.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    message: str


# ---------------------------------------------------------------------------
# Send (T06)
# ---------------------------------------------------------------------------


class DispatchOutcome(StrEnum):
    """Outcome of a send dispatch.

    - ``NEW_TASK`` — no invocation id was requested; a fresh subagent task
      session was created.
    - ``NOT_APPLICABLE`` — peer send or parent reply (no task creation
      semantics).
    - ``RESUMED`` — T07: an existing invocation id was resumed.
    - ``REQUESTED_INVOCATION_NOT_FOUND`` — T07: the requested invocation id
      did not match an existing session; a fresh task was created instead.
    """

    NEW_TASK = "new_task"
    RESUMED = "resumed"
    REQUESTED_INVOCATION_NOT_FOUND = "requested_invocation_not_found"
    NOT_APPLICABLE = "not_applicable"


class SendRequest(BaseModel):
    """Request payload for ``POST /api/control/send``.

    ``comm_kind`` is the caller's topology kind: ``"normal"`` for a main
    agent (peer send or subagent dispatch), ``"subagent"`` for a subagent
    replying to its parent. ``parent_session_id`` is required when
    ``comm_kind == "subagent"`` so the topology gate can verify the target
    is the caller's parent. ``invocation_id`` is ``None`` in T06 (T07 adds
    the continuation path). ``graph_instance_id`` optionally binds the
    dispatched turn to a running graph instance so the agent receives it
    in :class:`AgentContext.graph_instance_id`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    caller: AgentSessionRef
    comm_kind: str
    parent_session_id: str | None = None
    target_agent: str
    content: str
    invocation_id: str | None = None
    graph_instance_id: int | None = None


class SendResult(BaseModel):
    """Response payload for ``POST /api/control/send``.

    ``target_kind`` is the :class:`AgentCommKind` value (``"normal"`` or
    ``"subagent"``). ``dispatch_outcome`` tells the CLI which ack template
    to emit. ``is_peer_send`` and ``is_external_target`` drive the CLI's
    format selection. ``trace_dir`` is sourced verbatim from
    :class:`AgentSendResult` (never re-derived from
    the scope-path resolution). ``requested_invocation_id`` is
    populated only in T07.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_agent: str
    target_kind: str
    session_id: str
    invocation_id: str | None = None
    dispatch_outcome: DispatchOutcome
    requested_invocation_id: str | None = None
    is_peer_send: bool = False
    is_external_target: bool = False
    trace_dir: Path | None = None
