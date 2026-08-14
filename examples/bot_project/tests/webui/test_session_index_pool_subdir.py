"""Regression: session index files must land in pool subdirectories.

The bug (2026-06-21): ``E:\\download\bot\\.modex\\session_index\\87c236de3a2b.coding.json``
was directly in ``session_index/`` root, not ``session_index/coding/``.

Root cause: ``wiring.py:209`` created a ``LocalFileSessionStore`` (which writes
``root/file.json``) instead of ``WorkspacePoolSessionStore`` (which writes
``root/<pool>/file.json``). After the home workspace materialized, the
``_session_store`` property returned this ``LocalFileSessionStore``, so all
session index writes bypassed the pool subdirectory layer.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from bot.service.session_store import WorkspacePoolSessionStore

from modex_agent.core.session_id import SessionInfo, now_ms

_DATA_DIR_NAME = ".modex"


@pytest.mark.asyncio
async def test_coding_session_index_lands_in_coding_subdir() -> None:
    """A session with agent_name='coding' must be saved under
    ``session_index/coding/``, not ``session_index/`` root."""
    with tempfile.TemporaryDirectory() as tmp:
        index_dir = Path(tmp) / "session_index"
        index_dir.mkdir(parents=True)

        store = WorkspacePoolSessionStore(
            base_dir=index_dir,
            pool_resolver=lambda s: {"coding": "coding", "main": "main"}.get(
                s.agent_name, "main"
            ),
            data_dir_name=_DATA_DIR_NAME,
        )

        session = SessionInfo(
            session_id="abc123.coding",
            agent_name="coding",
            created_at=now_ms(),
            updated_at=now_ms(),
        )
        await store.save(session)

        # File must exist under coding/ subdir.
        coding_dir = index_dir / "coding"
        assert coding_dir.is_dir(), f"coding/ subdir not created at {coding_dir}"
        files = list(coding_dir.glob("*.json"))
        assert len(files) == 1, f"expected 1 file in coding/, got {files}"
        assert "abc123.coding" in files[0].name

        # File must NOT exist in root.
        root_files = list(index_dir.glob("*.json"))
        assert len(root_files) == 0, (
            f"LEAK: session index file in root, not pool subdir; "
            f"root_files={root_files}"
        )


@pytest.mark.asyncio
async def test_main_session_index_lands_in_main_subdir() -> None:
    """A session with agent_name='main' must be saved under
    ``session_index/main/``, not ``session_index/`` root."""
    with tempfile.TemporaryDirectory() as tmp:
        index_dir = Path(tmp) / "session_index"
        index_dir.mkdir(parents=True)

        store = WorkspacePoolSessionStore(
            base_dir=index_dir,
            pool_resolver=lambda s: {"coding": "coding", "main": "main"}.get(
                s.agent_name, "main"
            ),
            data_dir_name=_DATA_DIR_NAME,
        )

        session = SessionInfo(
            session_id="xyz789.main",
            agent_name="main",
            created_at=now_ms(),
            updated_at=now_ms(),
        )
        await store.save(session)

        main_dir = index_dir / "main"
        assert main_dir.is_dir(), f"main/ subdir not created at {main_dir}"
        files = list(main_dir.glob("*.json"))
        assert len(files) == 1, f"expected 1 file in main/, got {files}"
        assert "xyz789.main" in files[0].name

        root_files = list(index_dir.glob("*.json"))
        assert len(root_files) == 0, (
            f"LEAK: session index file in root, not pool subdir; "
            f"root_files={root_files}"
        )


@pytest.mark.asyncio
async def test_list_sessions_finds_session_in_pool_subdir() -> None:
    """list_sessions must find sessions saved in pool subdirs."""
    with tempfile.TemporaryDirectory() as tmp:
        index_dir = Path(tmp) / "session_index"
        index_dir.mkdir(parents=True)

        store = WorkspacePoolSessionStore(
            base_dir=index_dir,
            pool_resolver=lambda s: {"coding": "coding", "main": "main"}.get(
                s.agent_name, "main"
            ),
            data_dir_name=_DATA_DIR_NAME,
        )

        session = SessionInfo(
            session_id="abc123.coding",
            agent_name="coding",
            created_at=now_ms(),
            updated_at=now_ms(),
        )
        await store.save(session)

        loaded = await store.list_sessions()
        ids = {s.session_id for s in loaded}
        assert "abc123.coding" in ids, (
            f"session not found in list_sessions; ids={ids}"
        )
