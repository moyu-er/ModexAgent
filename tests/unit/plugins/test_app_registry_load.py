"""W6 — the app-service registry load with the new packaging contract.

AppService's generic ``_load_component_registry`` block: the user plugin
directory is enabled by DEFAULT (opt-out via app-config
``user_plugins_enabled: false``), the project plugin dir points at the
deployment's plugin package, and every discovered file loads under a
QUALIFIED ``sys.modules`` key — no top-level ``plugins`` package, no
``sys.path`` mutation by the loader.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import modex_agent.plugins.loader as loader_module
from modex_agent.app.config import AppConfig
from modex_agent.app.service import AppService
from modex_agent.plugins.loader import USER_PLUGIN_PACKAGE_PREFIX
from modex_agent.scope.components import ComponentSlot

_USER_PLUGIN_SOURCE = textwrap.dedent(
    '''
    from pydantic import BaseModel

    from modex_agent.plugins.loader import Plugin, PluginRegistrationContext
    from modex_agent.scope.components import SimpleFactory


    class _Config(BaseModel):
        model_config = {"frozen": True, "extra": "forbid"}


    class UserDirPlugin(Plugin):
        config_model = _Config

        def register(self, ctx: PluginRegistrationContext) -> None:
            ctx.register_tool(
                "user_dir_tool",
                SimpleFactory(instance="from-user-dir", config_model=_Config),
            )
    '''
)


class _SkeletonService(AppService):
    """The abstract lifecycle steps are irrelevant to the registry block."""

    async def initialize(self) -> None: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


def _service(tmp_path: Path, app_config: AppConfig | None) -> _SkeletonService:
    service = _SkeletonService(
        config_dir=tmp_path / "config",
        input_adapter=MagicMock(),
        output_adapter=MagicMock(),
        emitter_factory=MagicMock(),
        resource_root=tmp_path,
    )
    # The deployment's config step fills the slot after construction (the
    # same road BotService takes); the registry block reads it.
    service._app_config = app_config  # noqa: SLF001 — test seam
    return service


async def test_user_dir_enabled_by_default_and_qualified_module_name(
    tmp_path: Path, monkeypatch
) -> None:
    user_dir = tmp_path / "home-plugins"
    user_dir.mkdir()
    (user_dir / "user_tool_plugin.py").write_text(_USER_PLUGIN_SOURCE, encoding="utf-8")
    monkeypatch.setattr(loader_module, "DEFAULT_USER_PLUGIN_DIR", user_dir)

    service = _service(tmp_path, None)
    registry = await service._load_component_registry()

    # Default-on: the user plugin's factory is registered (source USER
    # would override any same-name project entry — priority contract).
    assert "user_dir_tool" in registry.names(ComponentSlot.TOOL)
    # Qualified loading: the plugin module lives under the synthetic
    # userplugins namespace — no top-level ``plugins`` package appears.
    qualified = [
        name
        for name in sys.modules
        if name.startswith(USER_PLUGIN_PACKAGE_PREFIX)
        and name.endswith(".user_tool_plugin")
    ]
    assert qualified, "the user plugin must load under a qualified name"
    assert "plugins" not in sys.modules
    assert "plugins.user_tool_plugin" not in sys.modules


async def test_user_dir_opt_out_via_config(
    tmp_path: Path, monkeypatch
) -> None:
    user_dir = tmp_path / "home-plugins"
    user_dir.mkdir()
    (user_dir / "user_tool_plugin.py").write_text(_USER_PLUGIN_SOURCE, encoding="utf-8")
    monkeypatch.setattr(loader_module, "DEFAULT_USER_PLUGIN_DIR", user_dir)

    service = _service(
        tmp_path, AppConfig.model_validate({"user_plugins_enabled": False})
    )
    registry = await service._load_component_registry()
    assert "user_dir_tool" not in registry.names(ComponentSlot.TOOL)
