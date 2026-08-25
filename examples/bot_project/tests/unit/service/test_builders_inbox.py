from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from bot.scope import BotRecordScope
from bot.service.builders import build_inbox
from bot.service.external_strategy import build_external_env_spec

from modex_agent.ioc.configs.app import AppConfig
from modex_agent.multi_agent.inbox.server_local import LocalFileInboxMQ
from modex_agent.persistence.adapters.inbox_mq import SqliteInboxMQ
from modex_agent.persistence.config import PersistenceBackend, PersistenceConfig
from modex_agent.persistence.managers.workspace import WorkspacePersistenceManager
from modex_agent.scope.spec import AgentSpec, PoolSpec


def test_build_inbox_uses_pool_record_scope_for_sqlite(tmp_path: Path) -> None:
    persistence = WorkspacePersistenceManager(tmp_path / "state.db")

    inbox = build_inbox(
        AppConfig(),
        persistence,
        tmp_path / "inbox",
        tmp_path / "state.db",
        "pool_coder",
    )

    assert isinstance(inbox, SqliteInboxMQ)
    assert inbox._scope == BotRecordScope(pool="pool_coder")


def test_build_inbox_keeps_file_backend(tmp_path: Path) -> None:
    config = AppConfig(persistence=PersistenceConfig(backend=PersistenceBackend.FILE))

    inbox = build_inbox(
        config,
        None,
        tmp_path / "inbox",
        tmp_path / "state.db",
        "pool_coder",
    )

    assert isinstance(inbox, LocalFileInboxMQ)


def test_external_env_keeps_workspace_inbox_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / ("modexctl.bat" if sys.platform == "win32" else "modexctl")).write_text(
        "@echo off\n"
    )
    monkeypatch.setenv("MODEXBOT_BIN_DIR", str(bin_dir))

    workspace_dir = tmp_path / "workspace"
    inbox_root = workspace_dir / ".modex" / "inbox"
    pool_spec = PoolSpec(name="pool_coder", agents=[AgentSpec(name="coder")])

    env_spec = build_external_env_spec(
        pool_name="pool_coder",
        pool_spec=pool_spec,
        peer_links=(),
        project_dir=tmp_path,
        inbox_dir=inbox_root / "pool_coder",
        workspace_dir=workspace_dir,
        root_agent_name="coder",
    )

    assert env_spec.inbox_root == inbox_root
    assert env_spec.inbox_root.parent / "state.db" == workspace_dir / ".modex" / "state.db"


def test_main_agent_env_spec_defaults_to_normal_comm_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Main-agent env spec MUST default to comm_kind=NORMAL + parent_session_id=None.

    Regression guard: if a future change to build_external_env_spec
    accidentally sets comm_kind=SUBAGENT or a non-None parent_session_id,
    every main-agent modexctl send would route via the subagent branch
    (target_sid = MODEX_PARENT_SESSION_ID), which is either None (error)
    or a wrong session — silently breaking all main-agent peer messaging.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / ("modexctl.bat" if sys.platform == "win32" else "modexctl")).write_text(
        "@echo off\n"
    )
    monkeypatch.setenv("MODEXBOT_BIN_DIR", str(bin_dir))

    from modex_agent.core.agent import AgentCommKind

    workspace_dir = tmp_path / "workspace"
    inbox_root = workspace_dir / ".modex" / "inbox"
    pool_spec = PoolSpec(name="pool_coder", agents=[AgentSpec(name="coder")])

    env_spec = build_external_env_spec(
        pool_name="pool_coder",
        pool_spec=pool_spec,
        peer_links=(),
        project_dir=tmp_path,
        inbox_dir=inbox_root / "pool_coder",
        workspace_dir=workspace_dir,
        root_agent_name="coder",
    )

    assert env_spec.comm_kind is AgentCommKind.NORMAL
    assert env_spec.parent_session_id is None
