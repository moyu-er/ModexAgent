from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from bot.kb.builder import build_default_kb_provider
from bot.kb.provider import KbProvider
from bot.service import _assembly_helpers
from bot.service.model_choice import ModelChoiceRegistry
from bot.service.pool import create_pool

from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.hook import HookRunner
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.ioc.configs.app import AppConfig
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.multi_agent.pool_config.specs import MainAgentSpec, PoolSpec
from modex_agent.persistence.managers import WorkspacePersistenceManager
from modex_agent.tools.presets import ToolPreset


async def _build_tool_names(
    tmp_path: Path,
    kb_provider: KbProvider | None,
    *,
    app_config: AppConfig | None = None,
    persistence: WorkspacePersistenceManager | None = None,
) -> list[str]:
    main_spec = MainAgentSpec(agent_name="main", tool_preset=ToolPreset.NONE)
    pool_spec = PoolSpec(
        name="test-pool",
        main_agent_name="main",
        main=main_spec,
    )
    data_dir = tmp_path / ".modex"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "modexctl.bat").write_text("@exit /b 0\n", encoding="ascii")
    broker = InMemoryMessageBroker()
    await broker.start()
    with patch.dict("os.environ", {"MODEXBOT_BIN_DIR": str(bin_dir)}):
        pool_instance = await create_pool(
            pool_name="test-pool",
            pool_spec=pool_spec,
            assembly_deps=PoolAssemblyDeps(),
            project_dir=tmp_path,
            data_dir=data_dir,
            broker=broker,
            output_adapter=MagicMock(),
            safety=RuntimeSafetyPolicy(),
            retention=SessionRetentionPolicy(),
            im_ui=MagicMock(),
            shared_hooks=[],
            shared_hook_runner=HookRunner(),
            shared_interceptor_chain=InterceptorChain(),
            bot_model_config=None,
            model_choice_registry=ModelChoiceRegistry(),
            app_config=app_config,
            persistence=persistence,
            kb_provider=kb_provider,
        )
    try:
        return pool_instance.tool_manager.list_tools()
    finally:
        await pool_instance.pool.shutdown_all()
        await broker.stop()


@pytest.mark.asyncio
async def test_kb_tool_not_registered_by_default_through_create_pool(
    tmp_path: Path,
) -> None:
    app_config = AppConfig.model_validate({"persistence": {"backend": "sqlite"}})
    persistence = WorkspacePersistenceManager(tmp_path / ".modex" / "state.db")
    await persistence.open()
    kb_provider = await build_default_kb_provider(persistence.connection)

    try:
        tool_names = await _build_tool_names(
            tmp_path,
            kb_provider,
            app_config=app_config,
            persistence=persistence,
        )
    finally:
        await persistence.close()

    assert "kb" not in tool_names


@pytest.mark.asyncio
async def test_kb_tool_registered_when_register_flag_is_true(
    tmp_path: Path,
) -> None:
    main_spec = MainAgentSpec(agent_name="main", tool_preset=ToolPreset.NONE)
    PoolSpec(name="test-pool", main_agent_name="main", main=main_spec)
    data_dir = tmp_path / ".modex"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "modexctl.bat").write_text("@exit /b 0\n", encoding="ascii")
    broker = InMemoryMessageBroker()
    await broker.start()

    app_config = AppConfig.model_validate({"persistence": {"backend": "sqlite"}})
    persistence = WorkspacePersistenceManager(tmp_path / ".modex" / "state.db")
    await persistence.open()
    kb_provider = await build_default_kb_provider(persistence.connection)

    helper = _assembly_helpers._PoolAssemblyMixin()
    try:
        with patch.dict("os.environ", {"MODEXBOT_BIN_DIR": str(bin_dir)}):
            tm, _, _ = await helper._build_tools(
                main_spec,
                PoolAssemblyDeps(),
                MagicMock(),
                tmp_path,
                MagicMock(),
                "test-pool",
                data_dir,
                None,
                None,
                transcript_store=None,
                sessions_dir_provider=None,
                mcp_registry=None,
                persistence=persistence,
                app_config=app_config,
                kb_provider=kb_provider,
                register_kb_tool=True,
            )
        assert "kb" in tm.list_tools()
    finally:
        await persistence.close()
        await broker.stop()


@pytest.mark.asyncio
async def test_kb_tool_not_registered_when_provider_is_absent(tmp_path: Path) -> None:
    # Given / When
    tool_names = await _build_tool_names(tmp_path, None)

    # Then
    assert "kb" not in tool_names


def test_task_id_provider_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    monkeypatch.setenv("MODEX_TASK_ID", "task-123")

    # When
    task_id = _assembly_helpers._make_task_id_provider()()

    # Then
    assert task_id == "task-123"


def test_task_id_provider_returns_none_when_environment_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.delenv("MODEX_TASK_ID", raising=False)

    # When
    task_id = _assembly_helpers._make_task_id_provider()()

    # Then
    assert task_id is None


def test_task_id_provider_preserves_empty_environment_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setenv("MODEX_TASK_ID", "")

    # When
    task_id = _assembly_helpers._make_task_id_provider()()

    # Then
    assert task_id == ""


def test_session_id_provider_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    monkeypatch.setenv("MODEX_SESSION_ID", "session-123")

    # When
    session_id = _assembly_helpers._make_session_id_provider()()

    # Then
    assert session_id == "session-123"


def test_session_id_provider_returns_none_when_environment_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.delenv("MODEX_SESSION_ID", raising=False)

    # When
    session_id = _assembly_helpers._make_session_id_provider()()

    # Then
    assert session_id is None


def test_session_id_provider_preserves_empty_environment_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setenv("MODEX_SESSION_ID", "")

    # When
    session_id = _assembly_helpers._make_session_id_provider()()

    # Then
    assert session_id == ""
