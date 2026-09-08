"""External coding agent integration — public API for T1 (foundation types).

This sub-package admits industry-standard coding-agent CLIs (Pi,
OpenCode, future Claude Code / Codex / Cursor) as NORMAL main agents of
their own dedicated pools. T1 ships the pure-Pydantic type layer
every subsequent ticket depends on; richer pieces (session store,
provider backends, the `ExternalAgent` harness, ``modexbot``
CLI) land in T2–T8.

Per ADR-0022, the framework footprint outside this sub-package stays
at two lines (factory branch) plus one comment (descriptor). All
heavy lifting lives here.

ADR-0027 (T2) introduces the :class:`BackendProvider` borrowing seam:
:class:`ExternalAgent` borrows a backend per turn rather than
holding a fixed instance. The main-agent path wraps its pre-built
backend in :class:`PoolScopedBackendProvider`.
"""

from .backend_provider import (
    BackendProvider,
    PoolScopedBackendProvider,
    TurnContext,
)
from .contracts import ProviderBackend, ProviderEventParser
from .env_builder import ExternalEnvBuilder
from .events import ExternalEvent
from .paths import ExternalPaths
from .session_store import ExternalSessionMapStore, LocalFileExternalSessionMapStore
from .types import (
    BackendResult,
    BackendStatus,
    Emission,
    ExecOptions,
    ExternalEnvSpec,
    SessionMapEntry,
)

__all__ = [
    # Enums
    "ExternalEvent",
    "BackendStatus",
    # Path accessor
    "ExternalPaths",
    # Env builder + spec
    "ExternalEnvBuilder",
    "ExternalEnvSpec",
    # Backend contracts
    "ExecOptions",
    "BackendResult",
    "ProviderBackend",
    "ProviderEventParser",
    # Backend provider seam (ADR-0027)
    "BackendProvider",
    "PoolScopedBackendProvider",
    "TurnContext",
    # Session persistence
    "ExternalSessionMapStore",
    "LocalFileExternalSessionMapStore",
    "SessionMapEntry",
    # Per-line emission
    "Emission",
]
