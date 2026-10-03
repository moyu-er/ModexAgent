"""External coding agent integration — public API.

This sub-package admits industry-standard coding-agent CLIs
(OpenCode, future Claude Code / Codex / Cursor) as NORMAL main agents of
their own dedicated pools. The layout (W4 of the unified turn-event
stream refactor):

- ``transports/`` — the access-form seam. ``ExternalTransport.execute``
  drives the external agent for one turn and delivers core ``TurnEvent``
  records; ``OpenCodeTransport`` (CLI subprocess) is the real transport,
  ``ScriptedTransport`` the deterministic test double.
- ``normalizer.py`` — ``ExternalEventNormalizer`` owns every
  cross-transport concern (child-session routing, ``seq`` stamping,
  orphan tool-result policy, turn lifecycle synthesis).

ADR-0027 (T2) introduced the :class:`BackendProvider` borrowing seam:
:class:`ExternalAgent` borrows a transport per turn rather than holding
a fixed instance. The main-agent path wraps its pre-built transport in
:class:`PoolScopedBackendProvider`.
"""

from modex_agent.core.external_session import ExternalSessionMapStore

from .backend_provider import (
    BackendProvider,
    PoolScopedBackendProvider,
    TurnContext,
)
from .env_builder import ExternalEnvBuilder, write_env_snapshot_for_session
from .normalizer import (
    ExternalEventNormalizer,
    error_of_backend_result,
    stop_reason_of,
)
from .paths import ExternalPaths
from .session_store import LocalFileExternalSessionMapStore
from .transports import (
    ChildTurnEventCallbackFactory,
    ExternalTransport,
    ScriptedProgramme,
    ScriptedStep,
    ScriptedTransport,
    SendSideEffect,
    StaleSessionError,
    TurnEventCallback,
)
from .types import (
    BackendResult,
    BackendStatus,
    ExecOptions,
    ExternalEnvSpec,
    SessionMapEntry,
)

__all__ = [
    # Transport seam (access forms). The real CLI transport
    # (OpenCodeTransport, transports.cli_transport) is NOT re-exported:
    # its provider stack needs aiohttp, which base installs (the
    # conformance environment) do not carry — import it from its module.
    "ExternalTransport",
    "ScriptedTransport",
    "ScriptedProgramme",
    "ScriptedStep",
    "SendSideEffect",
    "StaleSessionError",
    "TurnEventCallback",
    "ChildTurnEventCallbackFactory",
    # Shared normalizer
    "ExternalEventNormalizer",
    "stop_reason_of",
    "error_of_backend_result",
    # Enums / status
    "BackendStatus",
    # Path accessor
    "ExternalPaths",
    # Env builder + spec
    "ExternalEnvBuilder",
    "write_env_snapshot_for_session",
    "ExternalEnvSpec",
    # Execution contracts
    "ExecOptions",
    "BackendResult",
    # Backend provider seam (ADR-0027)
    "BackendProvider",
    "PoolScopedBackendProvider",
    "TurnContext",
    # Session persistence
    "ExternalSessionMapStore",
    "LocalFileExternalSessionMapStore",
    "SessionMapEntry",
]
