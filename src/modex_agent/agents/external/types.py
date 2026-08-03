"""Frozen Pydantic models for the external coding agent integration.

Every cross-module data structure used by `ExternalAgent`,
`modexbot`, and the per-provider backends lives here. All models obey
type-safety rules 10–16 (frozen, ``extra="forbid"``, no bare
``dict[str, Any]``, ``Literal``/Enum over raw strings, nested structured
fields typed as BaseModel).

The single non-Pydantic access path in the integration is
``ExternalPaths`` (see ``paths.py``), which is a process-local path
accessor and intentionally not a value object.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.core.agent import AgentCommKind

from .events import ExternalEvent
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
    """Source values for the ``MODEX_*`` vars harness injects per spawn.

    The builder (``ExternalEnvBuilder.build``) takes an instance of this
    spec plus a base env and produces the dict passed to
    ``subprocess.Popen(env=...)``. This is the single convergence point
    for ``MODEX_*`` vars — no other site constructs them.

    ``agent_pool_map`` and ``targets`` are refreshed every turn from the
    `CommunicationTargetStore`; the rest are stable across the session
    lifetime.

    Two comm kinds drive two independent routing logics in ``modexctl``:

    - ``NORMAL`` (main-agent-as-peer): ``modexctl send`` derives
      ``target_sid = prefix + "." + target_name`` via ADR-0019
      prefix-reuse. ``parent_session_id`` is ``None``.
    - ``SUBAGENT``: ``modexctl send`` uses ``parent_session_id`` verbatim
      as ``target_sid``. Subagent session prefixes are invocation_ids,
      not conversation_ids, so prefix-reuse would mint a phantom parent
      session. ``parent_session_id`` is required.
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
    comm_kind: AgentCommKind = Field(
        default=AgentCommKind.NORMAL,
        description=(
            "Routing kind — drives which target_sid derivation modexctl uses. "
            "NORMAL: ADR-0019 prefix-reuse. SUBAGENT: parent_session_id verbatim."
        ),
    )
    parent_session_id: str | None = Field(
        default=None,
        description=(
            "Parent's full session_id. Required when comm_kind=SUBAGENT; "
            "ignored when comm_kind=NORMAL."
        ),
    )
    workflow_id: str | None = Field(
        default=None,
        description=(
            "Optional workflow context id surfaced as MODEX_WORKFLOW_ID. "
            "None omits the var entirely (external agent sees no key)."
        ),
    )
    task_id: str | None = Field(
        default=None,
        description=(
            "Workflow task id surfaced as MODEX_TASK_ID. "
            "In graph orchestration (ticket 05), task_id = str(graph_instance_id) "
            "— the bot factory sets this when creating a GraphInstance so the "
            "external agent's modexctl commands can route to the correct graph. "
            "None omits the var entirely (external agent sees no key)."
        ),
    )
    node_id: str | None = Field(
        default=None,
        description=(
            "Optional workflow node id surfaced as MODEX_NODE_ID. "
            "None omits the var entirely (external agent sees no key)."
        ),
    )
    control_origin: str = Field(
        default="",
        description=(
            "Bot HTTP listener origin (e.g. ``http://127.0.0.1:21800``) "
            "surfaced as MODEX_CONTROL_ORIGIN. ADR-0036 D6: the value is "
            "sourced from ``bot_config.yml``'s ``webui.host``/``webui.port`` "
            "at bot startup, with ``0.0.0.0`` normalized to ``127.0.0.1`` so "
            "the injected host is always loopback. Empty string when the "
            "spec is constructed outside ``examples/bot_project`` (framework "
            "tests, non-bot callers) — the var is still emitted, just empty."
        ),
    )


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

    event: ExternalEvent
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
    # Child session routing: None = main session, str = provider child session ID
    source_session_id: str | None = None


__all__ = [
    "ExecOptions",
    "BackendStatus",
    "BackendResult",
    "SessionMapEntry",
    "ExternalEnvSpec",
    "Emission",
]
