"""Conformance: migration 001 state CHECK values match MessageRowState enum.

Prevents drift between the SQL schema's CHECK constraint and the Python enum
that drives all parameterized queries in :class:`SqliteMessageStore`.
"""

from __future__ import annotations

import re
from pathlib import Path

from modex_agent.persistence.adapters.message_store import MessageRowState

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "modex_agent"
    / "persistence"
    / "migrations"
    / "workspace"
    / "001_initial.sql"
)


def test_migration_state_check_matches_enum() -> None:
    sql = _MIGRATION.read_text(encoding="utf-8")
    offset = sql.find("memory_session_messages")
    assert offset != -1, "memory_session_messages table not found in migration"
    section = sql[offset:]
    match = re.search(r"CHECK\s*\(\s*state\s+IN\s*\(([^)]+)\)\s*\)", section)
    assert match is not None, "state CHECK IN constraint not found in migration"
    sql_values = {s.strip().strip("'\"") for s in match.group(1).split(",")}
    enum_values = {s.value for s in MessageRowState}
    assert sql_values == enum_values, (
        f"migration CHECK values {sql_values} != enum values {enum_values}"
    )
