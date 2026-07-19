"""Shared fixtures for file + sqlite backend conformance tests (T27).

Provides the building blocks each per-ABC conformance test file uses to
construct parametrized ``file`` and ``sqlite`` store fixtures:

- ``scope`` — a standard :class:`RecordScope` for SQLite adapters.
- ``sqlite_connection`` — an opened workspace :class:`ConnectionManager`.
- ``sqlite_registry_connection`` — an opened registry :class:`ConnectionManager`.
- ``file_storage_dir`` — a tmp directory for file-backed stores.
- ``msg`` — helper to build a minimal message dict.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from modex_agent.core.scope import RecordScope
from modex_agent.persistence import ConnectionManager, DatabaseKind


@pytest.fixture
def scope() -> RecordScope:
    """A standard RecordScope for conformance tests.

    Note: ``pool`` lives on ``BotRecordScope`` (ADR-0028); framework-side
    conformance tests use the base ``RecordScope`` without ``pool``.
    """
    return RecordScope(session_id="s1", agent_id="main")


@pytest_asyncio.fixture
async def sqlite_connection(tmp_path: Path) -> AsyncGenerator[ConnectionManager]:
    """Yield an opened workspace ConnectionManager, closing after the test."""
    mgr = ConnectionManager(tmp_path / "workspace.db", DatabaseKind.WORKSPACE)
    await mgr.open()
    yield mgr
    await mgr.close()


@pytest_asyncio.fixture
async def sqlite_registry_connection(tmp_path: Path) -> AsyncGenerator[ConnectionManager]:
    """Yield an opened registry ConnectionManager, closing after the test."""
    mgr = ConnectionManager(tmp_path / "registry.db", DatabaseKind.REGISTRY)
    await mgr.open()
    yield mgr
    await mgr.close()


@pytest.fixture
def file_storage_dir(tmp_path: Path) -> Path:
    """A directory for file-backed stores."""
    d = tmp_path / "file_store"
    d.mkdir(parents=True, exist_ok=True)
    return d


def msg(mid: str, content: str = "x", **extra: object) -> dict[str, Any]:
    """Build a minimal message dict with an id."""
    result: dict[str, Any] = {"id": mid, "role": "user", "content": content}
    result.update(extra)
    return result
