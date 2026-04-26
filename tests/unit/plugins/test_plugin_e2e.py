"""End-to-end plugin flow tests."""

from pathlib import Path

import pytest

from framework.plugins import PluginLoader, PluginManager
from framework.plugins.abc import MemoryProvider


class E2EProvider(MemoryProvider):
    """A simple provider for end-to-end testing."""

    def __init__(self):
        self._memories: list[dict] = []
        self._initialized = False

    @property
    def name(self):
        return "e2e"

    def is_available(self):
        return True

    async def initialize(self, **kwargs):
        self._initialized = True

    async def shutdown(self):
        pass

    async def add(self, messages, context):
        self._memories.extend(messages)
        return {"status": "ok", "count": len(messages)}

    async def search(self, query, context, limit=5, filters=None):
        return [
            {"memory": m["content"], "score": 1.0}
            for m in self._memories
            if query.lower() in m.get("content", "").lower()
        ][:limit]


E2E_PLUGIN_CODE = '''
class E2EProvider:
    @property
    def name(self):
        return "e2e"

    def is_available(self):
        return True

    async def initialize(self, **kwargs):
        self.initialized = True

    async def shutdown(self):
        pass

    async def add(self, messages, context):
        return {"status": "ok"}

    async def search(self, query, context, limit=5, filters=None):
        return [{"memory": "e2e result", "score": 0.99}]

    def system_prompt_block(self):
        return "## E2E\\nTest block"

    def get_tool_schemas(self):
        return []

    async def handle_tool_call(self, tool_name, args):
        raise NotImplementedError

def register(ctx):
    ctx.register_memory_provider(E2EProvider())
    ctx.register_tool(type("T", (), {"name": "e2e_tool"})())
'''


class TestPluginE2E:
    """End-to-end plugin lifecycle tests."""

    def test_full_discovery_to_injection(self, tmp_path: Path):
        """Simulate full flow: discover -> load -> inject."""
        plugin_dir = tmp_path / "e2e_plugin"
        plugin_dir.mkdir()
        (plugin_dir / "__init__.py").write_text(E2E_PLUGIN_CODE, encoding="utf-8")

        pm = PluginManager(user_plugins_dir=tmp_path)
        pm.discover_and_load()

        assert len(pm.list_plugins()) == 1
        assert len(pm.memory_providers) == 1
        assert len(pm.tools) == 1

    @pytest.mark.asyncio
    async def test_provider_initialization_and_shutdown(self, tmp_path: Path):
        plugin_dir = tmp_path / "e2e_plugin"
        plugin_dir.mkdir()
        (plugin_dir / "__init__.py").write_text(E2E_PLUGIN_CODE, encoding="utf-8")

        pm = PluginManager(user_plugins_dir=tmp_path)
        pm.discover_and_load()

        initialized = await pm.initialize_providers()
        assert "e2e" in initialized

        # shutdown should not raise
        await pm.shutdown_providers()

    def test_plugin_loader_integration(self, tmp_path: Path):
        from framework.memory.system import create_memory_system

        plugin_dir = tmp_path / "e2e_plugin"
        plugin_dir.mkdir()
        (plugin_dir / "__init__.py").write_text(E2E_PLUGIN_CODE, encoding="utf-8")

        pm = PluginManager(user_plugins_dir=tmp_path)
        pm.discover_and_load()

        # Create a minimal memory system and inject
        ms = create_memory_system(tmp_path)
        loader = PluginLoader(pm)

        # inject_memory_providers is async but we only need to verify add_provider is called
        import asyncio
        asyncio.run(loader.inject_memory_providers(ms))

        providers = ms.get_providers()
        assert len(providers) == 1
        assert providers[0].name == "e2e"

    def test_multiple_plugins_isolation(self, tmp_path: Path):
        """Multiple plugins should not interfere with each other."""
        code1 = '''
def register(ctx):
    ctx.register_tool(type("T", (), {"name": "t1"})())
'''
        code2 = '''
def register(ctx):
    ctx.register_tool(type("T", (), {"name": "t2"})())
'''
        (tmp_path / "plugin_a").mkdir()
        (tmp_path / "plugin_a" / "__init__.py").write_text(code1, encoding="utf-8")
        (tmp_path / "plugin_b").mkdir()
        (tmp_path / "plugin_b" / "__init__.py").write_text(code2, encoding="utf-8")

        pm = PluginManager(user_plugins_dir=tmp_path)
        pm.discover_and_load()

        assert len(pm.list_plugins()) == 2
        tool_names = [t.name for t, _ in pm.tools]
        assert "t1" in tool_names
        assert "t2" in tool_names
