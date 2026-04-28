"""Tests for PluginManager discovery and loading."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from framework.plugins.abc import MemoryProvider
from framework.plugins.context import PluginContext
from framework.plugins.manager import PluginManager


class DummyProvider(MemoryProvider):
    @property
    def name(self):
        return "dummy"

    async def initialize(self, **kwargs):
        pass

    async def shutdown(self):
        pass

    async def add(self, messages, context):
        return {"status": "ok"}

    async def search(self, query, context, limit=5, filters=None):
        return []


def _make_plugin_dir(tmp_path: Path, name: str, register_code: str):
    """Create a fake plugin directory with __init__.py."""
    plugin_dir = tmp_path / name
    plugin_dir.mkdir(parents=True)
    init_file = plugin_dir / "__init__.py"
    init_file.write_text(register_code, encoding="utf-8")
    return plugin_dir


class TestPluginManager:
    """PluginManager discovery and loading tests."""

    def test_init_default_user_dir(self):
        pm = PluginManager()
        assert pm._user_plugins_dir == Path.home() / ".af" / "plugins"

    def test_init_custom_user_dir(self, tmp_path: Path):
        pm = PluginManager(user_plugins_dir=tmp_path)
        assert pm._user_plugins_dir == tmp_path

    def test_load_from_directory_empty(self, tmp_path: Path):
        pm = PluginManager(user_plugins_dir=tmp_path)
        pm.discover_and_load()
        assert len(pm.list_plugins()) == 0

    def test_load_single_plugin(self, tmp_path: Path):
        code = '''
def register(ctx):
    ctx.register_tool(type("T", (), {"name": "t1"})())
'''
        _make_plugin_dir(tmp_path, "test_plugin", code)
        pm = PluginManager(user_plugins_dir=tmp_path)
        pm.discover_and_load()
        plugins = pm.list_plugins()
        assert len(plugins) == 1
        assert plugins[0]["name"] == "test_plugin"

    def test_load_with_enabled_filter(self, tmp_path: Path):
        code1 = 'def register(ctx):\n    ctx.register_tool(type("T", (), {"name": "t1"})())\n'
        code2 = 'def register(ctx):\n    ctx.register_tool(type("T", (), {"name": "t2"})())\n'
        _make_plugin_dir(tmp_path, "plugin_a", code1)
        _make_plugin_dir(tmp_path, "plugin_b", code2)

        pm = PluginManager(user_plugins_dir=tmp_path)
        pm.discover_and_load({"_enabled": ["plugin_a"]})  # type: ignore[arg-type]
        plugins = pm.list_plugins()
        assert len(plugins) == 1
        assert plugins[0]["name"] == "plugin_a"

    def test_first_seen_wins_on_collision(self, tmp_path: Path):
        """Bundled plugins should not be overridden by user plugins with same name."""
        code = 'def register(ctx):\n    ctx.register_tool(type("T", (), {"name": "first"})())\n'
        _make_plugin_dir(tmp_path, "collision_test", code)
        pm = PluginManager(user_plugins_dir=tmp_path)
        # Simulate bundled load first
        pm._load_from_directory(tmp_path, {}, source="bundled")
        # Then user load — should be skipped
        pm._load_from_directory(tmp_path, {}, source="user")
        pm._collect_all()
        plugins = pm.list_plugins()
        assert len(plugins) == 1
        # Only one tool should be registered (from the first load)
        assert len(pm.tools) == 1
        assert pm.tools[0][0].name == "first"

    def test_load_missing_register(self, tmp_path: Path, caplog):
        code = 'x = 1\n'
        _make_plugin_dir(tmp_path, "no_register", code)
        pm = PluginManager(user_plugins_dir=tmp_path)
        pm.discover_and_load()
        assert len(pm.list_plugins()) == 0
        assert "no register() function" in caplog.text

    def test_collect_tools(self, tmp_path: Path):
        code = '''
def register(ctx):
    ctx.register_tool(type("T", (), {"name": "tool1"})())
    ctx.register_tool(type("T", (), {"name": "tool2"})())
'''
        _make_plugin_dir(tmp_path, "tool_plugin", code)
        pm = PluginManager(user_plugins_dir=tmp_path)
        pm.discover_and_load()
        assert len(pm.tools) == 2

    def test_collect_hooks(self, tmp_path: Path):
        from framework.hook import Hook

        code = '''
class FakeHook:
    pass

def register(ctx):
    ctx.register_hook(FakeHook())
'''
        _make_plugin_dir(tmp_path, "hook_plugin", code)
        pm = PluginManager(user_plugins_dir=tmp_path)
        pm.discover_and_load()
        assert len(pm.hooks) == 1

    def test_collect_memory_providers(self, tmp_path: Path):
        code = '''
class DummyProvider:
    @property
    def name(self):
        return "mem"

    def is_available(self):
        return True

    async def initialize(self, **kwargs):
        pass

    async def shutdown(self):
        pass

    async def add(self, messages, context):
        return {"status": "ok"}

    async def search(self, query, context, limit=5, filters=None):
        return []

    def system_prompt_block(self):
        return ""

    def get_tool_schemas(self):
        return []

    async def handle_tool_call(self, tool_name, args):
        raise NotImplementedError

def register(ctx):
    ctx.register_memory_provider(DummyProvider())
'''
        _make_plugin_dir(tmp_path, "mem_plugin", code)
        pm = PluginManager(user_plugins_dir=tmp_path)
        pm.discover_and_load()
        assert len(pm.memory_providers) == 1
        assert pm.memory_providers[0][0].name == "mem"

    @pytest.mark.asyncio
    async def test_initialize_providers(self, tmp_path: Path):
        code = '''
class DummyProvider:
    @property
    def name(self):
        return "init_test"

    def is_available(self):
        return True

    async def initialize(self, **kwargs):
        self.initialized = True

    async def shutdown(self):
        pass

    async def add(self, messages, context):
        return {"status": "ok"}

    async def search(self, query, context, limit=5, filters=None):
        return []

    def system_prompt_block(self):
        return ""

    def get_tool_schemas(self):
        return []

    async def handle_tool_call(self, tool_name, args):
        raise NotImplementedError

def register(ctx):
    ctx.register_memory_provider(DummyProvider())
'''
        _make_plugin_dir(tmp_path, "init_plugin", code)
        pm = PluginManager(user_plugins_dir=tmp_path)
        pm.discover_and_load()
        initialized = await pm.initialize_providers()
        assert "init_test" in initialized

    @pytest.mark.asyncio
    async def test_shutdown_providers(self, tmp_path: Path):
        code = '''
class DummyProvider:
    @property
    def name(self):
        return "shutdown_test"

    def is_available(self):
        return True

    async def initialize(self, **kwargs):
        pass

    async def shutdown(self):
        self.shut_down = True

    async def add(self, messages, context):
        return {"status": "ok"}

    async def search(self, query, context, limit=5, filters=None):
        return []

    def system_prompt_block(self):
        return ""

    def get_tool_schemas(self):
        return []

    async def handle_tool_call(self, tool_name, args):
        raise NotImplementedError

def register(ctx):
    ctx.register_memory_provider(DummyProvider())
'''
        _make_plugin_dir(tmp_path, "shutdown_plugin", code)
        pm = PluginManager(user_plugins_dir=tmp_path)
        pm.discover_and_load()
        await pm.initialize_providers()
        # shutdown should not raise
        await pm.shutdown_providers()

    def test_load_from_entry_points(self):
        """Test entry_points loading with mocked importlib.metadata."""
        mock_ep = MagicMock()
        mock_ep.name = "ep_plugin"

        def mock_register(_ctx):
            _ctx.register_tool(type("T", (), {"name": "ep_tool"})())

        mock_ep.load.return_value = mock_register

        with patch("framework.plugins.manager.importlib.metadata.entry_points") as mock_eps:
            mock_eps.return_value.select.return_value = [mock_ep]
            pm = PluginManager()
            pm.discover_and_load()
            plugins = pm.list_plugins()
            assert len(plugins) == 1
            assert plugins[0]["name"] == "ep_plugin"

    def test_load_from_entry_points_fallback(self):
        """Test entry_points fallback for older Python versions."""
        mock_ep = MagicMock()
        mock_ep.name = "ep_plugin"

        def mock_register(ctx):
            pass

        mock_ep.load.return_value = mock_register

        with patch("framework.plugins.manager.importlib.metadata.entry_points") as mock_eps:
            # simulate older Python without .select()
            class OldStyleEps:
                def get(self, group, default=None):
                    if group == "framework.plugins":
                        return [mock_ep]
                    return default

            mock_eps.return_value = OldStyleEps()
            pm = PluginManager()
            pm.discover_and_load()
            plugins = pm.list_plugins()
            assert len(plugins) == 1
