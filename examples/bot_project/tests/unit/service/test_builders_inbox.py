from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from bot.config.webui_config import build_control_origin
from bot.scope import BotRecordScope
from bot.service.builders import build_inbox
from bot.service.external_strategy import (
    ExternalExecutionStrategy,
    build_external_env_spec,
)
from bot.workspace.handle import WorkspaceHandle

from modex_agent.core.agent import ExecutionStrategyKind, ProviderKind
from modex_agent.ioc.configs.app import AppConfig
from modex_agent.multi_agent.execution_strategy import PoolAssemblyContext
from modex_agent.multi_agent.inbox.server_local import LocalFileInboxMQ
from modex_agent.persistence.adapters.inbox_mq import SqliteInboxMQ
from modex_agent.persistence.config import PersistenceBackend, PersistenceConfig
from modex_agent.persistence.managers.workspace import WorkspacePersistenceManager
from modex_agent.scope.spec import AgentSpec, PoolSpec
from modex_agent.workspace.scope_path import ScopePath


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
        inbox_dir=inbox_root / "pool_coder",
        workspace_dir=workspace_dir,
        root_agent_name="coder",
        control_origin="",
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
        inbox_dir=inbox_root / "pool_coder",
        workspace_dir=workspace_dir,
        root_agent_name="coder",
        control_origin="",
    )

    assert env_spec.comm_kind is AgentCommKind.NORMAL
    assert env_spec.parent_session_id is None


@pytest.mark.asyncio
async def test_external_main_uses_runtime_root_and_context_control_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """External main consumes the assembly context's authoritative runtime
    root and control origin; it must not recompute either from project_dir."""
    resource_root = tmp_path / "bot-assets"
    workspace_root = tmp_path / "ide-workspace"
    config_dir = tmp_path / "actual-config"
    for path in (resource_root, workspace_root, config_dir):
        path.mkdir()
    (config_dir / "bot_config.yml").write_text(
        "webui:\n  host: 0.0.0.0\n  port: 32123\n", encoding="utf-8",
    )
    expected_origin = build_control_origin(config_dir)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / ("modexctl.bat" if sys.platform == "win32" else "modexctl")).write_text(
        "@echo off\n", encoding="ascii",
    )
    monkeypatch.setenv("MODEXBOT_BIN_DIR", str(bin_dir))
    monkeypatch.setattr(
        "bot.service.external_strategy.shutil.which", lambda _: "opencode"
    )

    pool_spec = PoolSpec(
        name="external",
        agents=[AgentSpec(
            name="opencode",
            execution_strategy=ExecutionStrategyKind.EXTERNAL,
            provider_kind=ProviderKind.OPENCODE,
        )],
    )
    strategy = ExternalExecutionStrategy()
    monkeypatch.setattr(strategy, "_build_external_backend", lambda _: MagicMock())
    context = PoolAssemblyContext(
        pool_name="external",
        pool_spec=pool_spec,
        project_dir=resource_root,
        data_dir=workspace_root / ".modex",
        broker=MagicMock(),
        inbox_server=MagicMock(),
        agent_bus=MagicMock(),
        output_adapter=MagicMock(),
        safety=MagicMock(),
        retention=MagicMock(),
        registry=MagicMock(),
        workspace_handle=WorkspaceHandle(
            target=workspace_root, data_root=workspace_root / ".modex",
        ),
        scope_path=ScopePath(workspace_root=workspace_root, pool_name="external"),
        control_origin=expected_origin,
    )

    assembled = await strategy.assemble_main(context)

    assert assembled.external_deps is not None
    spec = assembled.external_deps["spec"]
    assert spec.workspace_root == workspace_root
    assert spec.workdir == workspace_root
    assert spec.control_origin == expected_origin
