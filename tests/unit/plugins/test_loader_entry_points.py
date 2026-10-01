"""W6 packaging contract — entry_points + installed-layout discovery.

Two red anchors for the plugin packaging contract (the directory layout is
NOT the API):

- **Entry points** — a distribution exposing a ``modex_agent.plugins``
  entry point pointing at a ``Plugin`` subclass is discovered and its
  factories registered through the loader's real
  ``_discover_entry_points`` path (stubbed ``importlib.metadata`` feed —
  no real distribution needed).
- **Installed layout (wheel shape)** — a temp site-packages-like
  directory containing a plugin package plus ``*.dist-info`` entry-point
  metadata is discovered by the REAL
  ``importlib.metadata.entry_points()`` scanner once the dir is on
  ``sys.path`` (test-simulated installation), and the plugin's factories
  reach the registry through the loader's create path. No pip install.
"""

from __future__ import annotations

import importlib.metadata
import textwrap
from pathlib import Path

from modex_agent.plugins.loader import (
    ComponentRegistryLoader,
    PluginDiscoveryConfig,
)
from modex_agent.scope.component_registry import ComponentRegistry
from modex_agent.scope.components import ComponentSlot

_ENTRY_POINT_GROUP = "modex_agent.plugins"

_PLUGIN_PACKAGE_SOURCE = textwrap.dedent(
    '''
    from pydantic import BaseModel

    from modex_agent.plugins.loader import Plugin, PluginRegistrationContext
    from modex_agent.scope.components import SimpleFactory


    class _Config(BaseModel):
        model_config = {"frozen": True, "extra": "forbid"}


    class WheelPlugin(Plugin):
        config_model = _Config

        def register(self, ctx: PluginRegistrationContext) -> None:
            ctx.register_tool(
                "wheel_tool",
                SimpleFactory(instance="from-wheel", config_model=_Config),
            )
    '''
)


class TestEntryPointDiscovery:
    async def test_entry_point_plugin_registers_through_loader(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A ``Plugin`` subclass reachable via an entry point in the
        ``modex_agent.plugins`` group is discovered, instantiated, and its
        create-path factory registered — the loader's entry-point road."""
        package_dir = tmp_path / "acme_pkg"
        package_dir.mkdir()
        (package_dir / "__init__.py").write_text("", encoding="utf-8")
        (package_dir / "plugin.py").write_text(_PLUGIN_PACKAGE_SOURCE, encoding="utf-8")
        monkeypatch.syspath_prepend(tmp_path)

        entry_point = importlib.metadata.EntryPoint(
            name="acme",
            value="acme_pkg.plugin:WheelPlugin",
            group=_ENTRY_POINT_GROUP,
        )
        entry_points = importlib.metadata.EntryPoints([entry_point])
        monkeypatch.setattr(
            importlib.metadata,
            "entry_points",
            lambda: entry_points,
        )

        registry = ComponentRegistry()
        await ComponentRegistryLoader.load(
            registry,
            PluginDiscoveryConfig(
                bundled_factories=(),
                project_plugin_paths=(),
                user_plugin_path=None,
            ),
        )
        assert registry.names(ComponentSlot.TOOL) == ("wheel_tool",)

    async def test_non_plugin_entry_point_is_skipped_with_warning(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """An entry point resolving to a non-Plugin object logs a warning
        and does not abort the load (fault isolation)."""
        import logging

        package_dir = tmp_path / "acme_pkg2"
        package_dir.mkdir()
        (package_dir / "__init__.py").write_text("", encoding="utf-8")
        (package_dir / "not_a_plugin.py").write_text(
            "SOMETHING = object()\n", encoding="utf-8"
        )
        monkeypatch.syspath_prepend(tmp_path)

        entry_point = importlib.metadata.EntryPoint(
            name="bad",
            value="acme_pkg2.not_a_plugin:SOMETHING",
            group=_ENTRY_POINT_GROUP,
        )
        monkeypatch.setattr(
            importlib.metadata,
            "entry_points",
            lambda: importlib.metadata.EntryPoints([entry_point]),
        )
        registry = ComponentRegistry()
        with caplog.at_level(logging.WARNING):
            await ComponentRegistryLoader.load(
                registry,
                PluginDiscoveryConfig(
                    bundled_factories=(),
                    project_plugin_paths=(),
                    user_plugin_path=None,
                ),
            )
        assert registry.names(ComponentSlot.TOOL) == ()
        assert any("not a Plugin subclass" in record.message for record in caplog.records)


class TestInstalledWheelLayout:
    async def test_dist_info_entry_points_metadata_discovers_plugin(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The wheel shape: ``<site>/acme_plugins/`` (the package) +
        ``<site>/acme_plugins-1.0.dist-info/`` (entry-point metadata) is
        discovered by the REAL ``importlib.metadata.entry_points()``
        scanner once on ``sys.path`` — the loader finds the plugin with
        zero directory-discovery input."""
        site_dir = tmp_path / "site-packages"
        package_dir = site_dir / "acme_plugins"
        package_dir.mkdir(parents=True)
        (package_dir / "__init__.py").write_text("", encoding="utf-8")
        (package_dir / "plugin.py").write_text(_PLUGIN_PACKAGE_SOURCE, encoding="utf-8")

        dist_info = site_dir / "acme_plugins-1.0.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: acme-plugins\nVersion: 1.0\n",
            encoding="utf-8",
        )
        (dist_info / "entry_points.txt").write_text(
            f"[{_ENTRY_POINT_GROUP}]\nacme = acme_plugins.plugin:WheelPlugin\n",
            encoding="utf-8",
        )
        # Test-only sys.path prepend simulates the installed environment
        # (the loader itself never mutates sys.path).
        monkeypatch.syspath_prepend(site_dir)

        discovered = ComponentRegistryLoader._discover_entry_points(
            _ENTRY_POINT_GROUP
        )
        assert [cls.__name__ for cls in discovered] == ["WheelPlugin"]

        registry = ComponentRegistry()
        await ComponentRegistryLoader.load(
            registry,
            PluginDiscoveryConfig(
                bundled_factories=(),
                project_plugin_paths=(),
                user_plugin_path=None,
            ),
        )
        assert registry.names(ComponentSlot.TOOL) == ("wheel_tool",)
        factory = registry.resolve(ComponentSlot.TOOL, "wheel_tool")
        # The create-path works: the factory resolves and produces.
        from modex_agent.plugins.assembly.context import (
            PoolRuntimeDeps,
            resolution_context,
        )
        from modex_agent.workspace.context import WorkspaceContext
        from modex_agent.workspace.paths import WorkspacePaths

        ctx = resolution_context(
            registry,
            WorkspaceContext(
                target=tmp_path,
                paths=WorkspacePaths(root=tmp_path / "data"),
                is_home=False,
            ),
            PoolRuntimeDeps(),
        )
        product = await factory.create(factory.config_model(), ctx)
        assert product == "from-wheel"
