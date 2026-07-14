"""Frozen Pydantic models for the external coding agent integration.

Every cross-module data structure used by `ExternalCodingAgent`,
`modexbot`, and the per-provider backends lives here. All models obey
type-safety rules 10–16 (frozen, ``extra="forbid"``, no bare
``dict[str, Any]``, ``Literal``/Enum over raw strings, nested structured
fields typed as BaseModel).

The single non-Pydantic access path in the integration is
``ExternalPaths`` (see ``paths.py``), which is a process-local path
accessor and intentionally not a value object.

The byte-exact ``OutboxLine`` shape matches what
``LocalFileInboxServer.receive()`` writes to ``pending.jsonl`` so
``modexctl send`` can be a second writer into the same on-disk format.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from .events import ExternalCodingEvent
from .paths import ProviderKind

# ---------------------------------------------------------------------------
# Exec options / backend result
# ---------------------------------------------------------------------------


class ExecOptions(BaseModel):
    """Per-spawn execution options passed to a `ProviderBackend.execute()`.

    The backend is otherwise stateless; session continuity is the caller's
    responsibility through ``resume_session_id`` and the
    ``BackendResult.session_id`` echoed back.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str
    workdir: Path
    resume_session_id: str | None = None
    system_prompt: str | None = None
    model: str | None = None
    thinking_level: str | None = None
    timeout: float | None = Field(default=None, ge=0)


class BackendStatus(StrEnum):
    """Terminal status of a provider-backend execution.

    Mirrors the closed set historically spelled as a ``Literal`` on
    :class:`BackendResult` and :class:`ScriptedProgramme`.
    """

    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    ABORTED = "aborted"


class BackendResult(BaseModel):
    """A single backend execution result.

    ``status`` is the closed set ``completed`` | ``failed`` | ``timeout`` |
    ``aborted``. ``session_id`` is provider-minted (or empty on early
    failure). ``error`` carries the stderr tail on non-zero exit.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: BackendStatus
    session_id: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Session map
# ---------------------------------------------------------------------------


class SessionMapEntry(BaseModel):
    """One entry in ``<workdir>/.modex/external/session-map.json``.

    Persisted as the JSON value of one key (``modex_session_id``). The
    map is a flat dict of ``modex_session_id`` → ``SessionMapEntry``;
    the file is rebuilt atomically on commit so a torn-write leaves the
    previous version intact.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    modex_session_id: str
    provider_session_id: str
    provider_kind: ProviderKind = Field(
        description="Provider kind discriminator (PI, OPENCODE, ...)."
    )
    last_committed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    invalidated: bool = False


# ---------------------------------------------------------------------------
# Env spec (the 9 MODEX_* source values)
# ---------------------------------------------------------------------------


class ExternalEnvSpec(BaseModel):
    """Source values for the 9 ``MODEX_*`` vars harness injects per spawn.

    The builder (``ExternalEnvBuilder.build``) takes an instance of this
    spec plus a base env and produces the dict passed to
    ``subprocess.Popen(env=...)``. This is the single convergence point
    for ``MODEX_*`` vars — no other site constructs them.

    ``agent_pool_map`` and ``targets`` are refreshed every turn from the
    `CommunicationTargetStore`; the rest are stable across the session
    lifetime.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_root: Path
    inbox_root: Path
    workdir: Path
    session_id: str
    agent_name: str
    provider_session_id: str
    agent_pool_map: dict[str, str] = Field(
        description="Map of agent_name → pool_name (the MODEX_AGENT_POOL_MAP source)."
    )
    targets: list[tuple[str, str]] = Field(
        description=(
            "Order-preserving list of (name, description) pairs serialised "
            "as MODEX_TARGETS. Description is opaque text the provider "
            "may surface to its LLM."
        )
    )
    modexctl_bin_dir: Path = Field(
        description="Directory prepended to PATH so bash tools find ``modexctl``."
    )


# ---------------------------------------------------------------------------
# Outbox line — byte-identical to LocalFileInboxServer.receive() output
# ---------------------------------------------------------------------------


class OutboxMetadata(BaseModel):
    """The ``metadata`` field of one ``OutboxLine``.

    Carries the bookkeeping ``LocalFileInboxServer._session_id_from_text``
    relies on to recover the original ``session_id`` during
    ``sessions_with_pending`` scans (namely ``agent_session_id``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_session_id: str = Field(
        description="Original (un-encoded) session_id used by _session_id_from_text."
    )
    session_id: str = Field(description="Sender session_id (the modexbot caller).")
    invocation_id: str | None = None
    parent_session_id: str | None = None


class OutboxLine(BaseModel):
    """A single JSONL line written to ``pending.jsonl`` by ``modexctl send``.

    Field order and content match ``LocalFileInboxServer.receive()``'s
    serialiser exactly: ``message_id``, ``source``, ``content``,
    ``message_type``, ``timestamp`` (ISO-8601 string), ``metadata``.
    ``model_dump_json()`` is therefore byte-identical to what
    ``receive()`` writes, modulo the values themselves.

    The ``timestamp`` field serialises through ``datetime.isoformat()``
    (matching the existing inbox serialiser) rather than Pydantic's
    default ``Z``-format so UTC offsets render as ``+00:00``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    message_id: str
    source: str
    content: str
    message_type: str
    timestamp: datetime
    metadata: OutboxMetadata

    @field_serializer("timestamp")
    def _serialize_timestamp(self, value: datetime) -> str:
        # Match ``LocalFileInboxServer.receive()``'s
        # ``message.timestamp.isoformat()`` byte-for-byte so consumers
        # can re-parse with ``datetime.fromisoformat()`` unchanged.
        return value.isoformat()


# ---------------------------------------------------------------------------
# Emission (per line) — discriminated union by event
# ---------------------------------------------------------------------------


class Emission(BaseModel):
    """A single emission parsed from one stdout JSONL line.

    Field set is the union of every event-kind-specific payload; only
    those relevant to the concrete event are populated. ``event`` is the
    discriminator consumers switch on. Day-one shapes:

    - ``TEXT_DELTA``  → ``text``
    - ``THINKING``    → ``text``
    - ``TOOL_USE``    → ``tool_name`` + ``tool_input``
    - ``TOOL_RESULT`` → ``call_id`` + ``output``
    - ``ERROR``       → ``message``

    Parsers (`ProviderEventParser`) return zero or more ``Emission``
    records per line so a single line carrying multiple updates (e.g. Pi
    ``message_update`` with both thinking + text) fans out cleanly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event: ExternalCodingEvent
    part_id: str | None = None
    # TEXT_DELTA / THINKING
    text: str | None = None
    # TOOL_USE
    tool_name: str | None = None
    tool_input: str | None = None
    # TOOL_RESULT
    call_id: str | None = None
    output: str | None = None
    # ERROR
    message: str | None = None


__all__ = [
    "ExecOptions",
    "BackendStatus",
    "BackendResult",
    "SessionMapEntry",
    "ExternalEnvSpec",
    "OutboxMetadata",
    "OutboxLine",
    "Emission",
]
