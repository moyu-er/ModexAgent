"""Session artifact cleanup result model and error types."""

from __future__ import annotations

from dataclasses import dataclass

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


@dataclass(frozen=True, slots=True)
class MissingSessionScopeError(SessionScopeIdentityError):
    def __str__(self) -> str:
        return "session cleanup requires session_id in scope"


@dataclass(frozen=True, slots=True)
class SessionScopeMismatchError(SessionScopeIdentityError):
    session_id: str
    scope_session_id: str

    def __str__(self) -> str:
        return (
            f"cleanup session_id {self.session_id!r} does not match "
            f"scope.session_id {self.scope_session_id!r}"
        )


@dataclass(frozen=True, slots=True)
class SessionDatabaseCleanupError(Exception):
    """Storage-neutral failure to clean or discover session records."""

    scope: RecordScope | None = None

    def __str__(self) -> str:
        return "session database cleanup failed"


class SessionCleanupResult(BaseModel):
    """Outcome of cleaning one session's artifacts.

    Frozen Pydantic model: the result is a value object summarising what was
    removed.  ``errors`` collects non-fatal failures (a missing target is NOT
    an error — only unexpected ``OSError`` / DB failures).
    """

    model_config = ConfigDict(frozen=True)

    db_rows_deleted: int = 0
    files_deleted: int = 0
    dirs_deleted: int = 0
    errors: list[str] = Field(default_factory=list)
