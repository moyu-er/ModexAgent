"""Storage-neutral session database cleanup capability."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from modex_agent.core.scope import RecordScope


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


class SessionDatabaseCleaner(ABC):
    """Deletes structured records owned by one exact canonical session scope."""

    @abstractmethod
    async def delete_session_rows(self, scope: RecordScope) -> int:
        """Delete rows whose stored scope equals ``scope.canonical()``.

        ``scope.session_id`` is required; ``scope.pool`` is optional. The
        implementation owns canonicalization so callers pass the typed identity
        without constructing or serializing database keys.
        """
        ...

    @abstractmethod
    async def list_session_scopes(
        self,
        session_ids: frozenset[str] | None = None,
    ) -> list[RecordScope]:
        """List complete persisted scopes containing a session identity.

        ``None`` lists every session scope. A supplied set filters by exact
        ``RecordScope.session_id`` equality after persisted identities have
        been parsed.
        """
        ...


__all__ = [
    "MissingSessionScopeError",
    "SessionDatabaseCleaner",
    "SessionDatabaseCleanupError",
    "SessionScopeIdentityError",
    "SessionScopeMismatchError",
]
