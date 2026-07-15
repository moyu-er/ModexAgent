"""Namespaced migrations for bot-owned tables in a workspace database."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Final

from modex_agent.persistence.connection import ConnectionManager

_MIGRATION_NAME: Final = re.compile(
    r"^(?P<version>\d{3,})_(?P<description>[a-z0-9_]+)\.sql$"
)
_TRANSACTION_CONTROL: Final = re.compile(
    r"^\s*(?:BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE)\b", re.IGNORECASE
)
_NAMESPACE: Final = "bot_project_workspace"


class BotWorkspaceMigrationRunner:
    """Apply bot-owned workspace migrations without sharing framework versions."""

    def __init__(self, connection: ConnectionManager) -> None:
        self._connection = connection
        self._migration_dir = Path(__file__).parent / "migrations" / "workspace"

    async def run_pending(self) -> None:
        await self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_schema_migrations (
                namespace TEXT NOT NULL,
                version INTEGER NOT NULL,
                description TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (namespace, version)
            )
            """
        )
        current_version = await self._connection.query_value(
            "SELECT COALESCE(MAX(version), 0) FROM bot_schema_migrations "
            "WHERE namespace = ?",
            int,
            (_NAMESPACE,),
        )
        for path in sorted(self._migration_dir.glob("*.sql")):
            match = _MIGRATION_NAME.fullmatch(path.name)
            if match is None:
                raise ValueError(f"invalid bot migration filename: {path.name}")
            version = int(match.group("version"))
            if version <= current_version:
                continue
            statements = _split_statements(path.read_text(encoding="utf-8"))
            async with self._connection.transaction(immediate=True) as transaction:
                for statement in statements:
                    await transaction.execute(statement)
                await transaction.execute(
                    "INSERT INTO bot_schema_migrations "
                    "(namespace, version, description) VALUES (?, ?, ?)",
                    (_NAMESPACE, version, match.group("description")),
                )


def _split_statements(sql: str) -> list[str]:
    statements: list[str] = []
    buffer: list[str] = []
    for character in sql:
        buffer.append(character)
        if character != ";" or not sqlite3.complete_statement("".join(buffer)):
            continue
        _append_statement(statements, "".join(buffer))
        buffer.clear()
    _append_statement(statements, "".join(buffer))
    return statements


def _append_statement(statements: list[str], statement: str) -> None:
    cleaned = statement.strip().rstrip(";").strip()
    if not cleaned:
        return
    if _TRANSACTION_CONTROL.match(cleaned):
        raise ValueError("bot migration must not contain transaction control")
    statements.append(cleaned)
