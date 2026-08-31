"""KbTool registration — the factory road (the builder flag road is dead).

The ``kb`` tool ships REGISTERED-but-UNREFERENCED: ``KbToolFactory``
lives in the TOOL slot; enabling it for an agent is a declaration concern
(``tools: [+kb]``), and the factory resolves the workspace's
``KbProvider`` from the context chain at assembly time. The retired
``register_kb_tool`` builder flag (a parallel enable path outside the
roster system) is deleted — death-grepped below.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from bot.kb.provider import KbProvider
from bot.tools.kb import KbTool
from bot.workspace.handle import PoolWorkspaceResources
from plugins.bot_hooks import BotHooksPlugin, KbToolFactory

from modex_agent.plugins.abc import ComponentSlot
from modex_agent.plugins.assembly.context import WorkspaceContext
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry


def _registered() -> ComponentRegistry:
    registry = ComponentRegistry()
    with PluginRegistrationContext(registry) as ctx:
        BotHooksPlugin().register(ctx)
    return registry


def _resources(kb_provider: KbProvider | None) -> PoolWorkspaceResources:
    """Minimal real bundle — the factory type-checks the resources against
    PoolWorkspaceResources (rule 6: typed access at the bot boundary)."""
    import pathlib

    from modex_agent.workspace.context import WorkspaceContext as WsCtx
    from modex_agent.workspace.paths import WorkspacePaths

    root = pathlib.Path("/tmp/kb-test-ws")
    return PoolWorkspaceResources(
        target=root,
        ctx=WsCtx(target=root, paths=WorkspacePaths(root=root), is_home=False),
        overflow_store=MagicMock(),
        session_index_store=MagicMock(),
        broker=MagicMock(),
        kb_provider=kb_provider,
    )


def _ws_ctx(kb_provider: KbProvider | None) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_ctx=MagicMock(),
        workspace_resources=_resources(kb_provider),
    )


def test_kb_factory_registered_in_tool_slot() -> None:
    factory = _registered().resolve(ComponentSlot.TOOL, "kb")
    assert isinstance(factory, KbToolFactory)


async def test_kb_factory_builds_tool_from_workspace_provider() -> None:
    factory = _registered().resolve(ComponentSlot.TOOL, "kb")

    tool = await factory.create(factory.config_model(), _ws_ctx(MagicMock(spec=KbProvider)))

    assert isinstance(tool, KbTool)
    assert tool.name == "kb"


async def test_kb_factory_missing_provider_is_actionable() -> None:
    factory = _registered().resolve(ComponentSlot.TOOL, "kb")

    with pytest.raises(ValueError, match=r"KbProvider"):
        await factory.create(factory.config_model(), _ws_ctx(None))


async def test_kb_factory_missing_resources_is_actionable() -> None:
    factory = _registered().resolve(ComponentSlot.TOOL, "kb")
    ctx = WorkspaceContext(workspace_ctx=MagicMock())

    with pytest.raises(ValueError, match=r"KbProvider"):
        await factory.create(factory.config_model(), ctx)


async def test_kb_tool_task_id_provider_reads_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _registered().resolve(ComponentSlot.TOOL, "kb")
    monkeypatch.setenv("MODEX_TASK_ID", "task-123")

    tool = await factory.create(factory.config_model(), _ws_ctx(MagicMock(spec=KbProvider)))
    assert tool.name == "kb"
    assert tool._task_id_provider() == "task-123"  # noqa: SLF001


async def test_kb_tool_identity_prefers_per_turn_contextvar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The identity closures read the per-turn ContextVar channel FIRST —
    the native runtime's real identity source (os.environ is unset for
    native agents and wrong under concurrent turns)."""
    from modex_agent.runtime.env_context import _current_session_id, _modex_env

    factory = _registered().resolve(ComponentSlot.TOOL, "kb")
    monkeypatch.setenv("MODEX_TASK_ID", "stale-env-task")  # must NOT win
    monkeypatch.setenv("MODEX_SESSION_ID", "stale-env-session")

    token_env = _modex_env.set({"MODEX_TASK_ID": "graph-42"})
    token_session = _current_session_id.set("sess-live")
    try:
        tool = await factory.create(
            factory.config_model(), _ws_ctx(MagicMock(spec=KbProvider))
        )
        assert tool._task_id_provider() == "graph-42"  # noqa: SLF001
        assert tool._session_id_provider() == "sess-live"  # noqa: SLF001
    finally:
        _modex_env.reset(token_env)
        _current_session_id.reset(token_session)


async def test_kb_tool_identity_falls_back_to_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ContextVar set (e.g. the modexctl/CLI subprocess context) — the
    closures fall back to os.environ."""
    factory = _registered().resolve(ComponentSlot.TOOL, "kb")
    monkeypatch.setenv("MODEX_TASK_ID", "cli-task-9")

    tool = await factory.create(factory.config_model(), _ws_ctx(MagicMock(spec=KbProvider)))
    assert tool._task_id_provider() == "cli-task-9"  # noqa: SLF001


async def test_kb_tool_session_id_provider_reads_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _registered().resolve(ComponentSlot.TOOL, "kb")
    monkeypatch.delenv("MODEX_SESSION_ID", raising=False)

    tool = await factory.create(factory.config_model(), _ws_ctx(MagicMock(spec=KbProvider)))
    assert tool._session_id_provider() is None  # noqa: SLF001


def test_kb_builder_flag_road_is_dead() -> None:
    """Death grep: the builder flag + its private env helpers are gone."""
    from bot.service import builders

    assert not hasattr(builders, "_make_task_id_provider")
    assert not hasattr(builders, "_make_session_id_provider")
