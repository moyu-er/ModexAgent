"""External coding agent integration — public API for T1 (foundation types).

This sub-package admits industry-standard coding-agent CLIs (Pi,
OpenCode, future Claude Code / Codex / Cursor) as NORMAL main agents of
their own dedicated pools. T1 ships the pure-Pydantic type layer
every subsequent ticket depends on; richer pieces (session store,
provider backends, the `ExternalCodingAgent` harness, ``modexbot``
CLI) land in T2–T8.

Per ADR-0022, the framework footprint outside this sub-package stays
at two lines (factory branch) plus one comment (descriptor). All
heavy lifting lives here.
"""

from .contracts import ProviderBackend, ProviderEventParser
from .env_builder import ExternalEnvBuilder
from .events import ExternalCodingEvent
from .paths import ExternalPaths, ProviderKind
from .session_store import ExternalSessionMapStore, LocalFileExternalSessionMapStore
from .types import (
    BackendResult,
    BackendStatus,
    Emission,
    ExecOptions,
    ExternalEnvSpec,
    OutboxLine,
    OutboxMetadata,
    SessionMapEntry,
)

__all__ = [
    # Enums / provider discriminator
    "ExternalCodingEvent",
    "ProviderKind",
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
    # Session persistence
    "ExternalSessionMapStore",
    "LocalFileExternalSessionMapStore",
    "SessionMapEntry",
    # Outbox (inbox-line shape)
    "OutboxMetadata",
    "OutboxLine",
    # Per-line emission
    "Emission",
]
