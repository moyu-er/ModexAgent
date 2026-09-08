"""Session artifact cleanup contracts and implementations (ADR-0018, plan §12).

Framework persistence owns artifact enumeration and idempotent deletion
(``DefaultSessionArtifactCleaner``, ``SqliteSessionDatabaseCleaner``, file
scope/pool discovery). Cascade traversal, retry authority, liveness gating,
and pool-route reclamation stay in the bot layer (plan §12.3).
"""

from __future__ import annotations

from modex_agent.persistence.session_artifacts.cleaner import (
    DefaultSessionArtifactCleaner,
    SessionArtifactCleaner,
    SessionDatabaseCleaner,
)
from modex_agent.persistence.session_artifacts.discovery import (
    discover_file_session_pool_map,
    discover_file_session_scopes,
)
from modex_agent.persistence.session_artifacts.models import (
    MissingSessionScopeError,
    SessionCleanupResult,
    SessionDatabaseCleanupError,
    SessionScopeIdentityError,
    SessionScopeMismatchError,
)
from modex_agent.persistence.session_artifacts.sqlite import (
    SqliteSessionDatabaseCleaner,
)

__all__ = [
    "DefaultSessionArtifactCleaner",
    "MissingSessionScopeError",
    "SessionArtifactCleaner",
    "SessionCleanupResult",
    "SessionDatabaseCleaner",
    "SessionDatabaseCleanupError",
    "SessionScopeIdentityError",
    "SessionScopeMismatchError",
    "SqliteSessionDatabaseCleaner",
    "discover_file_session_pool_map",
    "discover_file_session_scopes",
]
