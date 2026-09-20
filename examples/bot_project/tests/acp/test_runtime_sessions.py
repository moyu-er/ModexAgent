"""Backend admission uses the bound project's existing pool and session store."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from bot.acp.runtime import AcpEntryConfig, AcpRuntime

from modex_agent.acp.types import AcpBackendError, AcpBackendErrorCode, AcpOpenKind, AcpOpenRequest


async def test_backend_rejects_invalid_cwd_without_claiming_project(tmp_path: Path) -> None:
    runtime = AcpRuntime(tmp_path, AcpEntryConfig())
    with pytest.raises(AcpBackendError) as raised:
        await runtime.open(AcpOpenRequest(kind=AcpOpenKind.NEW, cwd=tmp_path / "missing"))
    assert raised.value.code == AcpBackendErrorCode.INVALID_CWD
    assert runtime.project_root is None
    await runtime.close()


async def test_new_session_can_be_loaded_from_same_project_after_restart(tmp_path: Path) -> None:
    from .test_project_boot import write_config_and_project

    config, project = write_config_and_project(tmp_path)
    first = AcpRuntime(config, AcpEntryConfig(pool="main"))
    try:
        handle = await first.open(AcpOpenRequest(kind=AcpOpenKind.NEW, cwd=project))
        session_id = handle.session_id
        with pytest.raises(AcpBackendError) as busy:
            await first.open(AcpOpenRequest(kind=AcpOpenKind.LOAD, cwd=project, session_id=session_id))
        assert busy.value.code == AcpBackendErrorCode.BUSY
    finally:
        await first.close()
    second = AcpRuntime(config, AcpEntryConfig(pool="main"))
    try:
        outcomes = await asyncio.gather(
            second.open(AcpOpenRequest(kind=AcpOpenKind.LOAD, cwd=project, session_id=session_id)),
            second.open(AcpOpenRequest(kind=AcpOpenKind.LOAD, cwd=project, session_id=session_id)),
            return_exceptions=True,
        )
        handles = [value for value in outcomes if not isinstance(value, BaseException)]
        failures = [value for value in outcomes if isinstance(value, AcpBackendError)]
        assert len(handles) == 1
        assert len(failures) == 1 and failures[0].code == AcpBackendErrorCode.BUSY
        assert handles[0].session_id == session_id
        assert await handles[0].read_history() == []
        await handles[0].close()
        reopened = await second.open(AcpOpenRequest(kind=AcpOpenKind.LOAD, cwd=project, session_id=session_id))
        assert reopened.session_id == session_id
    finally:
        await second.close()


async def test_backend_after_close_never_starts_project(tmp_path: Path) -> None:
    runtime = AcpRuntime(tmp_path, AcpEntryConfig())
    await runtime.close()
    with pytest.raises(AcpBackendError):
        await runtime.open(AcpOpenRequest(kind=AcpOpenKind.NEW, cwd=tmp_path))
    assert runtime.project_root is None
