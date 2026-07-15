from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.persistence.transcript import build_transcript_store_resolver
from bot.service.session_store import WorkspacePoolSessionStore
from bot.service.web_ui_service import WebUIService
from bot.webui.transcript_store import TranscriptStore

from modex_agent.core.session_store import LocalFileSessionStore
from modex_agent.ioc.configs.app import AppConfig
from modex_agent.persistence.config import PersistenceBackend


async def test_sqlite_access_returns_materialized_workspace_store(tmp_path: Path) -> None:
    service = WebUIService.__new__(WebUIService)
    service._app_config = AppConfig.model_validate(
        {"persistence": {"backend": "sqlite"}}
    )
    expected = LocalFileSessionStore(tmp_path / "sentinel")
    registry = SimpleNamespace(
        get_or_open=AsyncMock(return_value=object()),
        materialize=AsyncMock(
            return_value=SimpleNamespace(session_index_store=expected)
        ),
    )
    service.workspace_stack = SimpleNamespace(registry=registry)

    store = await service._session_store_for_index(
        tmp_path / "workspace" / ".modex" / "session_index"
    )

    assert store is expected


async def test_file_access_reconstructs_workspace_file_store(tmp_path: Path) -> None:
    service = WebUIService.__new__(WebUIService)
    service._app_config = AppConfig.model_validate(
        {"persistence": {"backend": "file"}}
    )
    service._data_dir_name = ".modex"
    service._pool_for_agent = lambda agent_name: agent_name

    store = await service._session_store_for_index(tmp_path / "session_index")

    assert isinstance(store, WorkspacePoolSessionStore)


async def test_file_transcript_backend_does_not_resolve_database() -> None:
    connection_resolver = AsyncMock()

    resolver = build_transcript_store_resolver(
        PersistenceBackend.FILE,
        connection_resolver,
    )

    assert resolver is None
    connection_resolver.assert_not_called()


async def test_database_transcript_resolver_accepts_provider_neutral_adapter(
    tmp_path: Path,
) -> None:
    alternate_adapter = AsyncMock(spec=TranscriptStore)
    database_resolver = AsyncMock(return_value=alternate_adapter)
    resolver = build_transcript_store_resolver(
        PersistenceBackend.SQLITE,
        database_resolver,
    )
    assert resolver is not None

    resolved = await resolver(tmp_path / "sessions")

    assert resolved is alternate_adapter
    database_resolver.assert_awaited_once_with(tmp_path / "sessions")
