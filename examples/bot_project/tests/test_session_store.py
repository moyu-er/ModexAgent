"""Tests for WorkspacePoolSessionStore."""

from __future__ import annotations

from pathlib import Path

import pytest

from bot.service.session_store import WorkspacePoolSessionStore
from framework.core.session_id import SessionIdFactory


@pytest.fixture
def factory() -> SessionIdFactory:
    return SessionIdFactory()


async def test_path_partitioned_by_workspace_and_pool(
    tmp_path: Path, factory: SessionIdFactory
):
    store = WorkspacePoolSessionStore(
        base_dir=tmp_path
    )
    session = factory.create(agent_name="main")
    await store.save(session)
    expected_dir = tmp_path / "ws1" / "coding"
    assert expected_dir.is_dir()
    files = list(expected_dir.glob("*.json"))
    assert len(files) == 1
