"""WorkspaceScopedTranscriptStore ctxvar model (Task 3).

Writes route by the ``bind_workspace_root`` ctxvar; reads accept an explicit
``sessions_dir`` override (falling back to the ctxvar root when omitted).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.events import UserMessageEvent
from modex_agent.workspace.runtime import bind_workspace_root


_DATA_DIR_NAME = ".modex"


def _store() -> WorkspaceScopedTranscriptStore:
    return WorkspaceScopedTranscriptStore(data_dir_name=_DATA_DIR_NAME)


def _sessions_dir(root: Path) -> Path:
    from modex_agent.workspace.paths import WorkspacePaths

    return WorkspacePaths(root=root / _DATA_DIR_NAME).sessions_dir


@pytest.mark.asyncio
async def test_append_lands_in_bound_workspace(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    ws_b = tmp_path / "wsB"
    ws_b.mkdir()
    store = _store()
    with bind_workspace_root(ws_b):
        await store.append(
            "convB.main",
            UserMessageEvent(session_id="convB.main", agent_name="main", content="hi"),
        )
    assert (ws_b / ".modex" / "sessions" / "main" / "convB.main.jsonl").exists()
    assert not (home / ".modex" / "sessions").exists() or not (
        home / ".modex" / "sessions" / "main" / "convB.main.jsonl"
    ).exists()


@pytest.mark.asyncio
async def test_no_bind_defaults_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.chdir(home)
    store = _store()
    await store.append(
        "convX.main",
        UserMessageEvent(session_id="convX.main", agent_name="main", content="hi"),
    )
    assert (home / ".modex" / "sessions" / "main" / "convX.main.jsonl").exists()


@pytest.mark.asyncio
async def test_read_with_explicit_sessions_dir(tmp_path: Path) -> None:
    ws_b = tmp_path / "wsB"
    ws_b.mkdir()
    store = _store()
    with bind_workspace_root(ws_b):
        await store.append(
            "convB.main",
            UserMessageEvent(session_id="convB.main", agent_name="main", content="hi"),
        )
    b_sessions = _sessions_dir(ws_b)
    assert "convB.main" in await store.list_sessions(b_sessions)
    home_sessions = _sessions_dir(tmp_path / "home")
    assert "convB.main" not in await store.list_sessions(home_sessions)


@pytest.mark.asyncio
async def test_load_reads_explicit_dir_without_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_b = tmp_path / "wsB"
    ws_b.mkdir()
    store = _store()
    with bind_workspace_root(ws_b):
        await store.append(
            "convB.main",
            UserMessageEvent(session_id="convB.main", agent_name="main", content="hi"),
        )
    # Read from an unrelated cwd with the explicit sessions_dir.
    monkeypatch.chdir(tmp_path)
    events = await store.load("convB.main", sessions_dir=_sessions_dir(ws_b))
    assert len(events) == 1
