from __future__ import annotations

import re
import sqlite3
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict

from modex_agent.persistence.connection import ConnectionManager

_MIGRATION_NAME: Final = re.compile(r"^(?P<version>\d{3,})_(?P<description>[a-z0-9_]+)\.sql$")
_TRANSACTION_CONTROL: Final = re.compile(
    r"^\s*(?:BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE)\b", re.IGNORECASE
)
_LEADING_SQL_COMMENTS: Final = re.compile(
    r"\A(?:\s|--[^\n]*(?:\n|\Z)|/\*.*?\*/)*", re.DOTALL
)


class DatabaseKind(StrEnum):
    WORKSPACE = "workspace"
    REGISTRY = "registry"


class MigrationFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int
    description: str
    path: Path


class InvalidMigrationNameError(ValueError):
    """Raised when a SQL migration file does not follow the versioned name format."""


class TransactionControlStatementError(ValueError):
    """Raised when migration SQL tries to manage its own transaction."""


class MigrationRunner:
    """Apply one ordered packaged migration stream with atomic version tracking."""

    def __init__(
        self,
        connection: ConnectionManager,
        database_kind: DatabaseKind,
        *,
        migration_dir: Path | None = None,
    ) -> None:
        self._connection = connection
        self._database_kind = database_kind
        self._migration_dir = migration_dir

    async def run_pending(self) -> None:
        await self._ensure_table()
        current_version = await self._current_version()
        for migration in self._migrations():
            if migration.version > current_version:
                await self._apply(migration)

    async def _ensure_table(self) -> None:
        await self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                description TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )

    async def _current_version(self) -> int:
        return await self._connection.query_value(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations", int
        )

    async def _apply(self, migration: MigrationFile) -> None:
        statements = _split_statements(migration.path.read_text(encoding="utf-8"))
        async with self._connection.transaction(immediate=True) as transaction:
            for statement in statements:
                await transaction.execute(statement)
            await transaction.execute(
                "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
                (migration.version, migration.description),
            )

    def _migrations(self) -> list[MigrationFile]:
        directory = self._migration_dir
        if directory is None:
            directory = Path(str(files("modex_agent.persistence.migrations"))) / self._database_kind.value
        migrations = [_parse_migration(path) for path in directory.glob("*.sql")]
        migrations.sort(key=lambda migration: migration.version)
        return migrations


def _parse_migration(path: Path) -> MigrationFile:
    match = _MIGRATION_NAME.fullmatch(path.name)
    if match is None:
        raise InvalidMigrationNameError(f"invalid migration filename: {path.name}")
    return MigrationFile(
        version=int(match.group("version")),
        description=match.group("description"),
        path=path,
    )


def _split_statements(sql: str) -> list[str]:
    statements: list[str] = []
    buffer: list[str] = []
    for character in sql:
        buffer.append(character)
        if character == ";" and sqlite3.complete_statement("".join(buffer)):
            _append_statement(statements, "".join(buffer))
            buffer.clear()
    _append_statement(statements, "".join(buffer))
    return statements


def _append_statement(statements: list[str], statement: str) -> None:
    cleaned = _LEADING_SQL_COMMENTS.sub("", statement).strip().rstrip(";").strip()
    if not cleaned:
        return
    if _TRANSACTION_CONTROL.match(cleaned):
        raise TransactionControlStatementError(
            "migration SQL must not contain transaction-control statements"
        )
    statements.append(cleaned)
