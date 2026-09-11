"""Real service assembly must use the editor project without bot files there."""
from __future__ import annotations

from pathlib import Path

import pytest
from bot.acp.runtime import AcpEntryConfig, AcpRuntime

from modex_agent.acp.types import AcpBackendError, AcpBackendErrorCode, AcpOpenKind, AcpOpenRequest


def write_config_and_project(tmp_path: Path) -> tuple[Path, Path]:
    config = tmp_path / "settings"
    (config / "scopes").mkdir(parents=True)
    (config / "bot_config.yml").write_text("persistence:\n  backend: sqlite\n", encoding="utf-8")
    (config / "scopes" / "bot.yml").write_text(
        "pool:\n  name: main\n  agents:\n    main:\n      capabilities:\n        skills: false\n",
        encoding="utf-8",
    )
    project = tmp_path / "editor-project"
    project.mkdir()
    return config, project


async def test_real_project_boot_uses_explicit_config_and_editor_data_root(tmp_path: Path) -> None:
    config, project = write_config_and_project(tmp_path)
    runtime = AcpRuntime(config, AcpEntryConfig(pool="main"))
    try:
        await runtime.bind_project(project)
        assert runtime.project_root == project
        assert runtime.pool.pool_name == "main"
        assert not (project / "config").exists()
        assert (project / ".modex" / "state.db").exists()
        assert not (config.parent / ".modex").exists()
        resources = runtime._resources
        assert resources is not None
        store = resources.session_index_store
        assert await store.list_sessions() == []
    finally:
        await runtime.close()


async def test_post_initialize_boot_failure_stops_service_before_raising(tmp_path: Path) -> None:
    """A failure after service.initialize() must run the same stop as close().

    The boot task sets runtime._service BEFORE initialize() resolves, so a
    _select_pool/native-strategy failure must still drain the fully started
    service (registry evict_all, provider aclose, MCP shutdown, stores close)
    before the error surfaces — not leak the live resources to process exit.
    """
    config, project = write_config_and_project(tmp_path)
    runtime = AcpRuntime(config, AcpEntryConfig(pool="missing"))
    try:
        with pytest.raises(AcpBackendError) as raised:
            await runtime.open(AcpOpenRequest(kind=AcpOpenKind.NEW, cwd=project))
        assert raised.value.code == AcpBackendErrorCode.BOOT_FAILED
        assert "missing" in raised.value.message
        service = runtime._service
        assert service is not None
        # BotService.stop() nulls the routing store handle (core.py stop tail) —
        # only the single close owner's path does this; an un-stopped leaked
        # service keeps the store (and its SQLite writer) alive to process
        # exit (observed as a hung pytest process after output finished).
        assert service.pool_session_store is None
    finally:
        await runtime.close()


async def test_boot_holds_opencode_lifecycle_owner_until_after_service_stop(
    tmp_path: Path,
) -> None:
    """The ACP entry is the shared ``opencode serve`` process owner.

    ACP never runs ``BotService.start()`` (it blocks on the shutdown
    event), so ``_boot`` enters the SAME ``OpenCodeServerManager.lifecycle()``
    context the resident entry holds — without spawning the process (entering
    binds ownership only; the first acquire() stays lazy). ``_close`` releases
    it LAST, after ``service.stop()`` drained the pools that could still hold
    external sessions.
    """
    from modex_agent.agents.external.providers.opencode import server_manager

    config, project = write_config_and_project(tmp_path)
    runtime = AcpRuntime(config, AcpEntryConfig(pool="main"))
    try:
        await runtime.bind_project(project)
        # ownership is bound, but no process was spawned by binding alone
        assert server_manager.OpenCodeServerManager._lifecycle_bound
        assert server_manager.OpenCodeServerManager._instance is not None
        assert server_manager.OpenCodeServerManager._instance._proc is None
        stop_order: list[str] = []
        service = runtime._service
        assert service is not None
        original_stop = service.stop

        async def _traced_stop() -> None:
            stop_order.append("service.stop")
            await original_stop()

        service.stop = _traced_stop  # type: ignore[method-assign]
        await runtime.close()
        assert stop_order == ["service.stop"]
        # the lifecycle owner was released (and could be re-bound by a fresh
        # runtime in the same process)
        assert not server_manager.OpenCodeServerManager._lifecycle_bound
        assert server_manager.OpenCodeServerManager._instance is None
    finally:
        await runtime.close()


async def test_failed_boot_releases_opencode_lifecycle_owner(tmp_path: Path) -> None:
    """A boot failure after the lifecycle was entered must release it.

    The failed-boot path drains through the single close owner; the
    ExitStack release rides that same path, so a rejected pool selection
    cannot leave the singleton bound to a dead runtime.
    """
    from modex_agent.agents.external.providers.opencode import server_manager

    config, project = write_config_and_project(tmp_path)
    runtime = AcpRuntime(config, AcpEntryConfig(pool="missing"))
    with pytest.raises(AcpBackendError):
        await runtime.open(AcpOpenRequest(kind=AcpOpenKind.NEW, cwd=project))
    assert not server_manager.OpenCodeServerManager._lifecycle_bound
    assert server_manager.OpenCodeServerManager._instance is None
    await runtime.close()


async def test_boot_registers_transcript_store_with_service_and_home_resources(
    tmp_path: Path,
) -> None:
    """The scoped transcript store is owner-visible.

    ``_prepare_inputs`` publishes the same ``WorkspaceScopedTranscriptStore``
    on the service slot and the materialized home bundle, so the EXISTING
    owner path releases it on workspace eviction
    (``_stop_resources → release_workspace``) — the ACP runtime holds no
    private resource invisible to the owner.
    """
    config, project = write_config_and_project(tmp_path)
    runtime = AcpRuntime(config, AcpEntryConfig(pool="main"))
    try:
        await runtime.bind_project(project)
        service = runtime._service
        resources = runtime._resources
        assert service is not None and resources is not None
        assert service._transcript_store is runtime._transcript
        assert resources.transcript_store is runtime._transcript
    finally:
        await runtime.close()
