from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[3]))

from bot.service._external_coding_wiring import build_external_coding_env_spec
from bot.service.builders import build_inbox

from modex_agent.core.scope import RecordScope
from modex_agent.ioc.configs.app import AppConfig
from modex_agent.multi_agent.inbox.server_local import LocalFileInboxMQ
from modex_agent.multi_agent.pool_config.specs import MainAgentSpec, PoolSpec
from modex_agent.persistence.adapters.inbox_mq import SqliteInboxMQ
from modex_agent.persistence.config import PersistenceBackend, PersistenceConfig
from modex_agent.persistence.managers.workspace import WorkspacePersistenceManager


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
    assert inbox._scope == RecordScope(pool="pool_coder")


def test_build_inbox_keeps_file_backend(tmp_path: Path) -> None:
    config = AppConfig(
        persistence=PersistenceConfig(backend=PersistenceBackend.FILE)
    )

    inbox = build_inbox(
        config,
        None,
        tmp_path / "inbox",
        tmp_path / "state.db",
        "pool_coder",
    )

    assert isinstance(inbox, LocalFileInboxMQ)


def test_external_coding_env_keeps_workspace_inbox_contract(tmp_path: Path) -> None:
    workspace_dir = tmp_path / "workspace"
    inbox_root = workspace_dir / ".modex" / "inbox"
    pool_spec = PoolSpec(
        name="pool_coder",
        main_agent_name="coder",
        main=MainAgentSpec(agent_name="coder"),
    )

    env_spec = build_external_coding_env_spec(
        pool_name="pool_coder",
        pool_spec=pool_spec,
        project_dir=tmp_path,
        inbox_dir=inbox_root / "pool_coder",
        workspace_dir=workspace_dir,
        main_agent_name="coder",
    )

    assert env_spec.inbox_root == inbox_root
    assert env_spec.inbox_root.parent / "state.db" == workspace_dir / ".modex" / "state.db"
