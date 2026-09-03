"""Session artifact cleanup result model and error types."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.core.scope import RecordScope

__all__ = [
    "MissingSessionScopeError",
    "SessionCleanupResult",
    "SessionDatabaseCleanupError",
    "SessionScopeIdentityError",
    "SessionScopeMismatchError",
]


class SessionScopeIdentityError(ValueError):
    pass


class MissingSessionScopeError(SessionScopeIdentityError):
    def __str__(self) -> str:
        return "session cleanup requires session_id in scope"


class SessionScopeMismatchError(SessionScopeIdentityError):
    def __init__(self, session_id: str, scope_session_id: str) -> None:
        self.session_id = session_id
        self.scope_session_id = scope_session_id
        super().__init__(session_id, scope_session_id)

    def __str__(self) -> str:
        return (
            f"cleanup session_id {self.session_id!r} does not match "
            f"scope.session_id {self.scope_session_id!r}"
        )


class SessionDatabaseCleanupError(Exception):
    """Storage-neutral failure to clean or discover session records."""

    def __init__(self, scope: RecordScope | None = None) -> None:
        self.scope = scope
        super().__init__(scope)

    def __str__(self) -> str:
        return "session database cleanup failed"


class SessionCleanupResult(BaseModel):
    """Outcome of cleaning one session's artifacts.

    Frozen Pydantic model: the result is a value object summarising what was
    removed.  ``errors`` collects non-fatal failures (a missing target is NOT
    an error — only unexpected ``OSError`` / DB failures).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    db_rows_deleted: int = 0
    files_deleted: int = 0
    dirs_deleted: int = 0
    errors: list[str] = Field(default_factory=list)
